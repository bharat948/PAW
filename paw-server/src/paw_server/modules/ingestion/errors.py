class IngestError(Exception):
    code = "ingest_error"


class UnsupportedDocument(IngestError):
    """Terminal: the document is valid but cannot yield searchable text (scan, empty, too large)."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class PermanentIngestError(IngestError):
    """Terminal FAILED: retrying cannot help (corrupt data, integrity mismatch, budget)."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class TransientIngestError(IngestError):
    """Retryable: provider 429/timeout, storage blip, lease contention."""

    code = "transient"


class LeaseLost(IngestError):
    """Another worker owns the job now; stop without writing."""
