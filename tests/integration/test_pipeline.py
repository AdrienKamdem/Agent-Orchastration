"""End-to-end pipeline against a real Postgres + pgvector, with mocked HTTP.

Run with:  TEST_DATABASE_URL=postgresql://copilot:copilot@localhost:5432/copilot_test \
           uv run pytest -m integration
The schema in that database is dropped and recreated, so never point it at real data.
"""

from __future__ import annotations

import os
from datetime import UTC, date, datetime

import httpx
import pytest
import respx

from copilot.ingestion import db
from copilot.ingestion.blobstore import LocalBlobStore
from copilot.ingestion.chunking import make_chunker
from copilot.ingestion.config import (
    EdgarSourceConfig,
    PdfInboxSourceConfig,
    RssSourceConfig,
    WatchlistEntry,
)
from copilot.ingestion.connectors.edgar import EdgarConnector
from copilot.ingestion.connectors.pdf_inbox import PdfInboxConnector
from copilot.ingestion.connectors.rss import RssConnector
from copilot.ingestion.embedding import HashingEmbedder
from copilot.ingestion.pipeline import Pipeline, load_parsed
from tests.conftest import fixture_bytes
from tests.unit.test_parsers import _make_pdf

pytestmark = pytest.mark.integration

DB_URL = os.environ.get("TEST_DATABASE_URL")
FED_URL = "https://www.federalreserve.gov/feeds/press_monetary.xml"
NOW = datetime(2026, 11, 20, tzinfo=UTC)
ARCHIVE = "https://www.sec.gov/Archives/edgar/data/1045810/"


@pytest.fixture
def conn():
    if not DB_URL:
        pytest.skip("TEST_DATABASE_URL not set")
    c = db.connect(DB_URL)
    c.execute("DROP SCHEMA public CASCADE")
    c.execute("CREATE SCHEMA public")
    c.commit()
    db.migrate(c)
    yield c
    c.close()


@pytest.fixture
def pipeline(conn, tmp_path):
    return Pipeline(
        conn,
        LocalBlobStore(tmp_path / "data"),
        make_chunker("section_aware", 120, 20),
        HashingEmbedder(),
    )


def _fed_connector(http):
    cfg = RssSourceConfig(id="fed", kind="cb_statement", url=FED_URL)
    return cfg, RssConnector(cfg, http, now=NOW)


def _mock_fed(statement=b"", minutes_status=200):
    respx.get(FED_URL).mock(return_value=httpx.Response(200, content=fixture_bytes("fed_feed.xml")))
    respx.get(url__regex=r".*monetary20261104a\.htm.*").mock(
        return_value=httpx.Response(
            200,
            content=statement or fixture_bytes("fed_statement.html"),
            headers={"Content-Type": "text/html"},
        )
    )
    respx.get(url__regex=r".*monetary20261014a\.htm.*").mock(
        return_value=httpx.Response(
            minutes_status,
            content=b"<html><body><p>" + b"Minutes text. " * 40 + b"</p></body></html>",
            headers={"Content-Type": "text/html"},
        )
    )


def _count(conn, sql):
    return conn.execute(sql).fetchone()["n"]


@respx.mock
def test_rss_end_to_end_is_idempotent_and_searchable(conn, pipeline, http):
    _mock_fed()
    cfg, connector = _fed_connector(http)

    st = pipeline.acquire(connector, cfg)
    assert (st.discovered, st.new, st.failed) == (2, 2, 0)
    p = pipeline.process()
    assert p.failed == 0 and p.chunks > 0
    assert _count(conn, "SELECT count(*) n FROM documents WHERE status='embedded'") == 2
    n_chunks = _count(conn, "SELECT count(*) n FROM chunks WHERE embedding IS NOT NULL")
    assert db.get_cursor(conn, "fed") is not None

    # second run: nothing new, nothing duplicated
    st2 = pipeline.acquire(connector, cfg)
    assert (st2.new, st2.skipped) == (0, 2)
    assert pipeline.process().processed == 0
    assert _count(conn, "SELECT count(*) n FROM chunks") == n_chunks

    hits = db.search(
        conn,
        HashingEmbedder().embed_query("balance sheet runoff treasury"),
        model="hashing-384",
        chunker=pipeline.chunker.name,
        k=3,
    )
    assert hits and "runoff" in hits[0].text
    assert hits[0].url and hits[0].url.endswith("monetary20261104a.htm?utm_source=rss")

    # citation invariant holds through the DB round trip
    parsed = load_parsed(pipeline.store, hits[0].document_id)
    assert parsed.text[hits[0].char_start : hits[0].char_end] == hits[0].text

    runs = db.recent_runs(conn)
    assert [r["status"] for r in runs] == ["ok", "ok"]


