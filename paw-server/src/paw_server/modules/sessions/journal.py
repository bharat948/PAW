"""Crash-safe deletion receipts, independent of PostgreSQL and Hermes state."""

import fcntl
import os
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Receipt(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: Literal[1] = 1
    id: UUID
    scope: Literal["message", "session"]
    session_id: UUID
    workspace_id: UUID
    message_id: UUID | None = None
    run_ids: list[UUID] = Field(default_factory=list, max_length=100)
    requested_by: str
    requested_at: datetime
    include_delegated: bool = False

    @model_validator(mode="after")
    def valid_scope(self) -> "Receipt":
        if (self.scope == "message") != (self.message_id is not None):
            raise ValueError("message receipts require a message ID")
        return self


class Journal:
    def __init__(self, directory: str = "") -> None:
        self.path = (
            Path(directory)
            if directory
            else Path(os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local/state")))
            / "paw/deletions"
        )

    @contextmanager
    def guard(self, *, synchronization: bool = False) -> Iterator[None]:
        self.path.mkdir(parents=True, exist_ok=True, mode=0o700)
        lock = self.path / (".sync.lock" if synchronization else ".lock")
        with lock.open("a+") as f:
            os.chmod(lock, 0o600)
            fcntl.flock(f, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)

    def write(self, receipt: Receipt) -> None:
        dest = self.path / f"{receipt.id}.json"
        if dest.exists():
            if Receipt.model_validate_json(dest.read_text()) != receipt:
                raise ValueError("journal_receipt_conflict")
            return
        fd, name = tempfile.mkstemp(prefix=".receipt-", dir=self.path)
        try:
            with os.fdopen(fd, "w") as f:
                f.write(receipt.model_dump_json())
                f.flush()
                os.fsync(f.fileno())
            os.replace(name, dest)
            dfd = os.open(self.path, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(dfd)
            finally:
                os.close(dfd)
        finally:
            if os.path.exists(name):
                os.unlink(name)

    def receipts(self) -> list[Receipt]:
        if not self.path.exists():
            return []
        rows = [
            Receipt.model_validate_json(p.read_text())
            for p in self.path.glob("*.json")
            if p.name != "sync-status.json"
        ]
        for row in rows:
            if (
                row.version != 1
                or row.scope not in ("message", "session")
                or (row.scope == "message") != (row.message_id is not None)
            ):
                raise ValueError("invalid_deletion_receipt")
        return sorted(rows, key=lambda r: (r.requested_at, str(r.id)))

    def export(self) -> dict[str, Any]:
        rows = self.receipts()
        return {
            "version": 1,
            "receipts": [r.model_dump(mode="json") for r in rows],
            "synced_through": rows[-1].requested_at.isoformat() if rows else None,
        }
