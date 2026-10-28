"""PostgreSQL-backed durable job queue: SKIP LOCKED claims, leases, fencing, bounded backoff."""

import random
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from psycopg import Connection
from psycopg.rows import DictRow
from psycopg.types.json import Jsonb

from paw_server.modules.ingestion.errors import LeaseLost

Conn = Connection[DictRow]


@dataclass
class Job:
    id: UUID
    version_id: UUID
    workspace_id: UUID
    stage: str
    attempt_count: int
    checkpoint: dict[str, Any]
    owner: str


def claim_job(conn: Conn, owner: str, lease_s: int, max_attempts: int) -> Job | None:
    """Claim one runnable job (new, retry-due, or lease-expired). Increments attempt_count."""
    row = conn.execute(
        """
        UPDATE ingestion_jobs SET lease_owner = %(o)s,
               lease_expires_at = now() + make_interval(secs => %(l)s),
               attempt_count = attempt_count + 1,
               started_at = COALESCE(started_at, now())
        WHERE id = (
            SELECT id FROM ingestion_jobs
            WHERE finished_at IS NULL AND next_retry_at <= now()
              AND (lease_owner IS NULL OR lease_expires_at < now())
            ORDER BY next_retry_at, created_at
            FOR UPDATE SKIP LOCKED LIMIT 1)
        RETURNING id, version_id, workspace_id, stage, attempt_count, checkpoint
        """,
        {"o": owner, "l": lease_s},
    ).fetchone()
    conn.commit()
    if row is None:
        return None
    job = Job(owner=owner, **row)
    if job.attempt_count > max_attempts:  # repeated crashes/lease expiries: dead-letter it
        finish(
            conn,
            job,
            "FAILED",
            "max_attempts_exceeded",
            "The job exceeded its retry limit.",
            dead_letter=True,
        )
        return None
    return job


def _fenced(conn: Conn, job: Job, sql: str, params: dict[str, Any]) -> None:
    cur = conn.execute(sql, {"job": job.id, "owner": job.owner, **params})
    if cur.rowcount != 1:
        conn.rollback()
        raise LeaseLost(str(job.id))


def set_stage(conn: Conn, job: Job, status: str, lease_s: int, detail: str | None = None) -> None:
    """Advance stage + version status + event atomically; fails if the lease was taken over."""
    _fenced(
        conn,
        job,
        """UPDATE ingestion_jobs SET stage = %(st)s,
               lease_expires_at = now() + make_interval(secs => %(l)s)
           WHERE id = %(job)s AND lease_owner = %(owner)s AND finished_at IS NULL""",
        {"st": status, "l": lease_s},
    )
    conn.execute(
        "UPDATE document_versions SET status = %s, status_updated_at = now() WHERE id = %s",
        (status, job.version_id),
    )
    conn.execute(
        "INSERT INTO ingestion_events (version_id, status, detail) VALUES (%s, %s, %s)",
        (job.version_id, status, detail),
    )
    conn.commit()


def heartbeat(conn: Conn, job: Job, lease_s: int, checkpoint: dict[str, Any] | None = None) -> None:
    """Extend the lease (and optionally persist the checkpoint) inside the caller's transaction."""
    _fenced(
        conn,
        job,
        """UPDATE ingestion_jobs SET lease_expires_at = now() + make_interval(secs => %(l)s),
               checkpoint = COALESCE(%(cp)s::jsonb, checkpoint)
           WHERE id = %(job)s AND lease_owner = %(owner)s AND finished_at IS NULL""",
        {"l": lease_s, "cp": Jsonb(checkpoint) if checkpoint is not None else None},
    )


def finish(
    conn: Conn,
    job: Job,
    status: str,
    code: str | None = None,
    message: str | None = None,
    dead_letter: bool = False,
) -> None:
    """Terminal transition (COMPLETED handled by pipeline.finalize). Fenced."""
    _fenced(
        conn,
        job,
        """UPDATE ingestion_jobs SET stage = %(st)s, finished_at = now(), lease_owner = NULL,
               lease_expires_at = NULL, dead_letter = %(dl)s, last_error = %(msg)s
           WHERE id = %(job)s AND lease_owner = %(owner)s AND finished_at IS NULL""",
        {"st": status, "dl": dead_letter, "msg": message},
    )
    conn.execute(
        """UPDATE document_versions SET status = %s, error_code = %s, error_message = %s,
               status_updated_at = now() WHERE id = %s""",
        (status, code, message, job.version_id),
    )
    conn.execute(
        "INSERT INTO ingestion_events (version_id, status, detail) VALUES (%s, %s, %s)",
        (job.version_id, status, f"{code}: {message}" if code else message),
    )
    conn.commit()


def schedule_retry(conn: Conn, job: Job, error: str, base_s: float, max_s: float) -> float:
    """Release the lease and back off exponentially with jitter. Returns the delay."""
    delay = min(max_s, base_s * (2 ** (job.attempt_count - 1)))
    delay = delay * (0.5 + random.random() / 2)  # noqa: S311 - jitter, not security
    _fenced(
        conn,
        job,
        """UPDATE ingestion_jobs SET lease_owner = NULL, lease_expires_at = NULL,
               next_retry_at = now() + make_interval(secs => %(d)s), last_error = %(e)s
           WHERE id = %(job)s AND lease_owner = %(owner)s AND finished_at IS NULL""",
        {"d": delay, "e": error},
    )
    conn.execute(
        "INSERT INTO ingestion_events (version_id, status, detail) VALUES (%s, 'RETRY', %s)",
        (job.version_id, f"attempt {job.attempt_count} failed: {error}; retry in {delay:.1f}s"),
    )
    conn.commit()
    return float(delay)
