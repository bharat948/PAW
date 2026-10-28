import pytest

from paw_server.modules.ingestion.chunking import chunk_segments, split_spans
from paw_server.modules.ingestion.errors import UnsupportedDocument
from paw_server.modules.ingestion.models import Format
from paw_server.modules.ingestion.parsers import parse
from tests import fixtures as fx

KW = {"chunk_size": 1000, "max_rows": 1000, "max_pages": 100}


def run(data: bytes, fmt: Format, size: int = 1000, overlap: int = 100):
    return chunk_segments(parse(data, fmt, **{**KW, "chunk_size": size}), size, overlap)


def test_chunking_is_deterministic_and_offsets_resolve():
    text = ("Sentence number one is here. " * 80).strip()
    a, b = split_spans(text, 300, 50), split_spans(text, 300, 50)
    assert a == b and len(a) > 3
    for s, e in a:
        assert e - s <= 300 and not text[s].isspace()
    assert a[1][0] < a[0][1]  # overlap


def test_markdown_sections_and_offsets():
    chunks = run(fx.MD_DOC, Format.MD)
    assert [c.section for c in chunks] == ["Guide", "Guide > Install"]
    src = fx.MD_DOC.decode()
    for c in chunks:
        assert src[c.source_start : c.source_end] == c.text


def test_markdown_ignores_headings_in_code_fences():
    chunks = run(b"# A\ntext\n```\n# not a heading\n```\n", Format.MD)
    assert len(chunks) == 1 and chunks[0].section == "A"


def test_text_unicode_and_blank():
    assert run("héllo wörld 日本語".encode(), Format.TXT)[0].text == "héllo wörld 日本語"
    with pytest.raises(UnsupportedDocument) as e:
        run(b"  \n \n", Format.TXT)
    assert e.value.code == "empty_document"


def test_pdf_pages_preserved():
    chunks = run(
        fx.text_pdf(["First page about apples and oranges", "Second page about bananas"]),
        Format.PDF,
    )
    assert [c.page_number for c in chunks] == [1, 2]
    assert "bananas" in chunks[1].text


def test_image_only_pdf_is_unsupported():
    with pytest.raises(UnsupportedDocument) as e:
        run(fx.image_only_pdf(), Format.PDF)
    assert e.value.code == "no_extractable_text"


def test_csv_rows_become_atomic_chunks_with_raw_records():
    chunks = run(fx.CSV_DOC, Format.CSV)
    assert len(chunks) == 1
    c = chunks[0]
    assert "city: London" in c.text and c.metadata["columns"] == ["name", "city", "note"]
    assert c.metadata["rows"][1]["name"] == "Linus"
    assert (c.source_start, c.source_end, c.metadata["source_unit"]) == (1, 2, "rows")


def test_csv_groups_rows_by_size():
    data = b"k,v\n" + b"".join(f"{i},value number {i}\n".encode() for i in range(200))
    chunks = run(data, Format.CSV, size=300)
    assert len(chunks) > 5
    ranges = [(c.source_start, c.source_end) for c in chunks]
    assert ranges[0][0] == 1 and ranges[-1][1] == 200
    assert all(b[0] == a[1] + 1 for a, b in zip(ranges, ranges[1:], strict=False))


def test_json_and_jsonl_flatten_nested():
    c = run(fx.JSON_DOC, Format.JSON)[0]
    assert "dims.w: 2" in c.text and "desc: blue gadget" in c.text
    assert len(run(fx.JSONL_DOC, Format.JSONL)[0].metadata["rows"]) == 2


def test_header_only_csv_unsupported():
    with pytest.raises(UnsupportedDocument):
        run(b"a,b\n", Format.CSV)
