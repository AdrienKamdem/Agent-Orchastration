from datetime import UTC, datetime

import pymupdf

from copilot.ingestion.models import RawDocument
from copilot.ingestion.parsers import parser_for
from copilot.ingestion.parsers.edgar_filing import EdgarFilingParser, find_sections
from copilot.ingestion.parsers.html_article import HtmlArticleParser
from copilot.ingestion.parsers.pdf import PdfParser
from tests.conftest import fixture_bytes


def _raw(content: bytes, mime: str, **meta) -> RawDocument:
    return RawDocument(
        "s", "e", None, None, datetime(2026, 1, 1, tzinfo=UTC), content, mime, metadata=meta
    )


def test_html_article_extracts_main_text_without_boilerplate():
    doc = HtmlArticleParser().parse(_raw(fixture_bytes("fed_statement.html"), "text/html"))
    assert "maintain the target range" in doc.text
    assert "balance sheet runoff" in doc.text
    assert "do not index me" not in doc.text
    assert "News & Events" not in doc.text
    assert "\n\n" in doc.text  # paragraphs preserved for the chunker


def test_edgar_parser_drops_xbrl_and_finds_real_sections():
    doc = EdgarFilingParser().parse(_raw(fixture_bytes("edgar_10k.htm"), "text/html"))
    assert "HIDDEN-XBRL-FACT" not in doc.text
    labels = [s.label for s in doc.sections]
    assert labels[0].startswith("Item 1.") and "Business" in labels[0]
    assert any(lbl.startswith("Item 1A.") and "Risk Factors" in lbl for lbl in labels)
    assert any(lbl.startswith("Item 7.") for lbl in labels)
    assert len(labels) == 3  # TOC entries were not chosen as sections

    risk = next(s for s in doc.sections if s.label.startswith("Item 1A"))
    body = doc.text[risk.start : risk.end]
    assert "Export controls" in body and "Revenue for fiscal year" not in body
    # tables flattened to readable rows
    assert "Compute & Networking | 100.0 | 80.0" in doc.text


def test_cross_reference_is_not_a_heading():
    text = "Item 1A. Risk Factors\nsome risks\nSee Item 7 for details.\nItem 7. MD&A\nbody"
    labels = [s.label for s in find_sections(text)]
    assert labels == ["Item 1A. Risk Factors", "Item 7. MD&A"]


def _make_pdf(pages: list[str]) -> bytes:
    doc = pymupdf.open()
    for i, body in enumerate(pages, 1):
        page = doc.new_page()
        page.insert_text((72, 40), "ACME Research Quarterly")  # repeated header
        page.insert_textbox(pymupdf.Rect(72, 100, 520, 700), body, fontsize=11)
        page.insert_text((72, 800), f"Page {i}")  # repeated footer (digits differ)
    data = doc.tobytes()
    doc.close()
    return data


def test_pdf_parser_pages_headers_and_hyphenation():
    pdf = _make_pdf(
        [
            "Term premia rose sharply as mone-\ntary policy uncertainty increased.",
            "Credit spreads widened in high yield.",
            "Duration exposure is the main portfolio risk.",
        ]
    )
    doc = PdfParser().parse(_raw(pdf, "application/pdf"))
    assert "ACME Research Quarterly" not in doc.text
    assert "Page 2" not in doc.text
    assert "monetary policy" in doc.text
    assert [p.label for p in doc.pages] == ["1", "2", "3"]
    p2 = doc.pages[1]
    assert "Credit spreads" in doc.text[p2.start : p2.end]


def test_parser_selection():
    assert parser_for(_raw(b"", "application/pdf")).name == "pdf"
    assert parser_for(_raw(b"", "text/html; charset=utf-8")).name == "html_article"
    assert parser_for(_raw(b"", "text/html", parser="edgar_filing")).name == "edgar_filing"
    assert parser_for(_raw(b"", "text/plain")).name == "plain_text"
