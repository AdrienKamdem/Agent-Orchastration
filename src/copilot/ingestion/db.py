"""Database access: plain SQL through psycopg 3.

No ORM on purpose. The schema is small, and seeing the SQL makes the idempotency and
versioning rules explicit. Every function takes a connection; the caller owns the
transaction (the pipeline commits after each document, so one failure never rolls back
another document's work).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from importlib import resources
from typing import Any

import numpy as np
import psycopg
from pgvector.psycopg import register_vector
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from .models import Chunk

Conn = psycopg.Connection[dict[str, Any]]


def connect(database_url: str) -> Conn:
    conn: Conn = psycopg.connect(database_url, row_factory=dict_row)
    _try_register_vector(conn)
    return conn


def _try_register_vector(conn: Conn) -> None:
    try:
        register_vector(conn)
    except psycopg.ProgrammingError:
        conn.rollback()  # extension not created yet; migrate() registers it afterwards


# --------------------------------------------------------------------------- migrations


def migrate(conn: Conn) -> list[str]:
    """Apply any SQL files in ``migrations/`` that haven't been applied yet, in order."""
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations ("
        " name TEXT PRIMARY KEY, applied_at TIMESTAMPTZ NOT NULL DEFAULT now())"
    )
    applied = {r["name"] for r in conn.execute("SELECT name FROM schema_migrations")}
    files = sorted(
        (
            f
            for f in resources.files("copilot.ingestion.migrations").iterdir()
            if f.name.endswith(".sql")
        ),
        key=lambda f: f.name,
    )
    newly: list[str] = []
    for f in files:
        if f.name in applied:
            continue
        conn.execute(f.read_text())
        conn.execute("INSERT INTO schema_migrations (name) VALUES (%s)", (f.name,))
        newly.append(f.name)
    conn.commit()
    _try_register_vector(conn)
    return newly


# --------------------------------------------------------------------------- sources


def upsert_source(conn: Conn, source_id: str, kind: str, config: dict[str, Any]) -> None:
    conn.execute(
        """
        INSERT INTO sources (id, kind, config) VALUES (%s, %s, %s)
        ON CONFLICT (id) DO UPDATE
            SET kind = EXCLUDED.kind, config = EXCLUDED.config, updated_at = now()
        """,
        (source_id, kind, Jsonb(config)),
    )


def get_cursor(conn: Conn, source_id: str) -> dict[str, Any] | None:
    row = conn.execute("SELECT cursor FROM sources WHERE id = %s", (source_id,)).fetchone()
    return row["cursor"] if row else None


def set_cursor(conn: Conn, source_id: str, cursor: dict[str, Any]) -> None:
    conn.execute(
        "UPDATE sources SET cursor = %s, updated_at = now() WHERE id = %s",
        (Jsonb(cursor), source_id),
    )


# --------------------------------------------------------------------------- documents


@dataclass
class DocumentRow:
    id: int
    source_id: str
    external_id: str
    version: int
    url: str | None
    title: str | None
    published_at: datetime | None
    content_hash: str
    mime_type: str | None
    raw_path: str
    status: str
    error: str | None
    metadata: dict[str, Any]


_DOC_COLS = (
    "id, source_id, external_id, version, url, title, published_at, content_hash, "
    "mime_type, raw_path, status, error, metadata"
)


def latest_document(conn: Conn, source_id: str, external_id: str) -> DocumentRow | None:
    row = conn.execute(
        f"SELECT {_DOC_COLS} FROM documents WHERE source_id = %s AND external_id = %s "
        "ORDER BY version DESC LIMIT 1",
        (source_id, external_id),
    ).fetchone()
    return DocumentRow(**row) if row else None


def known_external_ids(conn: Conn, source_id: str) -> set[str]:
    rows = conn.execute(
        "SELECT DISTINCT external_id FROM documents WHERE source_id = %s", (source_id,)
    )
    return {r["external_id"] for r in rows}


def hash_exists(conn: Conn, source_id: str, content_hash: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM documents WHERE source_id = %s AND content_hash = %s LIMIT 1",
        (source_id, content_hash),
    ).fetchone()
    return row is not None


def insert_document(
    conn: Conn,
    *,
    source_id: str,
    external_id: str,
    version: int,
    url: str | None,
    title: str | None,
    published_at: datetime | None,
    content_hash: str,
    mime_type: str,
    raw_path: str,
    metadata: dict[str, Any],
) -> int:
    row = conn.execute(
        """
        INSERT INTO documents (source_id, external_id, version, url, title, published_at,
                               content_hash, mime_type, raw_path, status, metadata)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'fetched', %s)
        RETURNING id
        """,
        (
            source_id,
            external_id,
            version,
            url,
            title,
            published_at,
            content_hash,
            mime_type,
            raw_path,
            Jsonb(metadata),
        ),
    ).fetchone()
    assert row is not None
    return int(row["id"])


def set_status(
    conn: Conn,
    document_id: int,
    status: str,
    *,
    error: str | None = None,
    parser_version: str | None = None,
) -> None:
    conn.execute(
        "UPDATE documents SET status = %s, error = %s, "
        "parser_version = COALESCE(%s, parser_version) WHERE id = %s",
        (status, error, parser_version, document_id),
    )


def documents_by_status(
    conn: Conn, statuses: list[str], source_id: str | None = None
) -> list[DocumentRow]:
    sql = f"SELECT {_DOC_COLS} FROM documents WHERE status = ANY(%s)"
    params: list[Any] = [statuses]
    if source_id:
        sql += " AND source_id = %s"
        params.append(source_id)
    sql += " ORDER BY id"
    return [DocumentRow(**r) for r in conn.execute(sql, params)]


