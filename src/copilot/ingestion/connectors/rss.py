"""Generic RSS/Atom connector: central bank press releases, news feeds, research feeds.

One class, many sources: everything source-specific lives in ``sources.yaml``.

Incremental strategy:
1. Conditional GET on the feed with the ETag / Last-Modified saved last run. A 304 means
   nothing new, at almost zero cost.
2. Items older than ``lookback_days`` are ignored (so the first run doesn't backfill years).
3. Items already in the DB are skipped by the pipeline (dedupe on ``external_id``).
"""

from __future__ import annotations

import calendar
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import parse_qsl, quote_plus, urlencode, urlsplit, urlunsplit

import feedparser
import structlog
from lxml import html as lxml_html

from ..config import RssSourceConfig, WatchlistEntry
from ..http import HttpClient
from ..models import DiscoveredItem, RawDocument
from .base import Cursor, raw_from_inline

log = structlog.get_logger(__name__)

_TRACKING_PARAMS = ("utm_", "fbclid", "gclid", "mc_cid", "mc_eid")


def normalize_url(url: str) -> str:
    """Drop tracking parameters and fragments so the same article dedupes to one key."""
    parts = urlsplit(url.strip())
    query = [
        (k, v)
        for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if not k.lower().startswith(_TRACKING_PARAMS)
    ]
    return urlunsplit(
        (parts.scheme.lower(), parts.netloc.lower(), parts.path, urlencode(query), "")
    )


def _entry_datetime(entry: Any) -> datetime | None:
    for key in ("published_parsed", "updated_parsed"):
        t = entry.get(key)
        if t:
            return datetime.fromtimestamp(calendar.timegm(t), tz=UTC)
    return None


def _html_to_text(fragment: str) -> str:
    if not fragment.strip():
        return ""
    try:
        return " ".join(lxml_html.fromstring(fragment).text_content().split())
    except Exception:
        return " ".join(fragment.split())


class RssConnector:
    def __init__(
        self,
        config: RssSourceConfig,
        http: HttpClient,
        watchlist: list[WatchlistEntry] | None = None,
        now: datetime | None = None,
    ):
        self.config = config
        self.source_id = config.id
        self.http = http
        self.watchlist = watchlist or []
        self._now = now

    def _feeds(self) -> list[tuple[str, dict[str, Any]]]:
        if not self.config.per_watchlist:
            return [(self.config.url, {})]
        return [
            (
                self.config.url.format(ticker=quote_plus(w.ticker), name=quote_plus(w.name)),
                {"ticker": w.ticker, "company": w.name},
            )
            for w in self.watchlist
        ]

    def discover(self, cursor: Cursor | None) -> tuple[list[DiscoveredItem], Cursor]:
        cursor = dict(cursor or {})
        now = self._now or datetime.now(UTC)
        cutoff = now - timedelta(days=self.config.lookback_days)
        items: list[DiscoveredItem] = []
        seen: set[str] = set()

        for feed_url, extra_meta in self._feeds():
            state = cursor.get(feed_url, {})
            resp = self.http.get(
                feed_url, etag=state.get("etag"), last_modified=state.get("last_modified")
            )
            if resp.status_code == 304:
                log.info("rss.not_modified", source=self.source_id, feed=feed_url)
                continue

            parsed = feedparser.parse(resp.content)
            if parsed.bozo and not parsed.entries:
                raise ValueError(f"unparseable feed {feed_url}: {parsed.bozo_exception}")

            for entry in parsed.entries[: self.config.max_items]:
                link = entry.get("link")
                external_id = entry.get("id") or (normalize_url(link) if link else None)
                if not external_id or external_id in seen:
                    continue
                published = _entry_datetime(entry)
                if published and published < cutoff:
                    continue
                seen.add(external_id)

                title = (entry.get("title") or "").strip() or None
                meta = {"feed_url": feed_url, **extra_meta}
                if self.config.fetch_full_page:
                    items.append(
                        DiscoveredItem(
                            external_id=external_id,
                            url=link,
                            title=title,
                            published_at=published,
                            metadata=meta,
                        )
                    )
                else:
                    summary = _html_to_text(entry.get("summary", ""))
                    body = f"{title or ''}\n\n{summary}".strip()
                    items.append(
                        DiscoveredItem(
                            external_id=external_id,
                            url=link,
                            title=title,
                            published_at=published,
                            metadata={**meta, "parser": "plain_text"},
                            inline_content=body.encode(),
                            inline_mime="text/plain",
                        )
                    )

            cursor[feed_url] = {
                "etag": resp.headers.get("ETag"),
                "last_modified": resp.headers.get("Last-Modified"),
            }

        return items, cursor

    def fetch(self, item: DiscoveredItem) -> RawDocument:
        if item.inline_content is not None:
            return raw_from_inline(self.source_id, item)
        if not item.url:
            raise ValueError(f"item {item.external_id} has no URL to fetch")
        resp = self.http.get(item.url)
        return RawDocument(
            source_id=self.source_id,
            external_id=item.external_id,
            url=str(resp.url),
            title=item.title,
            published_at=item.published_at,
            content=resp.content,
            mime_type=resp.headers.get("Content-Type", "text/html"),
            headers=dict(resp.headers),
            metadata=dict(item.metadata),
        )

    def acknowledge(self, item: DiscoveredItem) -> None:
        return None
