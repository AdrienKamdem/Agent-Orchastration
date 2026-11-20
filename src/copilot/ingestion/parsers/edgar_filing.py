"""SEC filings (10-K, 10-Q, 8-K, 20-F, 6-K and exhibits), usually inline XBRL HTML.

Two jobs:

1. **Clean the HTML.** Inline XBRL embeds a hidden ``<ix:header>`` full of machine-readable
   facts, plus ``display:none`` blocks. Both are noise for retrieval, so we drop them.
2. **Find the sections.** Filings are organised as "Item 1A. Risk Factors",
   "Item 7. Management's Discussion...", "Item 5. Operating and Financial Review" (20-F).
   The catch: every item heading appears twice, once in the table of contents and once
   for real. Heuristic: for each item number, keep the occurrence followed by the most
   text before the next heading. TOC entries are followed by almost nothing, so the real
   section wins.
"""

from __future__ import annotations

import re

from ..models import ParsedDocument, RawDocument, Span
from .base import decode, html_to_text

# "Item 1A. Risk Factors" / "ITEM 7 - MANAGEMENT'S..." / "Item 2.02 Results of Operations"
_ITEM_RE = re.compile(
    r"^item\s+(?P<num>\d{1,2}[a-c]?(?:\.\d{2})?)\s*[\.:\-–—]?\s*(?P<title>.{0,150})$",
    re.IGNORECASE | re.MULTILINE,
)

_DROP = (
    # The HTML parser keeps "ix:header" as the literal tag name; an XML parser would not.
    "//*[name()='ix:header' or (local-name()='header' and starts-with(name(), 'ix:'))]",
    "//*[contains(translate(@style,' ',''),'display:none')]",
)


def find_sections(text: str) -> list[Span]:
    candidates = []
    for m in _ITEM_RE.finditer(text):
        title = m.group("title").strip(" .|")
        # Cross-references ("see Item 7 for...") aren't headings: headings are short lines.
        if len(title) > 120 or title.lower().startswith(("of ", "and ", "in ", "above")):
            continue
        num = m.group("num").upper()
        label = f"Item {num}. {title}" if title else f"Item {num}"
        candidates.append((m.start(), num, label))
    if not candidates:
        return []

    # Length of text between each candidate and the next one.
    lengths = [
        (candidates[i + 1][0] if i + 1 < len(candidates) else len(text)) - candidates[i][0]
        for i in range(len(candidates))
    ]
    best: dict[str, tuple[int, int, str]] = {}  # num -> (length, start, label)
    for (start, num, label), length in zip(candidates, lengths, strict=True):
        if num not in best or length > best[num][0]:
            best[num] = (length, start, label)

    chosen = sorted((start, label) for _, start, label in best.values())
    sections = []
    for i, (start, label) in enumerate(chosen):
        end = chosen[i + 1][0] if i + 1 < len(chosen) else len(text)
        sections.append(Span(label=label, start=start, end=end))
    return sections


class EdgarFilingParser:
    name = "edgar_filing"
    version = "edgar_filing_v1"

    def parse(self, raw: RawDocument) -> ParsedDocument:
        html = decode(raw.content, raw.mime_type)
        text = html_to_text(html, drop_xpaths=_DROP)
        return ParsedDocument(
            text=text,
            parser_version=self.version,
            title=raw.title,
            sections=find_sections(text),
        )
