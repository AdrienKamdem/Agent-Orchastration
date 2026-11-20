"""The pipeline: runs connectors, then pushes each document through the processing steps.

Two phases, matching the architecture in the plan:

    acquire(source):  discover -> skip known -> fetch -> raw store -> documents row
    process():        for each unfinished doc: parse -> chunk -> embed  (status per step)

Failure handling:
* The DB commits after every document, so one failure never undoes others.
* An exception while processing a document marks it ``failed`` with the error, and the
  run continues. ``ingest retry`` resets failed documents and processes them again.
* The source cursor is only saved when every fetch succeeded. Otherwise the next run
  discovers the same items again, and dedupe skips the ones already stored.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import structlog

from . import db
from .blobstore import LocalBlobStore, parsed_key, raw_key, sha256
from .chunking import DocumentChunker
from .config import (
    EdgarSourceConfig,
    PdfInboxSourceConfig,
    RssSourceConfig,
    Settings,
    SourceConfig,
    SourcesConfig,
)
from .connectors.base import Connector
from .connectors.edgar import SEC_HOSTS, EdgarConnector
from .connectors.pdf_inbox import PdfInboxConnector
from .connectors.rss import RssConnector
from .embedding import Embedder
from .http import HttpClient, RateLimiter
from .models import DiscoveredItem, ParsedDocument, RawDocument
from .parsers import parser_for

log = structlog.get_logger(__name__)


def make_http_client(settings: Settings, **kwargs: Any) -> HttpClient:
    limiter = RateLimiter(per_host_rps=dict.fromkeys(SEC_HOSTS, 8.0), default_rps=2.0)
    return HttpClient(settings.http_user_agent, rate_limiter=limiter, **kwargs)


def make_connector(
    cfg: SourceConfig, settings: Settings, sources: SourcesConfig, http: HttpClient
) -> Connector:
    if isinstance(cfg, RssSourceConfig):
        return RssConnector(cfg, http, sources.watchlist)
    if isinstance(cfg, EdgarSourceConfig):
        return EdgarConnector(
            cfg, http, sources.watchlist, settings.sec_user_agent, settings.data_dir / "cache"
        )
    if isinstance(cfg, PdfInboxSourceConfig):
        return PdfInboxConnector(cfg, settings.data_dir)
    raise TypeError(f"no connector for {type(cfg).__name__}")


@dataclass
class AcquireStats:
    discovered: int = 0
    new: int = 0
    new_versions: int = 0
    skipped: int = 0
    failed: int = 0
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "discovered": self.discovered,
            "new": self.new,
            "new_versions": self.new_versions,
            "skipped": self.skipped,
            "failed": self.failed,
            "errors": self.errors[:20],
        }


@dataclass
class ProcessStats:
    processed: int = 0
    failed: int = 0
    chunks: int = 0


class Pipeline:
    def __init__(
        self,
        conn: db.Conn,
        store: LocalBlobStore,
        chunker: DocumentChunker,
        embedder: Embedder | None,
    ):
        self.conn = conn
        self.store = store
        self.chunker = chunker
        self.embedder = embedder

    # ================================================================== acquire

    def acquire(
        self, connector: Connector, cfg: SourceConfig, *, refresh: bool = False
    ) -> AcquireStats:
        """Discover and fetch new documents for one source into the raw store.

        ``refresh=False`` (default): items whose external_id we already have are skipped
        without fetching. Cheap, and right for immutable things like filings.
        ``refresh=True``: re-fetch known items too; if the bytes changed, store a new version.
        """
        source_id = cfg.id
        db.upsert_source(self.conn, source_id, cfg.kind, cfg.model_dump(mode="json"))
        self.conn.commit()
        run_id = db.start_run(self.conn, source_id)
        stats = AcquireStats()
        bound = log.bind(source=source_id, run_id=run_id)

        try:
            cursor = db.get_cursor(self.conn, source_id)
            items, new_cursor = connector.discover(cursor)
            stats.discovered = len(items)
            known = db.known_external_ids(self.conn, source_id)

            for item in items:
                if item.external_id in known and not refresh:
                    stats.skipped += 1
                    connector.acknowledge(item)
                    continue
                try:
                    outcome = self._store_item(connector, item)
                    connector.acknowledge(item)
                except Exception as e:
                    self.conn.rollback()
                    stats.failed += 1
                    stats.errors.append(f"{item.external_id}: {type(e).__name__}: {e}")
                    bound.warning("acquire.item_failed", item=item.external_id, error=str(e))
                    continue
                if outcome == "new":
                    stats.new += 1
                elif outcome == "new_version":
                    stats.new_versions += 1
                else:
                    stats.skipped += 1

            if stats.failed == 0:
                db.set_cursor(self.conn, source_id, new_cursor)
                self.conn.commit()
            status = "ok" if stats.failed == 0 else "partial"
        except Exception as e:
            self.conn.rollback()
            stats.errors.append(f"discover: {type(e).__name__}: {e}")
            bound.error("acquire.failed", error=str(e))
            status = "failed"

        db.finish_run(self.conn, run_id, status, stats.as_dict())
        bound.info(
            "acquire.done",
            status=status,
            **{k: v for k, v in stats.as_dict().items() if k != "errors"},
        )
        return stats

    def _store_item(self, connector: Connector, item: DiscoveredItem) -> str:
        raw = connector.fetch(item)
        digest = sha256(raw.content)
        previous = db.latest_document(self.conn, raw.source_id, raw.external_id)
        if previous and previous.content_hash == digest:
            return "unchanged"
        if previous is None and db.hash_exists(self.conn, raw.source_id, digest):
            return "duplicate_content"  # same bytes under another ID (e.g. syndicated item)

        when = raw.published_at or datetime.now(UTC)
        key = raw_key(raw.source_id, when, digest, raw.mime_type)
        self.store.put(key, raw.content)
        self.store.put_json(
            key + ".meta.json",
            {
                "source_id": raw.source_id,
                "external_id": raw.external_id,
                "url": raw.url,
                "fetched_at": datetime.now(UTC).isoformat(),
                "mime_type": raw.mime_type,
                "headers": raw.headers,
            },
        )
        db.insert_document(
            self.conn,
            source_id=raw.source_id,
            external_id=raw.external_id,
            version=(previous.version + 1) if previous else 1,
            url=raw.url,
            title=raw.title,
            published_at=raw.published_at,
            content_hash=digest,
            mime_type=raw.mime_type,
            raw_path=key,
            metadata=raw.metadata,
        )
        self.conn.commit()
        return "new_version" if previous else "new"

    # ================================================================== process

    def process(self, source_id: str | None = None) -> ProcessStats:
        """Parse, chunk and embed every document that hasn't reached ``embedded`` yet."""
        stats = ProcessStats()
        docs = db.documents_by_status(self.conn, ["fetched", "parsed", "chunked"], source_id)
        for doc in docs:
            n = self._process_one(doc)
            if n is None:
                stats.failed += 1
            else:
                stats.processed += 1
                stats.chunks += n
        log.info(
            "process.done", processed=stats.processed, failed=stats.failed, chunks=stats.chunks
        )
        return stats

    def reindex(self, source_id: str | None = None) -> ProcessStats:
        """Re-parse, re-chunk and re-embed the latest version of every document from raw.

        Use after changing a parser, the chunker settings or the embedding model. Chunks
        from other chunker configurations are kept, so you can compare them side by side.
        """
        stats = ProcessStats()
        for doc in db.latest_versions(self.conn, source_id):
            n = self._process_one(doc)
            if n is None:
                stats.failed += 1
            else:
                stats.processed += 1
                stats.chunks += n
        return stats

    def _process_one(self, doc: db.DocumentRow) -> int | None:
        bound = log.bind(document_id=doc.id, source=doc.source_id)
        try:
            raw = RawDocument(
                source_id=doc.source_id,
                external_id=doc.external_id,
                url=doc.url,
                title=doc.title,
                published_at=doc.published_at,
                content=self.store.get(doc.raw_path),
                mime_type=doc.mime_type or "",
                metadata=doc.metadata,
            )
            parser = parser_for(raw)
            parsed = parser.parse(raw)
            if not parsed.text.strip():
                raise ValueError("parser produced empty text (scanned PDF? JS-only page?)")
            self.store.put(parsed_key(doc.id), parsed.to_json().encode())
            db.set_status(self.conn, doc.id, "parsed", parser_version=parser.version)
            self.conn.commit()

            chunks = self.chunker.chunk_document(parsed)
            db.replace_chunks(self.conn, doc.id, self.chunker.name, chunks)
            db.set_status(self.conn, doc.id, "chunked")
            self.conn.commit()

            if self.embedder is not None:
                self._embed(doc.id)
                db.set_status(self.conn, doc.id, "embedded")
                self.conn.commit()
            bound.info("process.document_ok", chunks=len(chunks), parser=parser.version)
            return len(chunks)
        except Exception as e:
            self.conn.rollback()
            db.set_status(self.conn, doc.id, "failed", error=f"{type(e).__name__}: {e}")
            self.conn.commit()
            bound.warning("process.document_failed", error=str(e))
            return None

    def _embed(self, document_id: int, batch_size: int = 64) -> None:
        assert self.embedder is not None
        rows = db.chunks_needing_embedding(
            self.conn, document_id, self.chunker.name, self.embedder.name
        )
        for i in range(0, len(rows), batch_size):
            batch = rows[i : i + batch_size]
            vectors = self.embedder.embed_documents([r["text"] for r in batch])
            db.set_embeddings(self.conn, [r["id"] for r in batch], vectors, self.embedder.name)


def load_parsed(store: LocalBlobStore, document_id: int) -> ParsedDocument:
    return ParsedDocument.from_json(store.get(parsed_key(document_id)))


def ensure_dirs(settings: Settings) -> None:
    for sub in ("raw", "parsed", "cache", "inbox"):
        Path(settings.data_dir, sub).mkdir(parents=True, exist_ok=True)
