"""Ingestion stages with durable checkpoints. Safe to crash and re-run at any point."""

import hashlib
import logging
from dataclasses import dataclass
from datetime import date
from typing import Any

from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

from paw_server.core.config import Settings
from paw_server.modules.ingestion import jobs
from paw_server.modules.ingestion.chunking import chunk_segments
from paw_server.modules.ingestion.embedding import Embedder, estimate_tokens
from paw_server.modules.ingestion.errors import (
    LeaseLost,
    PermanentIngestError,
    TransientIngestError,
    UnsupportedDocument,
)
from paw_server.modules.ingestion.jobs import Conn
from paw_server.modules.ingestion.models import Chunk, Format
from paw_server.modules.ingestion.parsers import PARSER_VERSION, parse
from paw_server.modules.ingestion.storage import ObjectStore

log = logging.getLogger("paw.ingest")


def config_hash(s: Settings) -> str:
    """Identifies processing config: changing any of it yields a new document version."""
    parts = [PARSER_VERSION, s.chunk_size, s.chunk_overlap, s.embedding_model, s.embedding_dim]
    return hashlib.sha256("|".join(map(str, parts)).encode()).hexdigest()[:16]


def vec_literal(v: list[float]) -> str:
    return "[" + ",".join(f"{x:.7g}" for x in v) + "]"