def latest_versions(conn: Conn, source_id: str | None = None) -> list[DocumentRow]:
    sql = f"SELECT DISTINCT ON (source_id, external_id) {_DOC_COLS} FROM documents"
    params: list[Any] = []
    if source_id:
        sql += " WHERE source_id = %s"
        params.append(source_id)
    sql += " ORDER BY source_id, external_id, version DESC"
    return [DocumentRow(**r) for r in conn.execute(sql, params)]


# --------------------------------------------------------------------------- chunks


def replace_chunks(conn: Conn, document_id: int, chunker: str, chunks: list[Chunk]) -> None:
    """Delete-then-insert, so re-chunking a document with the same chunker is idempotent."""
    conn.execute(
        "DELETE FROM chunks WHERE document_id = %s AND chunker = %s", (document_id, chunker)
    )
    with conn.cursor() as cur:
        cur.executemany(
            """
            INSERT INTO chunks (document_id, ordinal, text, token_count, section, page,
                                char_start, char_end, chunker)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            [
                (
                    document_id,
                    c.ordinal,
                    c.text,
                    c.token_count,
                    c.section,
                    c.page,
                    c.char_start,
                    c.char_end,
                    chunker,
                )
                for c in chunks
            ],
        )


def chunks_needing_embedding(
    conn: Conn, document_id: int, chunker: str, model: str
) -> list[dict[str, Any]]:
    return list(
        conn.execute(
            "SELECT id, text FROM chunks WHERE document_id = %s AND chunker = %s "
            "AND (embedding IS NULL OR embedding_model IS DISTINCT FROM %s) ORDER BY ordinal",
            (document_id, chunker, model),
        )
    )


def set_embeddings(conn: Conn, ids: list[int], vectors: np.ndarray, model: str) -> None:
    with conn.cursor() as cur:
        cur.executemany(
            "UPDATE chunks SET embedding = %s, embedding_model = %s WHERE id = %s",
            [(vec, model, cid) for cid, vec in zip(ids, vectors, strict=True)],
        )


@dataclass
class SearchHit:
    score: float
    text: str
    section: str | None
    page: int | None
    char_start: int
    char_end: int
    document_id: int
    title: str | None
    url: str | None
    published_at: datetime | None
    source_id: str
    metadata: dict[str, Any]


def search(
    conn: Conn,
    query_vector: np.ndarray,
    *,
    model: str,
    chunker: str,
    k: int = 5,
    source_id: str | None = None,
    ticker: str | None = None,
) -> list[SearchHit]:
    where = [
        "c.embedding_model = %s",
        "c.chunker = %s",
        # only the latest version of each document
        "NOT EXISTS (SELECT 1 FROM documents d2 WHERE d2.source_id = d.source_id "
        "AND d2.external_id = d.external_id AND d2.version > d.version)",
    ]
    params: list[Any] = [query_vector, model, chunker]
    if source_id:
        where.append("d.source_id = %s")
        params.append(source_id)
    if ticker:
        where.append("d.metadata->>'ticker' = %s")
        params.append(ticker.upper())
    params.append(k)
    # `<=>` is pgvector's cosine distance; similarity = 1 - distance.
    sql = f"""
        SELECT 1 - (c.embedding <=> %s) AS score, c.text, c.section, c.page,
               c.char_start, c.char_end, d.id AS document_id, d.title, d.url,
               d.published_at, d.source_id, d.metadata
        FROM chunks c JOIN documents d ON d.id = c.document_id
        WHERE {" AND ".join(where)}
        ORDER BY c.embedding <=> %s
        LIMIT %s
    """
    params.insert(len(params) - 1, query_vector)
    return [SearchHit(**r) for r in conn.execute(sql, params)]


# --------------------------------------------------------------------------- runs


def start_run(conn: Conn, source_id: str) -> int:
    row = conn.execute(
        "INSERT INTO ingestion_runs (source_id) VALUES (%s) RETURNING id", (source_id,)
    ).fetchone()
    conn.commit()
    assert row is not None
    return int(row["id"])


def finish_run(conn: Conn, run_id: int, status: str, stats: dict[str, Any]) -> None:
    conn.execute(
        "UPDATE ingestion_runs SET finished_at = now(), status = %s, stats = %s WHERE id = %s",
        (status, Jsonb(stats), run_id),
    )
    conn.commit()


def recent_runs(conn: Conn, limit: int = 20) -> list[dict[str, Any]]:
    return list(
        conn.execute(
            "SELECT id, source_id, started_at, finished_at, status, stats "
            "FROM ingestion_runs ORDER BY id DESC LIMIT %s",
            (limit,),
        )
    )


def status_counts(conn: Conn) -> list[dict[str, Any]]:
    return list(
        conn.execute(
            "SELECT source_id, status, count(*) AS n FROM documents "
            "GROUP BY source_id, status ORDER BY source_id, status"
        )
    )


def failed_documents(conn: Conn, limit: int = 20) -> list[dict[str, Any]]:
    return list(
        conn.execute(
            "SELECT id, source_id, external_id, title, error FROM documents "
            "WHERE status = 'failed' ORDER BY id DESC LIMIT %s",
            (limit,),
        )
    )


def reset_failed(conn: Conn, source_id: str | None = None) -> int:
    sql = "UPDATE documents SET status = 'fetched', error = NULL WHERE status = 'failed'"
    params: list[Any] = []
    if source_id:
        sql += " AND source_id = %s"
        params.append(source_id)
    cur = conn.execute(sql, params)
    conn.commit()
    return cur.rowcount
