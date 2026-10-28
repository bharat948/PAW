"""Format-specific extraction into Segments. Unstructured: txt/md/pdf. Structured: csv/tsv/json/jsonl."""

import csv
import io
import json
import re
from collections.abc import Iterable
from typing import Any

from paw_server.modules.ingestion.errors import PermanentIngestError, UnsupportedDocument
from paw_server.modules.ingestion.models import Format, Segment

PARSER_VERSION = "parsers-1"
MIN_PDF_CHARS = 20
MAX_CELL_CHARS = 2000
_HEADING = re.compile(r"^(#{1,6})[ \t]+(.+?)[ \t#]*$")
_FENCE = re.compile(r"^\s*(```|~~~)")


def decode_text(data: bytes) -> str:
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise PermanentIngestError("invalid_encoding", "The file is not valid UTF-8.") from exc
    if "\x00" in text:
        raise UnsupportedDocument("binary_content", "The file contains binary data.")
    if not text.strip():
        raise UnsupportedDocument("empty_document", "The document contains no text.")
    return text


def parse_text(data: bytes) -> list[Segment]:
    return [Segment(text=decode_text(data))]


def parse_markdown(data: bytes) -> list[Segment]:
    """One segment per heading section, tagged with its heading path (e.g. 'Guide > Install')."""
    text = decode_text(data)
    segments: list[Segment] = []
    path: list[tuple[int, str]] = []
    start = 0
    in_fence = False
    pos = 0

    def flush(end: int) -> None:
        body = text[start:end]
        if body.strip():
            section = " > ".join(t for _, t in path) or None
            segments.append(Segment(text=body, offset=start, section=section))

    for line in text.splitlines(keepends=True):
        if _FENCE.match(line):
            in_fence = not in_fence
        m = None if in_fence else _HEADING.match(line.rstrip("\r\n"))
        if m:
            flush(pos)
            level, title = len(m.group(1)), m.group(2).strip()
            while path and path[-1][0] >= level:
                path.pop()
            path.append((level, title))
            start = pos
        pos += len(line)
    flush(len(text))
    return segments


def parse_pdf(data: bytes, max_pages: int) -> list[Segment]:
    import warnings

    from pypdf import PdfReader

    try:
        reader = PdfReader(io.BytesIO(data), strict=False)
        if reader.is_encrypted and not reader.decrypt(""):
            raise UnsupportedDocument("encrypted_pdf", "The PDF is password protected.")
        n = len(reader.pages)
        if n > max_pages:
            raise UnsupportedDocument("too_many_pages", f"The PDF has more than {max_pages} pages.")
        segments: list[Segment] = []
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            for i, page in enumerate(reader.pages, start=1):
                text = (page.extract_text() or "").strip()
                if text:
                    segments.append(Segment(text=text, page_number=i))
    except UnsupportedDocument:
        raise
    except Exception as exc:
        raise PermanentIngestError("corrupt_pdf", "The PDF could not be parsed.") from exc
    if sum(len(s.text) for s in segments) < MIN_PDF_CHARS:
        raise UnsupportedDocument(
            "no_extractable_text",
            "The PDF has no extractable text (scanned or image-only); OCR is not supported.",
        )
    return segments


# ---- structured data -------------------------------------------------------------------------


def _clip(v: Any) -> str:
    s = v if isinstance(v, str) else json.dumps(v, ensure_ascii=False, default=str)
    s = " ".join(s.split())
    return s if len(s) <= MAX_CELL_CHARS else s[:MAX_CELL_CHARS] + "…"


def flatten(value: Any, prefix: str = "", depth: int = 0) -> Iterable[tuple[str, str]]:
    """Flatten nested JSON into ('a.b[0]', 'value') pairs (depth-capped)."""
    if depth > 8:
        yield prefix or "$", _clip(value)
    elif isinstance(value, dict):
        for k, v in value.items():
            yield from flatten(v, f"{prefix}.{k}" if prefix else str(k), depth + 1)
    elif isinstance(value, list):
        for i, v in enumerate(value):
            yield from flatten(v, f"{prefix}[{i}]", depth + 1)
    elif value is None or value == "":
        return
    else:
        yield prefix or "$", _clip(value)


