"""Section-aware chunker: never lets a chunk cross a section boundary.

For a 10-K, a chunk that starts at the end of "Risk Factors" and runs into "Unresolved
Staff Comments" is bad for retrieval and gives a wrong citation. So each section is
chunked on its own with the recursive chunker, and every chunk records its section.
Text outside any section (cover page, preamble) is chunked as ``section=None``.

Documents without sections (news, most PDFs) come out the same as with the recursive chunker.
"""

from __future__ import annotations

from ..models import Chunk, ParsedDocument
from .recursive import RecursiveChunker, count_tokens


class SectionAwareChunker:
    def __init__(self, max_tokens: int = 400, overlap_tokens: int = 50):
        self._inner = RecursiveChunker(max_tokens, overlap_tokens)

    @property
    def name(self) -> str:
        return f"section_aware_v1:{self._inner.max_tokens}/{self._inner.overlap_tokens}"

    def chunk_document(self, doc: ParsedDocument) -> list[Chunk]:
        text = doc.text
        regions: list[tuple[int, int, str | None]] = []
        cursor = 0
        for sec in sorted(doc.sections, key=lambda s: s.start):
            if sec.start > cursor:
                regions.append((cursor, sec.start, None))
            regions.append((max(sec.start, cursor), sec.end, sec.label))
            cursor = max(cursor, sec.end)
        if cursor < len(text):
            regions.append((cursor, len(text), None))

        chunks: list[Chunk] = []
        for start, end, label in regions:
            for s, e in self._inner.spans(text, start, end):
                chunks.append(
                    Chunk(
                        ordinal=len(chunks),
                        text=text[s:e],
                        char_start=s,
                        char_end=e,
                        token_count=count_tokens(text[s:e]),
                        section=label,
                        page=doc.page_at(s),
                    )
                )
        return chunks
