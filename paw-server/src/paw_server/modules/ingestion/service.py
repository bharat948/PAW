"""Upload flow: validate -> hash -> store original durably -> register document/version/job."""

import hashlib
import tempfile
from dataclasses import dataclass
from typing import IO
from uuid import UUID

from psycopg.errors import UniqueViolation
from psycopg_pool import ConnectionPool

from paw_server.core.config import Settings
from paw_server.core.errors import ApiError
from paw_server.modules.ingestion.jobs import Conn
from paw_server.modules.ingestion.models import MEDIA_TYPES, Actor
from paw_server.modules.ingestion.pipeline import config_hash
from paw_server.modules.ingestion.sniff import detect_format, safe_filename, validate_content
from paw_server.modules.ingestion.storage import ObjectStore

_CHUNK = 64 * 1024


@dataclass
class UploadResult:
    document_id: UUID
    version_id: UUID
    duplicate: bool


def _spool(src: IO[bytes], max_bytes: int) -> tuple[IO[bytes], str, int]:
    """Stream to a bounded temp file while hashing; never holds the upload twice in memory."""
    tmp = tempfile.SpooledTemporaryFile(max_size=1024 * 1024)  # noqa: SIM115
    h = hashlib.sha256()
    size = 0
    while block := src.read(_CHUNK):
        size += len(block)
        if size > max_bytes:
            tmp.close()
            raise ApiError(
                413, message=f"The file exceeds the {max_bytes // (1024 * 1024)} MiB limit."
            )
        h.update(block)
        tmp.write(block)
    tmp.seek(0)
    return tmp, h.hexdigest(), size


def _existing(conn: Conn, ws: UUID, sha: str, cfg: str, idem: str | None) -> UploadResult | None:
    row = conn.execute(
        """SELECT document_id, id FROM document_versions WHERE workspace_id = %s AND (
               (sha256 = %s AND ingest_config_hash = %s) OR (%s::text IS NOT NULL AND idempotency_key = %s))
           ORDER BY (sha256 = %s) DESC LIMIT 1""",
        (ws, sha, cfg, idem, idem, sha),
    ).fetchone()
    conn.commit()
    return UploadResult(row["document_id"], row["id"], True) if row else None


def upload(
    pool: ConnectionPool[Conn],
    store: ObjectStore,
    s: Settings,
    actor: Actor,
    *,
    filename: str | None,
    declared_type: str | None,
    body: IO[bytes],
    idempotency_key: str | None,
) -> UploadResult:
    name = safe_filename(filename)
    if idempotency_key is not None and not (1 <= len(idempotency_key) <= 128):
        raise ApiError(422, "invalid_idempotency_key", "Idempotency-Key must be 1-128 characters.")
    tmp, sha, size = _spool(body, s.max_upload_bytes)
    try:
        if size == 0:
            raise ApiError(422, "empty_file", "The file is empty.")
        head = tmp.read(2048)
        tmp.seek(0)
        fmt = detect_format(name, declared_type, head)
        validate_content(fmt, tmp.read())
        tmp.seek(0)
        cfg = config_hash(s)

        with pool.connection() as conn:
            found = _existing(conn, actor.workspace_id, sha, cfg, idempotency_key)
            if found:
                return found
            key = f"workspaces/{actor.workspace_id}/{sha[:2]}/{sha}"
            store.put(
                key, tmp, size=size, sha256=sha, content_type=MEDIA_TYPES[fmt]
            )  # before queueing

            try:
                return _register(
                    conn, actor, name, MEDIA_TYPES[fmt], key, sha, size, cfg, idempotency_key
                )
            except UniqueViolation:
                conn.rollback()  # lost a race with an identical concurrent upload
                found = _existing(conn, actor.workspace_id, sha, cfg, idempotency_key)
                if found:
                    return found
                raise
    finally:
        tmp.close()


def _register(
    conn: Conn,
    actor: Actor,
    name: str,
    media_type: str,
    key: str,
    sha: str,
    size: int,
    cfg: str,
    idem: str | None,
) -> UploadResult:
    sibling = conn.execute(
        "SELECT document_id FROM document_versions WHERE workspace_id = %s AND sha256 = %s LIMIT 1",
        (actor.workspace_id, sha),
    ).fetchone()
    if sibling:  # same bytes, new processing config => next version of the same document
        doc_id = sibling["document_id"]
        conn.execute("SELECT id FROM documents WHERE id = %s FOR UPDATE", (doc_id,))
        version_no = conn.execute(
            "SELECT COALESCE(MAX(version_no), 0) + 1 AS n FROM document_versions WHERE document_id = %s",
            (doc_id,),
        ).fetchone()["n"]  # type: ignore[index]
    else:
        doc_id = conn.execute(
            """INSERT INTO documents (workspace_id, display_name, media_type, created_by)
               VALUES (%s, %s, %s, %s) RETURNING id""",
            (actor.workspace_id, name, media_type, actor.id),
        ).fetchone()["id"]  # type: ignore[index]
        version_no = 1
    ver_id = conn.execute(
        """INSERT INTO document_versions (document_id, workspace_id, version_no, sha256, object_key,
               size_bytes, original_filename, status, ingest_config_hash, idempotency_key)
           VALUES (%s, %s, %s, %s, %s, %s, %s, 'STORED', %s, %s) RETURNING id""",
        (doc_id, actor.workspace_id, version_no, sha, key, size, name, cfg, idem),
    ).fetchone()["id"]  # type: ignore[index]
    conn.execute(
        "INSERT INTO ingestion_events (version_id, status) VALUES (%s, 'RECEIVED'), (%s, 'STORED')",
        (ver_id, ver_id),
    )
    conn.execute(
        "INSERT INTO ingestion_jobs (version_id, workspace_id, stage) VALUES (%s, %s, 'STORED')",
        (ver_id, actor.workspace_id),
    )
    conn.commit()
    return UploadResult(doc_id, ver_id, False)