def _group_records(
    records: Iterable[dict[str, Any]], fmt: str, size: int, max_rows: int, columns: list[str] | None
) -> list[Segment]:
    """Render records as 'col: value; ...' lines and pack lines into chunk-sized segments.

    Raw records are kept in metadata so later features (filters, SQL tools) need no re-parse.
    """
    segments: list[Segment] = []
    lines: list[str] = []
    rows: list[dict[str, Any]] = []
    first = 1
    length = 0
    total = 0

    def emit(last: int) -> None:
        nonlocal lines, rows, length
        if lines:
            md: dict[str, Any] = {
                "format": fmt,
                "source_unit": "rows",
                "row_start": first,
                "row_end": last,
                "rows": rows,
            }
            if columns:
                md["columns"] = columns
            segments.append(Segment(text="\n".join(lines), atomic=True, metadata=md))
        lines, rows, length = [], [], 0

    for idx, rec in enumerate(records, start=1):
        if idx > max_rows:
            raise UnsupportedDocument("too_many_rows", f"More than {max_rows} records.")
        pairs = [(k, v) for k, v in flatten(rec)]
        if not pairs:
            continue
        line = f"row {idx}: " + "; ".join(f"{k}: {v}" for k, v in pairs)
        if lines and length + len(line) > size:
            emit(idx - 1)
        if not lines:
            first = idx
        lines.append(line)
        rows.append({k: v for k, v in pairs})
        length += len(line) + 1
        total = idx
    emit(total)
    if not segments:
        raise UnsupportedDocument("empty_document", "The file contains no data rows.")
    return segments


def parse_delimited(data: bytes, fmt: Format, size: int, max_rows: int) -> list[Segment]:
    text = decode_text(data)
    if fmt is Format.TSV:
        delim = "\t"
    else:
        try:
            delim = csv.Sniffer().sniff(text[:4096], delimiters=",;|\t").delimiter
        except csv.Error:
            delim = ","
    try:
        reader = csv.reader(io.StringIO(text, newline=""), delimiter=delim)
        header = next(reader, None)
        if not header:
            raise UnsupportedDocument("empty_document", "The file has no header row.")
        seen: dict[str, int] = {}
        columns: list[str] = []
        for i, h in enumerate(header, start=1):
            name = h.strip() or f"col_{i}"
            seen[name] = seen.get(name, 0) + 1
            columns.append(name if seen[name] == 1 else f"{name}_{seen[name]}")

        def records() -> Iterable[dict[str, Any]]:
            for row in reader:
                yield {c: v for c, v in zip(columns, row, strict=False) if v.strip()}

        return _group_records(records(), fmt.value, size, max_rows, columns)
    except csv.Error as exc:
        raise PermanentIngestError("malformed_csv", "The delimited file is malformed.") from exc


def parse_json(data: bytes, fmt: Format, size: int, max_rows: int) -> list[Segment]:
    text = decode_text(data)
    try:
        if fmt is Format.JSONL:
            recs = [json.loads(ln) for ln in text.splitlines() if ln.strip()]
        else:
            doc = json.loads(text)
            recs = doc if isinstance(doc, list) else [doc]
    except ValueError as exc:
        raise PermanentIngestError("malformed_json", "The JSON could not be parsed.") from exc
    records = [r if isinstance(r, dict) else {"value": r} for r in recs]
    return _group_records(records, fmt.value, size, max_rows, None)


def parse(
    data: bytes, fmt: Format, *, chunk_size: int, max_rows: int, max_pages: int
) -> list[Segment]:
    if fmt is Format.TXT:
        return parse_text(data)
    if fmt is Format.MD:
        return parse_markdown(data)
    if fmt is Format.PDF:
        return parse_pdf(data, max_pages)
    if fmt in (Format.CSV, Format.TSV):
        return parse_delimited(data, fmt, chunk_size, max_rows)
    return parse_json(data, fmt, chunk_size, max_rows)