@respx.mock
def test_fetch_failure_keeps_cursor_and_is_retried_next_run(conn, pipeline, http):
    _mock_fed(minutes_status=404)
    cfg, connector = _fed_connector(http)
    st = pipeline.acquire(connector, cfg)
    assert (st.new, st.failed) == (1, 1)
    assert db.get_cursor(conn, "fed") is None  # not advanced past the failure
    assert db.recent_runs(conn)[0]["status"] == "partial"

    respx.reset()
    _mock_fed(minutes_status=200)
    st2 = pipeline.acquire(connector, cfg)
    assert (st2.new, st2.skipped, st2.failed) == (1, 1, 0)


@respx.mock
def test_refresh_creates_new_version_and_search_uses_latest(conn, pipeline, http):
    _mock_fed()
    cfg, connector = _fed_connector(http)
    pipeline.acquire(connector, cfg)
    pipeline.process()

    respx.reset()
    _mock_fed(
        statement=b"<html><body><article><p>" + b"The Committee decided to lower the "
        b"target range by 25 basis points citing a weaker labour market. "
        * 6
        + b"</p></article></body></html>"
    )
    st = pipeline.acquire(connector, cfg, refresh=True)
    assert (st.new_versions, st.skipped) == (1, 1)  # statement changed, minutes did not
    pipeline.process()

    versions = conn.execute(
        "SELECT version FROM documents WHERE external_id='monetary20261104a' ORDER BY version"
    ).fetchall()
    assert [v["version"] for v in versions] == [1, 2]  # old version kept for diffing

    hits = db.search(
        conn,
        HashingEmbedder().embed_query("committee target range decision"),
        model="hashing-384",
        chunker=pipeline.chunker.name,
        k=10,
    )
    texts = " ".join(h.text for h in hits)
    assert "25 basis points" in texts and "runoff" not in texts


@respx.mock
def test_edgar_end_to_end_with_sections(conn, pipeline, http, tmp_path):
    respx.get("https://www.sec.gov/files/company_tickers.json").mock(
        return_value=httpx.Response(200, content=fixture_bytes("company_tickers.json"))
    )
    respx.get("https://data.sec.gov/submissions/CIK0001045810.json").mock(
        return_value=httpx.Response(200, content=fixture_bytes("submissions_nvda.json"))
    )
    respx.get(url__regex=r".*-index\.htm$").mock(
        return_value=httpx.Response(200, content=fixture_bytes("edgar_8k_index.htm"))
    )
    respx.get(url__startswith=ARCHIVE).mock(
        return_value=httpx.Response(
            200, content=fixture_bytes("edgar_10k.htm"), headers={"Content-Type": "text/html"}
        )
    )

    cfg = EdgarSourceConfig(id="edgar")
    connector = EdgarConnector(
        cfg,
        http,
        [WatchlistEntry(ticker="NVDA", name="NVIDIA")],
        "Test test@example.com",
        tmp_path / "cache",
        today=date(2026, 11, 20),
    )
    st = pipeline.acquire(connector, cfg)
    # 3 documents share identical (fixture) bytes with the first: deduped by content hash
    assert st.discovered == 5 and st.new == 1 and st.failed == 0
    pipeline.process()

    hits = db.search(
        conn,
        HashingEmbedder().embed_query("export controls foundry Taiwan"),
        model="hashing-384",
        chunker=pipeline.chunker.name,
        k=3,
        ticker="NVDA",
    )
    assert hits[0].section and hits[0].section.startswith("Item 1A")
    assert hits[0].metadata["form_type"] in {"8-K", "10-Q", "10-K"}


def test_pdf_inbox_and_parse_failure_is_isolated(conn, pipeline, tmp_path):
    data_dir = tmp_path / "data"
    inbox = data_dir / "inbox"
    inbox.mkdir(parents=True)
    (inbox / "duration_note.pdf").write_bytes(
        _make_pdf(
            ["Duration risk dominates the portfolio.", "Spreads tightened.", "Curve steepened."]
        )
    )
    (inbox / "broken.pdf").write_bytes(b"this is not a pdf")

    cfg = PdfInboxSourceConfig(id="pdf")
    st = pipeline.acquire(PdfInboxConnector(cfg, data_dir), cfg)
    assert st.new == 2
    p = pipeline.process()
    assert (p.processed, p.failed) == (1, 1)

    failed = db.failed_documents(conn)
    assert len(failed) == 1 and failed[0]["title"] == "broken"
    page = conn.execute(
        "SELECT page FROM chunks c JOIN documents d ON d.id=c.document_id "
        "WHERE d.title='duration note' ORDER BY ordinal LIMIT 1"
    ).fetchone()
    assert page["page"] == 1
    assert (inbox / "processed" / "duration_note.pdf").exists()

    assert db.reset_failed(conn) == 1
