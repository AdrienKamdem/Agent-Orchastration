"""SEC EDGAR connector: annual/quarterly/current reports for the watchlist.

How EDGAR is organised:

* ``company_tickers.json`` maps tickers to CIKs (Central Index Key, the SEC's company ID).
* ``data.sec.gov/submissions/CIK##########.json`` lists a company's recent filings as
  parallel arrays (``form[i]``, ``accessionNumber[i]``, ``filingDate[i]``...).
* Each filing is a folder ``Archives/edgar/data/{cik}/{accession without dashes}/`` with a
  primary document plus exhibits. ``{accession}-index.htm`` is a table listing every file
  and its type (``EX-99.1``...).

Rules we follow (https://www.sec.gov/os/accessing-edgar-data):
* A User-Agent identifying you, with an email address. Requests without one get blocked.
* At most 10 requests per second. The pipeline configures 8/s for SEC hosts.

US companies file 10-K / 10-Q / 8-K. Foreign private issuers (on your watchlist: ASML, TSM,
TotalEnergies) file 20-F (annual) and 6-K (everything else) instead, so both sets are
in the default form list.
"""

from __future__ import annotations

import json
import time
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import structlog
from lxml import html as lxml_html

from ..config import EdgarSourceConfig, WatchlistEntry
from ..http import HttpClient
from ..models import DiscoveredItem, RawDocument
from .base import Cursor

log = structlog.get_logger(__name__)

TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik:010d}.json"
ARCHIVE_URL = "https://www.sec.gov/Archives/edgar/data/{cik}/{acc}/{name}"
INDEX_URL = "https://www.sec.gov/Archives/edgar/data/{cik}/{acc}/{accession}-index.htm"

SEC_HOSTS = ("www.sec.gov", "data.sec.gov")
TICKER_CACHE_MAX_AGE_S = 7 * 24 * 3600


class EdgarConfigError(RuntimeError):
    pass


