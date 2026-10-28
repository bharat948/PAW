import logging
from dataclasses import dataclass

from psycopg_pool import ConnectionPool

from paw_server.db.migrations_info import expected_head
from paw_server.modules.ingestion.jobs import Conn

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ReadinessResult:
    ready: bool
    failed_check: str | None = None  # "database" | "migrations"


def check_ready(pool: ConnectionPool[Conn]) -> ReadinessResult:
    try:
        pool.open()  # idempotent
        with pool.connection() as conn:
            conn.execute("SELECT 1")
            try:
                row = conn.execute("SELECT version_num FROM alembic_version").fetchone()
            except Exception:
                conn.rollback()
                log.error("readiness: alembic_version missing; migrations not applied")
                return ReadinessResult(False, "migrations")
    except Exception as exc:
        log.error("readiness: database unavailable", extra={"error": type(exc).__name__})
        return ReadinessResult(False, "database")
    current = row["version_num"] if row else None
    if current != expected_head():
        log.error(
            "readiness: migration mismatch",
            extra={"current": current, "expected": expected_head()},
        )
        return ReadinessResult(False, "migrations")
    return ReadinessResult(True)
