"""Recursive chunker that tracks offsets.

Algorithm:
1. **Split** the text into pieces small enough to fit in a chunk, trying separators from
   coarse to fine: paragraphs (blank line), then lines, then sentences, then words. A
   piece that's still too big after words gets split on token boundaries.
2. **Merge** neighbouring pieces greedily until adding the next one would exceed
   ``max_tokens``.
3. **Overlap**: each new chunk starts with the last ~``overlap_tokens`` of the previous
   one, so a sentence cut at a boundary still appears whole in one of the two chunks.

All of this works on ``(start, end)`` offsets into the original text and never on copies,
so ``text[c.char_start:c.char_end] == c.text`` is always true.

Token counting is an approximation (words + punctuation). Real subword tokenizers
produce roughly 1.2–1.4x more tokens for English. That's why the default ``max_tokens``
of 400 stays safely under bge-small's 512-token limit.
"""

from __future__ import annotations

import re

from ..models import Chunk

_TOKEN_RE = re.compile(r"\w+|[^\w\s]")
# (regex, keep_separator_with_left_piece)
_SEPARATORS = [r"\n\s*\n", r"\n", r"(?<=[.!?])\s+", r"\s+"]


def count_tokens(text: str) -> int:
    return sum(1 for _ in _TOKEN_RE.finditer(text))


class RecursiveChunker:
    def __init__(self, max_tokens: int = 400, overlap_tokens: int = 50):
        if overlap_tokens >= max_tokens:
            raise ValueError("overlap must be smaller than max_tokens")
        self.max_tokens = max_tokens
        self.overlap_tokens = overlap_tokens

    @property
    def name(self) -> str:
        return f"recursive_v1:{self.max_tokens}/{self.overlap_tokens}"

    # ------------------------------------------------------------------ split

    def _split(self, text: str, start: int, end: int, level: int = 0) -> list[tuple[int, int]]:
        if count_tokens(text[start:end]) <= self.max_tokens:
            return [(start, end)]
        if level >= len(_SEPARATORS):
            return self._hard_split(text, start, end)

        pieces: list[tuple[int, int]] = []
        cursor = start
        for m in re.finditer(_SEPARATORS[level], text[start:end]):
            sep_end = start + m.end()
            if sep_end > cursor:
                pieces.append((cursor, sep_end))  # separator stays with the left piece
                cursor = sep_end
        if cursor < end:
            pieces.append((cursor, end))
        if len(pieces) <= 1:
            return self._split(text, start, end, level + 1)

        out: list[tuple[int, int]] = []
        for s, e in pieces:
            out.extend(self._split(text, s, e, level + 1))
        return out

    def _hard_split(self, text: str, start: int, end: int) -> list[tuple[int, int]]:
        starts = [start + m.start() for m in _TOKEN_RE.finditer(text[start:end])]
        out = []
        for i in range(0, len(starts), self.max_tokens):
            s = start if i == 0 else starts[i]
            e = starts[i + self.max_tokens] if i + self.max_tokens < len(starts) else end
            out.append((s, e))
        return out

    # ------------------------------------------------------------------ merge

    def spans(self, text: str, start: int = 0, end: int | None = None) -> list[tuple[int, int]]:
        """Chunk ``text[start:end]``; returns stripped (start, end) offsets."""
        end = len(text) if end is None else end
        pieces = self._split(text, start, end)
        chunks: list[tuple[int, int]] = []
        i = 0
        while i < len(pieces):
            c_start = pieces[i][0]
            c_end = pieces[i][1]
            j = i + 1
            while j < len(pieces) and count_tokens(text[c_start : pieces[j][1]]) <= self.max_tokens:
                c_end = pieces[j][1]
                j += 1
            chunks.append((c_start, c_end))
            if j >= len(pieces):
                break
            # Overlap: step back over trailing pieces of this chunk while they fit in the
            # overlap budget, and still leave room for the next piece.
            k = j
            while (
                k - 1 > i
                and count_tokens(text[pieces[k - 1][0] : c_end]) <= self.overlap_tokens
                and count_tokens(text[pieces[k - 1][0] : pieces[j][1]]) <= self.max_tokens
            ):
                k -= 1
            i = k

        stripped = []
        for s, e in chunks:
            while s < e and text[s].isspace():
                s += 1
            while e > s and text[e - 1].isspace():
                e -= 1
            if s < e:
                stripped.append((s, e))
        return stripped

    def chunk(self, text: str) -> list[Chunk]:
        return [
            Chunk(
                ordinal=n,
                text=text[s:e],
                char_start=s,
                char_end=e,
                token_count=count_tokens(text[s:e]),
            )
            for n, (s, e) in enumerate(self.spans(text))
        ]
