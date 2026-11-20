import itertools

import pytest

from copilot.ingestion.chunking import (
    RecursiveChunker,
    SectionAwareChunker,
    count_tokens,
    make_chunker,
)
from copilot.ingestion.models import ParsedDocument, Span

PARA = (
    "The Committee decided to maintain the target range for the federal funds rate. "
    "Inflation remains somewhat elevated. The labour market has cooled but remains solid. "
)
TEXT = "\n\n".join(f"Paragraph {i}. " + PARA * (i % 4 + 1) for i in range(30))


@pytest.mark.parametrize("max_tokens,overlap", [(50, 10), (120, 20), (400, 50)])
def test_offsets_slice_back_exactly(max_tokens, overlap):
    chunks = RecursiveChunker(max_tokens, overlap).chunk(TEXT)
    assert chunks
    for c in chunks:
        assert TEXT[c.char_start : c.char_end] == c.text
        assert c.text.strip() == c.text and c.text
        assert c.token_count <= max_tokens


def test_covers_whole_text_in_order():
    chunks = RecursiveChunker(60, 10).chunk(TEXT)
    assert chunks[0].char_start == 0
    assert chunks[-1].char_end == len(TEXT.rstrip())
    starts = [c.char_start for c in chunks]
    assert starts == sorted(starts)
    # nothing but whitespace is ever dropped between consecutive chunks
    for a, b in itertools.pairwise(chunks):
        assert TEXT[a.char_end : b.char_start].strip() == ""


def test_overlap_repeats_text_between_neighbours():
    chunks = RecursiveChunker(60, 25).chunk(TEXT)
    overlapping = sum(1 for a, b in itertools.pairwise(chunks) if b.char_start < a.char_end)
    assert overlapping >= len(chunks) // 2


def test_giant_word_is_hard_split():
    text = " ".join(["x"] * 1000)
    chunks = RecursiveChunker(100, 10).chunk(text)
    assert all(c.token_count <= 100 for c in chunks)


def test_short_text_is_one_chunk():
    chunks = RecursiveChunker(400, 50).chunk("Short statement.")
    assert len(chunks) == 1 and chunks[0].text == "Short statement."


def test_overlap_must_be_smaller_than_max():
    with pytest.raises(ValueError):
        RecursiveChunker(50, 50)


def test_section_aware_never_crosses_sections():
    a = "Item 1A. Risk Factors\n\n" + PARA * 10
    b = "\n\nItem 7. MD&A\n\n" + PARA * 10
    doc = ParsedDocument(
        text=a + b,
        parser_version="t",
        sections=[
            Span("Item 1A. Risk Factors", 0, len(a)),
            Span("Item 7. MD&A", len(a), len(a + b)),
        ],
    )
    chunks = SectionAwareChunker(60, 10).chunk_document(doc)
    for c in chunks:
        sec = next(s for s in doc.sections if s.label == c.section)
        assert sec.start <= c.char_start and c.char_end <= sec.end
    assert {c.section for c in chunks} == {"Item 1A. Risk Factors", "Item 7. MD&A"}
    assert [c.ordinal for c in chunks] == list(range(len(chunks)))


def test_pages_are_attached():
    p1, p2 = PARA * 3, PARA * 3
    text = p1 + "\n\n" + p2
    doc = ParsedDocument(
        text=text,
        parser_version="t",
        pages=[Span("1", 0, len(p1)), Span("2", len(p1) + 2, len(text))],
    )
    chunks = make_chunker("recursive", 30, 5).chunk_document(doc)
    assert chunks[0].page == 1 and chunks[-1].page == 2


def test_count_tokens():
    assert count_tokens("Rates rose 0.25%.") == 7  # Rates rose 0 . 25 % .
