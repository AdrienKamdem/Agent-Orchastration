"""The connector contract.

A connector knows how to talk to one kind of external source, and nothing else: no
database, no parsing, no embeddings. That keeps each one small and easy to test with
recorded HTTP responses.

``discover(cursor)`` returns what's available plus an updated cursor. The cursor is
opaque JSON the connector owns (last filing date per ticker, the ETag of a feed...). The
pipeline persists it, and only does so when the run had no fetch failures, so a failure
is retried next run rather than skipped forever.

``fetch(item)`` returns the exact bytes. Connectors whose discovery already produced the
content (RSS summaries, local files) just wrap ``item.inline_content``.
"""

from __future__ import annotations

from typing import Any, Protocol

from ..models import DiscoveredItem, RawDocument

Cursor = dict[str, Any]


class Connector(Protocol):
    source_id: str

    def discover(self, cursor: Cursor | None) -> tuple[list[DiscoveredItem], Cursor]: ...

    def fetch(self, item: DiscoveredItem) -> RawDocument: ...

    def acknowledge(self, item: DiscoveredItem) -> None:
        """Called once the raw bytes are safely stored (e.g. move a file out of an inbox)."""
        ...


def raw_from_inline(source_id: str, item: DiscoveredItem) -> RawDocument:
    assert item.inline_content is not None and item.inline_mime is not None
    return RawDocument(
        source_id=source_id,
        external_id=item.external_id,
        url=item.url,
        title=item.title,
        published_at=item.published_at,
        content=item.inline_content,
        mime_type=item.inline_mime,
        metadata=dict(item.metadata),
    )
