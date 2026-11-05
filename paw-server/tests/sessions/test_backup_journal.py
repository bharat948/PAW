"""Run with uv run --with pyrage pytest tests/sessions/test_backup_journal.py."""

import argparse
import importlib.util
import io
import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest

pyrage = pytest.importorskip(
    "pyrage", reason="backup tool uses its existing optional pyrage runtime"
)

from paw_server.modules.sessions.journal import Journal, Receipt  # noqa: E402


@pytest.fixture
def backup():
    path = Path(__file__).resolve().parents[3] / "scripts/backup/pawbackup.py"
    spec = importlib.util.spec_from_file_location("pawbackup_test", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class Storage:
    def __init__(self, corrupt=False):
        self.objects = {}
        self.corrupt = corrupt

    def put_object(self, *, Bucket, Key, Body):
        self.objects[Key] = Body

    def get_object(self, *, Bucket, Key):
        return {"Body": io.BytesIO(b"corrupt" if self.corrupt else self.objects[Key])}


def configured(tmp_path, monkeypatch, backup):
    identity = pyrage.x25519.Identity.generate()
    recipient = str(identity.to_public())
    envfile = tmp_path / "home.env"
    envfile.write_text("PAW_INSTALLATION_ID=privacy-test\n")
    journal = Journal(str(tmp_path / "journal"))
    receipt = Receipt(
        id=uuid4(),
        scope="session",
        session_id=uuid4(),
        workspace_id=uuid4(),
        requested_by="owner",
        requested_at=datetime.now(UTC),
    )
    with journal.guard():
        journal.write(receipt)
    monkeypatch.setenv("PAW_HOME_ENV", str(envfile))
    monkeypatch.setenv("PAW_BACKUP_AGE_RECIPIENT", recipient)
    monkeypatch.setenv("PAW_DELETION_JOURNAL_DIR", str(journal.path))
    return identity, journal, receipt


def test_encrypted_snapshot_is_verified_and_watermark_is_updated(tmp_path, monkeypatch, backup):
    identity, journal, receipt = configured(tmp_path, monkeypatch, backup)
    storage = Storage()
    monkeypatch.setattr(backup, "dest", lambda: (storage, "test"))
    backup.cmd_sync_deletions(argparse.Namespace(watch=False))
    latest = json.loads(storage.objects["privacy/privacy-test/latest.json"])
    encrypted = storage.objects[latest["key"]]
    assert str(receipt.session_id).encode() not in encrypted
    payload = json.loads(pyrage.decrypt(encrypted, [identity]))
    assert payload["receipts"][0]["id"] == str(receipt.id)
    assert (
        json.loads((journal.path / "sync-status.json").read_text())["synced_through"]
        == receipt.requested_at.isoformat()
    )


def test_corrupt_remote_copy_never_publishes_success(tmp_path, monkeypatch, backup):
    _, journal, _ = configured(tmp_path, monkeypatch, backup)
    storage = Storage(corrupt=True)
    monkeypatch.setattr(backup, "dest", lambda: (storage, "test"))
    with pytest.raises(ValueError, match="hash_mismatch"):
        backup.cmd_sync_deletions(argparse.Namespace(watch=False))
    assert "privacy/privacy-test/latest.json" not in storage.objects
    assert not (journal.path / "sync-status.json").exists()


def test_restore_requires_an_explicit_valid_journal(tmp_path, backup):
    bad = tmp_path / "invalid.json"
    bad.write_text('{"version":1,"receipts":[{"scope":"invalid"}]}')
    with pytest.raises(ValueError):
        backup.read_deletion_journal(str(bad), "unused")
    with pytest.raises(FileNotFoundError):
        backup.read_deletion_journal(str(tmp_path / "missing.json"), "unused")
