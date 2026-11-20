import json
from datetime import UTC, date, datetime

import httpx
import pytest
import respx

from copilot.ingestion.config import (
    EdgarSourceConfig,
    PdfInboxSourceConfig,
    RssSourceConfig,
    WatchlistEntry,
)
from copilot.ingestion.connectors.edgar import EdgarConfigError, EdgarConnector
from copilot.ingestion.connectors.pdf_inbox import PdfInboxConnector
from copilot.ingestion.connectors.rss import RssConnector, normalize_url
from tests.conftest import fixture_bytes

NOW = datetime(2026, 11, 20, tzinfo=UTC)
FED_URL = "https://www.federalreserve.gov/feeds/press_monetary.xml"


def _fed(**kw) -> RssSourceConfig:
    return RssSourceConfig(id="fed", kind="cb_statement", url=FED_URL, **kw)


# ----------------------------------------------------------------------------- RSS


@respx.mock
def test_rss_discover_filters_old_items_and_saves_validators(http):
    respx.get(FED_URL).mock(
        return_value=httpx.Response(
            200,
            content=fixture_bytes("fed_feed.xml"),
            headers={"ETag": '"v1"', "Last-Modified": "Wed, 04 Nov 2026 19:00:00 GMT"},
        )
    )
    items, cursor = RssConnector(_fed(lookback_days=365), http, now=NOW).discover(None)
    assert [i.external_id for i in items] == ["monetary20261104a", "monetary20261014a"]
    assert items[0].published_at == datetime(2026, 11, 4, 19, tzinfo=UTC)
    assert cursor[FED_URL]["etag"] == '"v1"'


@respx.mock
def test_rss_304_means_no_items(http):
    route = respx.get(FED_URL).mock(return_value=httpx.Response(304))
    cursor = {FED_URL: {"etag": '"v1"', "last_modified": None}}
    items, new_cursor = RssConnector(_fed(), http, now=NOW).discover(cursor)
    assert items == []
    assert new_cursor == cursor
    assert route.calls.last.request.headers["If-None-Match"] == '"v1"'


@respx.mock
def test_rss_fetch_full_page(http):
    respx.get(FED_URL).mock(return_value=httpx.Response(200, content=fixture_bytes("fed_feed.xml")))
    page = respx.get(url__startswith="https://www.federalreserve.gov/newsevents/").mock(
        return_value=httpx.Response(
            200,
            content=fixture_bytes("fed_statement.html"),
            headers={"Content-Type": "text/html; charset=utf-8"},
        )
    )
    conn = RssConnector(_fed(), http, now=NOW)
    items, _ = conn.discover(None)
    raw = conn.fetch(items[0])
    assert page.called
    assert raw.mime_type.startswith("text/html")
    assert b"maintain the target range" in raw.content


@respx.mock
def test_rss_summary_only_per_watchlist(http):
    cfg = RssSourceConfig(
        id="news",
        kind="news",
        per_watchlist=True,
        fetch_full_page=False,
        url="https://news.example.com/rss?q={name}",
    )
    route = respx.get(url__startswith="https://news.example.com/rss").mock(
        return_value=httpx.Response(200, content=fixture_bytes("news_feed.xml"))
    )
    wl = [WatchlistEntry(ticker="nvda", name="NVIDIA")]
    conn = RssConnector(cfg, http, wl, now=NOW)
    items, _ = conn.discover(None)
    assert str(route.calls.last.request.url) == "https://news.example.com/rss?q=NVIDIA"
    assert len(items) == 1
    raw = conn.fetch(items[0])  # no network: content came with the feed
    assert raw.mime_type == "text/plain"
    assert raw.content.decode() == (
        "Chipmaker raises data center guidance (synthetic)\n\n"
        "The company said accelerator demand remained strong."
    )
    assert raw.metadata["ticker"] == "NVDA" and raw.metadata["parser"] == "plain_text"


