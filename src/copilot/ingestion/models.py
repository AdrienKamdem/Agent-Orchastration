"""The data that flows between pipeline steps.

    Connector.discover() -> DiscoveredItem      "this exists, here's where"
    Connector.fetch()    -> RawDocument         "here are the exact bytes"
    Parser.parse()       -> ParsedDocument      "here is clean text + structure"
    Chunker.chunk()      -> list[Chunk]         "here are retrieval units with offsets"

All offsets (``start``/``end``, ``char_start``/``char_end``) index into
``ParsedDocument.text``, so ``text[c.char_start:c.char_end] == c.text`` holds for every
chunk. That invariant is what makes precise citations possible later.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any


@dataclass(frozen=True)
class DiscoveredItem:
    external_id: str  # stable natural key within a source (accession no., RSS guid, sha256)
    url: str | None
    title: str | None = None
    published_at: datetime | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    # Some sources hand us the content during discovery (RSS summaries, local files),
    # so fetch() doesn't need the network.
    inline_content: bytes | None = None
    inline_mime: str | None = None


@dataclass
class RawDocument:
    source_id: str
    external_id: str
    url: str | None
    title: str | None
    published_at: datetime | None
    content: bytes
    mime_type: str
    headers: dict[str, str] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Span:
    """A labelled [start, end) range in the parsed text: a section or a page."""

    label: str
    start: int
    end: int


@dataclass
class ParsedDocument:
    text: str
    parser_version: str
    title: str | None = None
    sections: list[Span] = field(default_factory=list)  # label = section heading
    pages: list[Span] = field(default_factory=list)  # label = page number as str

    def page_at(self, offset: int) -> int | None:
        for p in self.pages:
            if p.start <= offset < p.end:
                return int(p.label)
        return None

    def to_json(self) -> str:
        return json.dumps(asdict(self))

    @classmethod
    def from_json(cls, data: str | bytes) -> ParsedDocument:
        d = json.loads(data)
        return cls(
            text=d["text"],
            parser_version=d["parser_version"],
            title=d.get("title"),
            sections=[Span(**s) for s in d.get("sections", [])],
            pages=[Span(**p) for p in d.get("pages", [])],
        )


@dataclass(frozen=True)
class Chunk:
    ordinal: int
    text: str
    char_start: int
    char_end: int
    token_count: int
    section: str | None = None
    page: int | None = None
