"""PDF parser (PyMuPDF).

PDFs have no notion of paragraphs: just positioned text. What we do:

* Read text **blocks** (PyMuPDF groups nearby lines), joining a block's lines into one
  paragraph. Blocks are returned in reading order with ``sort=True``.
* **Repeated headers/footers**: a block at the top or bottom of a page that appears (digits
  ignored, so "Page 3" == "Page 4") on at least half of the pages is boilerplate, so drop it.
* **Hyphenation**: "mone-\\ntary" becomes "monetary".
* Record a **page span** for every page, so chunks can cite page numbers.

Known limits, worth evaluating later against ``docling`` / ``unstructured``: complex
multi-column layouts, tables, scanned PDFs (no text layer, so this needs OCR).
"""

from __future__ import annotations

import re
from collections import Counter

import pymupdf

from ..models import ParsedDocument, RawDocument, Span


def _norm(block: str) -> str:
    return re.sub(r"\d+", "#", " ".join(block.lower().split()))


def _clean_block(block: str) -> str:
    block = re.sub(r"(\w)-\n(\w)", r"\1\2", block)
    return " ".join(block.split())


class PdfParser:
    name = "pdf"
    version = "pdf_v1"

    def parse(self, raw: RawDocument) -> ParsedDocument:
        with pymupdf.open(stream=raw.content, filetype="pdf") as doc:
            pages: list[list[str]] = []
            for page in doc:
                blocks = page.get_text("blocks", sort=True)
                # block = (x0, y0, x1, y1, text, block_no, block_type); type 0 = text
                pages.append([b[4] for b in blocks if b[6] == 0 and b[4].strip()])
            meta_title = (doc.metadata or {}).get("title") or None

        boilerplate: set[str] = set()
        if len(pages) >= 3:
            edge_counts: Counter[str] = Counter()
            for blocks in pages:
                edges = {_norm(b) for b in blocks[:2] + blocks[-2:]}
                edge_counts.update(edges)
            boilerplate = {k for k, n in edge_counts.items() if n >= len(pages) / 2}

        parts: list[str] = []
        spans: list[Span] = []
        offset = 0
        for i, blocks in enumerate(pages, start=1):
            kept = [_clean_block(b) for b in blocks if _norm(b) not in boilerplate]
            page_text = "\n\n".join(k for k in kept if k)
            if parts:
                parts.append("\n\n")
                offset += 2
            spans.append(Span(label=str(i), start=offset, end=offset + len(page_text)))
            parts.append(page_text)
            offset += len(page_text)

        text = "".join(parts)
        return ParsedDocument(
            text=text,
            parser_version=self.version,
            title=raw.title or meta_title,
            pages=spans,
        )