class EdgarConnector:
    def __init__(
        self,
        config: EdgarSourceConfig,
        http: HttpClient,
        watchlist: list[WatchlistEntry],
        user_agent: str | None,
        cache_dir: Path,
        today: date | None = None,
    ):
        if not user_agent or "@" not in user_agent:
            raise EdgarConfigError(
                "SEC_USER_AGENT must be set to something like 'Your Name you@example.com' "
                "(SEC fair-access policy). Put it in your .env file."
            )
        self.config = config
        self.source_id = config.id
        self.http = http
        self.watchlist = watchlist
        self.user_agent = user_agent
        self.cache_dir = cache_dir
        self._today = today

    # ------------------------------------------------------------------ helpers

    def _get(self, url: str) -> bytes:
        return self.http.get(url, user_agent=self.user_agent).content

    def ticker_to_cik(self) -> dict[str, dict[str, Any]]:
        """Ticker -> {cik, title}. Cached on disk for a week: it's ~1 MB and rarely changes."""
        cache = self.cache_dir / "company_tickers.json"
        if cache.exists() and time.time() - cache.stat().st_mtime < TICKER_CACHE_MAX_AGE_S:
            raw = cache.read_bytes()
        else:
            raw = self._get(TICKERS_URL)
            cache.parent.mkdir(parents=True, exist_ok=True)
            cache.write_bytes(raw)
        data = json.loads(raw)
        return {
            row["ticker"].upper(): {"cik": int(row["cik_str"]), "title": row["title"]}
            for row in data.values()
        }

    def _exhibits(self, cik: int, accession: str) -> list[tuple[str, str]]:
        """Return [(exhibit_type, filename)] for wanted exhibit types in a filing."""
        acc = accession.replace("-", "")
        page = self._get(INDEX_URL.format(cik=cik, acc=acc, accession=accession))
        tree = lxml_html.fromstring(page)
        wanted = {t.upper() for t in self.config.exhibit_types}
        found: list[tuple[str, str]] = []
        for row in tree.xpath("//table[contains(@class,'tableFile')]//tr[td]"):
            cells = row.xpath("./td")
            if len(cells) < 4:
                continue
            ex_type = cells[3].text_content().strip().upper()
            href = cells[2].xpath(".//a/@href")
            if ex_type in wanted and href:
                # hrefs look like /Archives/... or /ix?doc=/Archives/... (inline XBRL viewer)
                name = href[0].split("/")[-1]
                found.append((ex_type, name))
        return found

    # ------------------------------------------------------------------ contract

    def discover(self, cursor: Cursor | None) -> tuple[list[DiscoveredItem], Cursor]:
        cursor = dict(cursor or {})
        today = self._today or datetime.now(UTC).date()
        floor = today - timedelta(days=self.config.lookback_days)
        forms = {f.upper() for f in self.config.forms}
        exhibit_forms = {f.upper() for f in self.config.exhibit_forms}
        ciks = self.ticker_to_cik()
        items: list[DiscoveredItem] = []

        for entry in self.watchlist:
            info = ciks.get(entry.ticker)
            if info is None:
                log.warning("edgar.unknown_ticker", ticker=entry.ticker)
                continue
            cik = info["cik"]
            sub = json.loads(self._get(SUBMISSIONS_URL.format(cik=cik)))
            recent = sub["filings"]["recent"]

            since = floor
            if last := cursor.get(entry.ticker):
                # >= on the last date seen: same-day filings we haven't stored yet still
                # get picked up; ones we have are skipped by dedupe.
                since = max(since, date.fromisoformat(last))

            picked = 0
            newest = cursor.get(entry.ticker)
            for i, form in enumerate(recent["form"]):  # newest first
                if picked >= self.config.max_filings_per_ticker:
                    break
                filed = date.fromisoformat(recent["filingDate"][i])
                if filed < since:
                    break
                if form.upper() not in forms:
                    continue
                picked += 1
                accession = recent["accessionNumber"][i]
                acc = accession.replace("-", "")
                primary = recent["primaryDocument"][i]
                published = datetime(filed.year, filed.month, filed.day, tzinfo=UTC)
                meta = {
                    "ticker": entry.ticker,
                    "company": sub.get("name") or entry.name,
                    "cik": cik,
                    "form_type": form,
                    "accession_no": accession,
                    "filing_date": filed.isoformat(),
                    "report_date": recent.get("reportDate", [None] * (i + 1))[i] or None,
                    "parser": "edgar_filing",
                }
                title = f"{entry.ticker} {form} filed {filed.isoformat()}"
                items.append(
                    DiscoveredItem(
                        external_id=accession,
                        url=ARCHIVE_URL.format(cik=cik, acc=acc, name=primary),
                        title=title,
                        published_at=published,
                        metadata={**meta, "document": "primary"},
                    )
                )
                if form.upper() in exhibit_forms:
                    for ex_type, name in self._exhibits(cik, accession):
                        items.append(
                            DiscoveredItem(
                                external_id=f"{accession}/{name}",
                                url=ARCHIVE_URL.format(cik=cik, acc=acc, name=name),
                                title=f"{title} ({ex_type})",
                                published_at=published,
                                metadata={**meta, "document": ex_type},
                            )
                        )
                if newest is None or filed.isoformat() > newest:
                    newest = filed.isoformat()

            if newest:
                cursor[entry.ticker] = newest
            log.info("edgar.discovered", ticker=entry.ticker, cik=cik, filings=picked)

        return items, cursor

    def fetch(self, item: DiscoveredItem) -> RawDocument:
        assert item.url
        resp = self.http.get(item.url, user_agent=self.user_agent)
        mime = resp.headers.get("Content-Type", "text/html")
        if item.url.lower().endswith(".pdf"):
            mime = "application/pdf"
        meta = dict(item.metadata)
        if mime.startswith("application/pdf"):
            meta.pop("parser", None)  # let the PDF parser take it
        return RawDocument(
            source_id=self.source_id,
            external_id=item.external_id,
            url=item.url,
            title=item.title,
            published_at=item.published_at,
            content=resp.content,
            mime_type=mime,
            headers=dict(resp.headers),
            metadata=meta,
        )

    def acknowledge(self, item: DiscoveredItem) -> None:
        return None
