"""Article-style web pages: central bank statements, press releases, news pages.

trafilatura finds the main content and drops navigation, cookie banners and footers.
If it finds nothing (unusual layouts), we fall back to the structure-preserving
``html_to_text`` on the whole page: more noise, but never empty.
"""

from __future__ import annotations

import trafilatura

from ..models import ParsedDocument, RawDocument
from .base import decode, html_to_text, normalize_whitespace


class HtmlArticleParser:
    name = "html_article"
    version = "html_article_v1"

    def parse(self, raw: RawDocument) -> ParsedDocument:
        html = decode(raw.content, raw.mime_type)
        text = trafilatura.extract(
            html,
            output_format="txt",
            include_tables=True,
            include_comments=False,
            favor_recall=True,
        )
        if not text or len(text) < 200:
            text = html_to_text(html)
        else:
            # trafilatura separates paragraphs with single newlines; the chunker
            # prefers blank lines as paragraph boundaries.
            text = normalize_whitespace(text.replace("\n", "\n\n"))

        title = raw.title
        if not title:
            meta = trafilatura.extract_metadata(html)
            title = meta.title if meta else None
        return ParsedDocument(text=text, parser_version=self.version, title=title)
