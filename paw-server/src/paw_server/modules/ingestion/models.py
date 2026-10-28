from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any
from uuid import UUID


class Format(StrEnum):
    TXT = "txt"
    MD = "md"
    PDF = "pdf"
    CSV = "csv"
    TSV = "tsv"
    JSON = "json"
    JSONL = "jsonl"


MEDIA_TYPES = {
    Format.TXT: "text/plain",
    Format.MD: "text/markdown",
    Format.PDF: "application/pdf",
    Format.CSV: "text/csv",
    Format.TSV: "text/tab-separated-values",
    Format.JSON: "application/json",
    Format.JSONL: "application/x-ndjson",
}
EXTENSIONS = {f".{f.value}": f for f in Format} | {".markdown": Format.MD, ".ndjson": Format.JSONL}
TABULAR = {Format.CSV, Format.TSV, Format.JSON, Format.JSONL}


@dataclass(frozen=True)
class Actor:
    id: str
    workspace_id: UUID


@dataclass
class Segment:
    """A unit of extracted text with provenance.

    `offset` is the character position of `text` within its source stream (the decoded file, or
    the page text for PDFs). `atomic` segments (row groups) become exactly one chunk.
    """

    text: str
    offset: int = 0
    page_number: int | None = None
    section: str | None = None
    atomic: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class Chunk:
    ordinal: int
    text: str
    page_number: int | None
    section: str | None
    source_start: int
    source_end: int
    metadata: dict[str, Any]
