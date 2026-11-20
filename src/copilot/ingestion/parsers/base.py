"""Parser contract, registry, and shared text helpers.

A parser turns raw bytes into a ``ParsedDocument``: clean text plus structure (sections,
pages). Every parser has a ``version``. Bump it when you change its output, then run
``ingest reindex`` to rebuild documents parsed by the old one.
"""

from __future__ import annotations

import re
from typing import Protocol

from lxml import html as lxml_html

from ..models import ParsedDocument, RawDocument


class Parser(Protocol):
    name: str
    version: str

    def parse(self, raw: RawDocument) -> ParsedDocument: ...


def decode(content: bytes, mime_type: str = "") -> str:
    m = re.search(r"charset=([\w-]+)", mime_type, re.I)
    for enc in ([m.group(1)] if m else []) + ["utf-8", "cp1252", "latin-1"]:
        try:
            return content.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return content.decode("utf-8", errors="replace")


def normalize_whitespace(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\xa0", " ")
    text = re.sub(r"[ \t\f\v]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


_BLOCK_TAGS = {
    "p",
    "div",
    "section",
    "article",
    "header",
    "footer",
    "li",
    "ul",
    "ol",
    "table",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "blockquote",
    "pre",
    "hr",
    "dt",
    "dd",
}


def html_to_text(html: str | bytes, drop_xpaths: tuple[str, ...] = ()) -> str:
    """Structure-preserving HTML -> text.

    ``text_content()`` on its own glues every block together. Instead, blocks become
    paragraph breaks, table rows become lines, and cells are separated by `` | ``, so
    financial tables stay readable to both humans and embedding models.
    """
    if isinstance(html, bytes):
        html = decode(html)
    # lxml refuses str input that carries an XML encoding declaration (inline XBRL has one).
    html = re.sub(r"^\s*<\?xml[^>]*\?>", "", html)
    tree = lxml_html.fromstring(html)
    for xp in ("//script", "//style", "//noscript", "//head", *drop_xpaths):
        for el in tree.xpath(xp):
            el.drop_tree()
    for el in tree.iter():
        tag = el.tag if isinstance(el.tag, str) else ""
        tag = tag.split("}")[-1].lower()
        if tag == "br":
            el.tail = "\n" + (el.tail or "")
        elif tag in ("td", "th"):
            el.tail = " | " + (el.tail or "")
        elif tag == "tr":
            el.tail = "\n" + (el.tail or "")
        elif tag in _BLOCK_TAGS:
            el.tail = "\n\n" + (el.tail or "")
    text = tree.text_content()
    text = re.sub(r"(?: \| )+\n", "\n", text)  # trailing cell separators
    return normalize_whitespace(text)