def test_normalize_url_strips_tracking():
    assert normalize_url("HTTPS://Ex.com/a?utm_source=x&id=1#frag") == "https://ex.com/a?id=1"


# ----------------------------------------------------------------------------- EDGAR

UA = "Test Person test@example.com"


def _edgar(http, tmp_path, **kw) -> EdgarConnector:
    cfg = EdgarSourceConfig(id="edgar", **kw)
    wl = [WatchlistEntry(ticker="NVDA", name="NVIDIA"), WatchlistEntry(ticker="ZZZZ", name="?")]
    return EdgarConnector(cfg, http, wl, UA, tmp_path, today=date(2026, 11, 20))


def _mock_edgar():
    respx.get("https://www.sec.gov/files/company_tickers.json").mock(
        return_value=httpx.Response(200, content=fixture_bytes("company_tickers.json"))
    )
    respx.get("https://data.sec.gov/submissions/CIK0001045810.json").mock(
        return_value=httpx.Response(200, content=fixture_bytes("submissions_nvda.json"))
    )
    return respx.get(
        "https://www.sec.gov/Archives/edgar/data/1045810/000104581026000150/"
        "0001045810-26-000150-index.htm"
    ).mock(return_value=httpx.Response(200, content=fixture_bytes("edgar_8k_index.htm")))


def test_edgar_requires_user_agent(http, tmp_path):
    with pytest.raises(EdgarConfigError, match="SEC_USER_AGENT"):
        EdgarConnector(EdgarSourceConfig(id="e"), http, [], None, tmp_path)


@respx.mock
def test_edgar_discover_forms_exhibits_and_cursor(http, tmp_path):
    index = _mock_edgar()
    items, cursor = _edgar(http, tmp_path).discover(None)
    ids = [i.external_id for i in items]
    # Form 4 filtered out, 2024 10-K outside the 365-day lookback, unknown ticker skipped
    assert ids == [
        "0001045810-26-000150",
        "0001045810-26-000150/q2fy27pr.htm",
        "0001045810-26-000150/q2fy27cfo.htm",
        "0001045810-26-000100",
        "0001045810-26-000050",
    ]
    assert index.called
    primary = items[0]
    assert primary.url == (
        "https://www.sec.gov/Archives/edgar/data/1045810/000104581026000150/nvda-20260827.htm"
    )
    assert primary.metadata["form_type"] == "8-K" and primary.metadata["ticker"] == "NVDA"
    assert items[1].metadata["document"] == "EX-99.1"
    assert cursor == {"NVDA": "2026-08-27"}
    # every SEC request carried the contact User-Agent
    assert all(c.request.headers["User-Agent"] == UA for c in respx.calls)
    # ticker map is cached on disk
    assert json.loads((tmp_path / "company_tickers.json").read_text())["0"]["ticker"] == "NVDA"


@respx.mock
def test_edgar_cursor_limits_to_newer_filings(http, tmp_path):
    _mock_edgar()
    items, _ = _edgar(http, tmp_path).discover({"NVDA": "2026-08-27"})
    assert {i.metadata["accession_no"] for i in items} == {"0001045810-26-000150"}


# ----------------------------------------------------------------------------- PDF inbox


def test_pdf_inbox_discovers_and_moves_after_ack(tmp_path):
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    (inbox / "rates_outlook.pdf").write_bytes(b"%PDF-1.4 fake")
    (inbox / "notes.txt").write_text("ignored")
    conn = PdfInboxConnector(PdfInboxSourceConfig(id="pdf"), tmp_path)
    items, _ = conn.discover(None)
    assert len(items) == 1 and items[0].title == "rates outlook"
    assert conn.fetch(items[0]).mime_type == "application/pdf"
    conn.acknowledge(items[0])
    assert (inbox / "processed" / "rates_outlook.pdf").exists()
    assert not (inbox / "rates_outlook.pdf").exists()
