from __future__ import annotations

from typing import Protocol

from ..models import Chunk, ParsedDocument
from .recursive import RecursiveChunker, count_tokens
from .section_aware import SectionAwareChunker


class DocumentChunker(Protocol):
    @property
    def name(self) -> str: ...

    def chunk_document(self, doc: ParsedDocument) -> list[Chunk]: ...


class _RecursiveDocumentChunker:
    """Plain recursive chunking that ignores sections (the baseline for evaluation)."""

    def __init__(self, max_tokens: int, overlap_tokens: int):
        self._inner = RecursiveChunker(max_tokens, overlap_tokens)

    @property
    def name(self) -> str:
        return self._inner.name

    def chunk_document(self, doc: ParsedDocument) -> list[Chunk]:
        return [
            Chunk(
                ordinal=c.ordinal,
                text=c.text,
                char_start=c.char_start,
                char_end=c.char_end,
                token_count=c.token_count,
                page=doc.page_at(c.char_start),
            )
            for c in self._inner.chunk(doc.text)
        ]


def make_chunker(kind: str, max_tokens: int, overlap_tokens: int) -> DocumentChunker:
    if kind == "section_aware":
        return SectionAwareChunker(max_tokens, overlap_tokens)
    if kind == "recursive":
        return _RecursiveDocumentChunker(max_tokens, overlap_tokens)
    raise ValueError(f"unknown chunker {kind!r}")


__all__ = [
    "DocumentChunker",
    "RecursiveChunker",
    "SectionAwareChunker",
    "count_tokens",
    "make_chunker",
]
