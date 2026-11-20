"""Parser selection.

Order: an explicit hint from the connector (``metadata["parser"]``), then the MIME type.
"""

from __future__ import annotations

from ..models import ParsedDocument, RawDocument
from .base import Parser, decode, normalize_whitespace
from .edgar_filing import EdgarFilingParser
from .html_article import HtmlArticleParser
from .pdf import PdfParser


class PlainTextParser:
    name = "plain_text"
    version = "plain_text_v1"

    def parse(self, raw: RawDocument) -> ParsedDocument:
        return ParsedDocument(
            text=normalize_whitespace(decode(raw.content, raw.mime_type)),
            parser_version=self.version,
            title=raw.title,
        )


PARSERS: dict[str, Parser] = {
    p.name: p for p in (HtmlArticleParser(), EdgarFilingParser(), PdfParser(), PlainTextParser())
}


def parser_for(raw: RawDocument) -> Parser:
    hint = raw.metadata.get("parser")
    if hint:
        if hint not in PARSERS:
            raise ValueError(f"unknown parser hint {hint!r}")
        return PARSERS[hint]
    mime = raw.mime_type.split(";")[0].strip().lower()
    if mime == "application/pdf":
        return PARSERS["pdf"]
    if mime in ("text/html", "application/xhtml+xml"):
        return PARSERS["html_article"]
    if mime.startswith("text/"):
        return PARSERS["plain_text"]
    raise ValueError(f"no parser for MIME type {raw.mime_type!r}")


__all__ = ["PARSERS", "Parser", "PlainTextParser", "parser_for"]
