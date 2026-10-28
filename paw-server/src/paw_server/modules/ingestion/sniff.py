"""Upload validation: filename safety, extension allowlist, content sniffing."""

import json
from pathlib import PurePosixPath

from paw_server.core.errors import ApiError
from paw_server.modules.ingestion.models import EXTENSIONS, MEDIA_TYPES, Format

ALLOWED_DECLARED = set(MEDIA_TYPES.values()) | {
    "application/octet-stream",
    "text/x-markdown",
    "application/x-json",
    "text/json",
    "application/csv",
    "application/vnd.ms-excel",  # browsers report .csv as this on Windows
    "",
}


def safe_filename(name: str | None) -> str:
    if not name:
        raise ApiError(422, "invalid_filename", "A filename is required.")
    if len(name) > 255 or any(ord(c) < 32 or ord(c) == 127 for c in name):
        raise ApiError(422, "invalid_filename", "The filename is not allowed.")
    if "/" in name or "\\" in name or name in (".", "..") or ".." in PurePosixPath(name).parts:
        raise ApiError(422, "invalid_filename", "The filename is not allowed.")
    return name.strip()


def detect_format(filename: str, declared_type: str | None, head: bytes) -> Format:
    ext = PurePosixPath(filename.lower()).suffix
    fmt = EXTENSIONS.get(ext)
    if fmt is None:
        raise ApiError(
            415, "unsupported_format", "Supported formats: .txt .md .pdf .csv .tsv .json .jsonl"
        )
    base = (declared_type or "").split(";")[0].strip().lower()
    if base not in ALLOWED_DECLARED:
        raise ApiError(415, "unsupported_format", "The declared content type is not supported.")
    if fmt is Format.PDF:
        if b"%PDF-" not in head[:1024]:
            raise ApiError(415, "content_mismatch", "The file content is not a PDF.")
    elif b"\x00" in head or head.startswith(b"%PDF-") or head[:4] in (b"\x89PNG", b"PK\x03\x04"):
        raise ApiError(415, "content_mismatch", "The file content does not match its extension.")
    return fmt


def validate_content(fmt: Format, data: bytes) -> None:
    """Reject corrupt uploads up front so no searchable document is created (001B-R01)."""
    if fmt is Format.PDF:
        import io

        from pypdf import PdfReader  # local import: heavy

        try:
            reader = PdfReader(io.BytesIO(data), strict=False)
            if reader.is_encrypted:
                return  # surfaces as UNSUPPORTED during ingestion
            if len(reader.pages) == 0 or not data.rstrip().endswith(b"%%EOF"):
                raise ValueError("truncated")
        except Exception as exc:
            raise ApiError(422, "corrupt_file", "The PDF is corrupt or truncated.") from exc
        return
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ApiError(422, "corrupt_file", "The file is not valid UTF-8 text.") from exc
    if "\x00" in text:
        raise ApiError(422, "corrupt_file", "The file contains binary data.")
    try:
        if fmt is Format.JSON:
            json.loads(text)
        elif fmt is Format.JSONL:
            for line in text.splitlines():
                if line.strip():
                    json.loads(line)
    except ValueError as exc:
        raise ApiError(422, "corrupt_file", "The file is not valid JSON.") from exc