@dataclass
class Worker:
    pool: ConnectionPool[Conn]
    store: ObjectStore
    embedder: Embedder
    settings: Settings
    owner: str

    # ---- public ------------------------------------------------------------------------------
    def run_once(self) -> bool:
        """Claim and process at most one job. Returns True if a job was handled."""
        with self.pool.connection() as conn:
            job = jobs.claim_job(
                conn, self.owner, self.settings.lease_seconds, self.settings.max_attempts
            )
            if job is None:
                return False
            self._process(conn, job)
            return True

    # ---- internals ---------------------------------------------------------------------------
    def _process(self, conn: Conn, job: jobs.Job) -> None:
        s = self.settings
        try:
            self._run_stages(conn, job)
        except LeaseLost:
            log.warning("lease lost; abandoning job", extra={"job_id": str(job.id)})
        except UnsupportedDocument as exc:
            conn.rollback()
            self._terminal(conn, job, "UNSUPPORTED", exc.code, str(exc))
        except PermanentIngestError as exc:
            conn.rollback()
            self._terminal(conn, job, "FAILED", exc.code, str(exc))
        except Exception as exc:  # transient or unexpected: bounded retry, never log content
            conn.rollback()
            msg = str(exc) if isinstance(exc, TransientIngestError) else type(exc).__name__
            if job.attempt_count >= s.max_attempts:
                self._terminal(conn, job, "FAILED", "retries_exhausted", msg, dead_letter=True)
            else:
                try:
                    delay = jobs.schedule_retry(conn, job, msg, s.retry_base_s, s.retry_max_s)
                    log.warning(
                        "job will retry", extra={"job_id": str(job.id), "delay_s": round(delay, 1)}
                    )
                except LeaseLost:
                    pass

    def _terminal(
        self,
        conn: Conn,
        job: jobs.Job,
        status: str,
        code: str,
        msg: str,
        dead_letter: bool = False,
    ) -> None:
        try:
            jobs.finish(conn, job, status, code, msg, dead_letter)
            log.info("job finished", extra={"job_id": str(job.id), "status": status, "code": code})
        except LeaseLost:
            pass

    def _run_stages(self, conn: Conn, job: jobs.Job) -> None:
        s = self.settings
        ver = conn.execute(
            "SELECT id, document_id, workspace_id, sha256, object_key, original_filename, status "
            "FROM document_versions WHERE id = %s",
            (job.version_id,),
        ).fetchone()
        assert ver is not None
        if ver["status"] in ("COMPLETED", "FAILED", "UNSUPPORTED"):
            jobs.finish(conn, job, ver["status"])
            return

        if not job.checkpoint.get("chunked"):
            jobs.set_stage(conn, job, "EXTRACTING", s.lease_seconds)
            data = self.store.get(ver["object_key"])
            if hashlib.sha256(data).hexdigest() != ver["sha256"]:
                raise PermanentIngestError(
                    "integrity_mismatch", "Stored original failed its checksum."
                )
            fmt = Format(
                ver["original_filename"]
                .lower()
                .rsplit(".", 1)[-1]
                .replace("markdown", "md")
                .replace("ndjson", "jsonl")
            )
            segments = parse(
                data,
                fmt,
                chunk_size=s.chunk_size,
                max_rows=s.max_tabular_rows,
                max_pages=s.max_pdf_pages,
            )
            del data

            jobs.set_stage(conn, job, "CHUNKING", s.lease_seconds)
            chunks = chunk_segments(segments, s.chunk_size, s.chunk_overlap)
            if not chunks:
                raise UnsupportedDocument("empty_document", "No searchable text was extracted.")
            if len(chunks) > s.max_chunks_per_document:
                raise UnsupportedDocument(
                    "too_many_chunks",
                    f"The document produces more than {s.max_chunks_per_document} chunks.",
                )
            self._write_chunks(conn, job, ver, chunks)

        jobs.set_stage(conn, job, "EMBEDDING", s.lease_seconds)
        self._embed_all(conn, job, ver)

        jobs.set_stage(conn, job, "INDEXING", s.lease_seconds)
        self._finalize(conn, job, ver)

    def _write_chunks(
        self, conn: Conn, job: jobs.Job, ver: dict[str, Any], chunks: list[Chunk]
    ) -> None:
        # replace any partial leftovers from a crashed earlier attempt, atomically with the checkpoint
        conn.execute("DELETE FROM document_chunks WHERE version_id = %s", (ver["id"],))
        with conn.cursor() as cur:
            cur.executemany(
                """INSERT INTO document_chunks (version_id, workspace_id, ordinal, text, page_number,
                       section, source_start, source_end, metadata)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                [
                    (
                        ver["id"],
                        ver["workspace_id"],
                        c.ordinal,
                        c.text,
                        c.page_number,
                        c.section,
                        c.source_start,
                        c.source_end,
                        Jsonb(c.metadata),
                    )
                    for c in chunks
                ],
            )
        conn.execute(
            "UPDATE document_versions SET parser_version = %s WHERE id = %s",
            (PARSER_VERSION, ver["id"]),
        )
        jobs.heartbeat(
            conn,
            job,
            self.settings.lease_seconds,
            {"chunked": True, "chunk_count": len(chunks), "embedded": 0},
        )
        conn.commit()

    def _embed_all(self, conn: Conn, job: jobs.Job, ver: dict[str, Any]) -> None:
        s = self.settings
        while True:
            rows = conn.execute(
                "SELECT id, text FROM document_chunks WHERE version_id = %s AND embedding IS NULL "
                "ORDER BY ordinal LIMIT %s",
                (ver["id"], s.embedding_batch_size),
            ).fetchall()
            conn.commit()
            if not rows:
                return
            tokens = sum(estimate_tokens(r["text"]) for r in rows)
            used = conn.execute(
                "SELECT COALESCE(SUM(embedding_tokens), 0) AS t FROM usage_daily "
                "WHERE workspace_id = %s AND day = %s",
                (ver["workspace_id"], date.today()),
            ).fetchone()["t"]  # type: ignore[index]
            conn.commit()
            if used + tokens > s.embedding_daily_token_budget:
                raise PermanentIngestError(
                    "embedding_budget_exceeded",
                    "The daily embedding budget for this workspace is used up.",
                )
            vectors = self.embedder.embed(
                [r["text"] for r in rows]
            )  # may raise Transient/Permanent
            if len(vectors) != len(rows) or any(len(v) != self.embedder.dim for v in vectors):
                raise PermanentIngestError(
                    "embedding_dim_mismatch", "Embedding batch failed validation."
                )
            with conn.cursor() as cur:
                cur.executemany(
                    "UPDATE document_chunks SET embedding = %s::vector, embedding_model = %s WHERE id = %s",
                    [
                        (vec_literal(v), self.embedder.model, r["id"])
                        for v, r in zip(vectors, rows, strict=True)
                    ],
                )
            conn.execute(
                """INSERT INTO usage_daily (workspace_id, day, embedding_tokens) VALUES (%s, %s, %s)
                   ON CONFLICT (workspace_id, day)
                   DO UPDATE SET embedding_tokens = usage_daily.embedding_tokens + EXCLUDED.embedding_tokens""",
                (ver["workspace_id"], date.today(), tokens),
            )
            done = conn.execute(
                "SELECT count(*) AS n FROM document_chunks WHERE version_id = %s AND embedding IS NOT NULL",
                (ver["id"],),
            ).fetchone()["n"]  # type: ignore[index]
            jobs.heartbeat(
                conn, job, s.lease_seconds, {**job.checkpoint, "chunked": True, "embedded": done}
            )
            conn.commit()

    def _finalize(self, conn: Conn, job: jobs.Job, ver: dict[str, Any]) -> None:
        """Idempotent: activate this version's chunks and complete the job in one transaction."""
        missing = conn.execute(
            "SELECT count(*) AS n FROM document_chunks WHERE version_id = %s AND embedding IS NULL",
            (ver["id"],),
        ).fetchone()["n"]  # type: ignore[index]
        if missing:
            conn.rollback()
            raise TransientIngestError(f"{missing} chunks still lack embeddings")
        cur = conn.execute(
            """UPDATE ingestion_jobs SET stage = 'COMPLETED', finished_at = now(), lease_owner = NULL,
                   lease_expires_at = NULL
               WHERE id = %s AND lease_owner = %s AND finished_at IS NULL""",
            (job.id, job.owner),
        )
        if cur.rowcount != 1:
            conn.rollback()
            raise LeaseLost(str(job.id))
        conn.execute(
            "UPDATE document_versions SET status = 'COMPLETED', embedding_model = %s, "
            "error_code = NULL, error_message = NULL, status_updated_at = now() WHERE id = %s",
            (self.embedder.model, ver["id"]),
        )
        conn.execute(
            """UPDATE documents SET current_version_id = %s WHERE id = %s AND (
                   current_version_id IS NULL OR current_version_id IN (
                       SELECT id FROM document_versions WHERE document_id = %s AND version_no <=
                           (SELECT version_no FROM document_versions WHERE id = %s)))""",
            (ver["id"], ver["document_id"], ver["document_id"], ver["id"]),
        )
        conn.execute(
            "INSERT INTO ingestion_events (version_id, status) VALUES (%s, 'COMPLETED')",
            (ver["id"],),
        )
        conn.commit()
        log.info("job completed", extra={"job_id": str(job.id), "version_id": str(ver["id"])})
