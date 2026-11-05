"""Ledger operations join the caller's transaction; they never commit it.

Writers lock session, then run, then event sequence. A fork inherits a prefix by reference;
its own sequence starts immediately after that prefix. No execution is copied.
"""

import json
import re
from typing import Any
from uuid import UUID

from psycopg.types.json import Jsonb

from paw_server.modules.ingestion.jobs import Conn

TOMBSTONE = "[message forgotten]"
_SECRET = re.compile(
    r"(?i)(secret|password|authorization|credential|api[_-]?key|access[_-]?token|refresh[_-]?token)"
)
_VALUE = re.compile(
    r"\b(?:sk-(?:proj-)?[A-Za-z0-9_-]{12,}|gh[pousr]_[A-Za-z0-9_]{16,}|Bearer\s+[A-Za-z0-9._~-]{12,})\b",
    re.I,
)


def safe_data(value: Any) -> Any:
    """Strip credentials recursively before persistence, including values in free text."""
    if isinstance(value, dict):
        return {
            str(k): "[redacted]" if _SECRET.search(str(k)) else safe_data(v)
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [safe_data(v) for v in value]
    if isinstance(value, str):
        return _VALUE.sub("[redacted]", value)
    return value


def estimate(text: str) -> int:
    return max(1, (len(text) + 3) // 4)


def lock(conn: Conn, session_id: UUID) -> dict[str, Any]:
    row = conn.execute(
        "SELECT * FROM agent_sessions WHERE id = %s FOR UPDATE", (session_id,)
    ).fetchone()
    if row is None:
        raise ValueError("session_missing")
    return row


def lock_for_run(conn: Conn, run_id: UUID) -> dict[str, Any] | None:
    row = conn.execute("SELECT session_id FROM agent_runs WHERE id = %s", (run_id,)).fetchone()
    if row is None:
        return None
    lock(conn, row["session_id"])
    return conn.execute("SELECT * FROM agent_runs WHERE id = %s FOR UPDATE", (run_id,)).fetchone()


def append(
    conn: Conn,
    session_id: UUID,
    kind: str,
    author: str,
    body: str = "",
    *,
    data: dict[str, Any] | None = None,
    run_id: UUID | None = None,
    invocation_id: UUID | None = None,
    status: str = "complete",
) -> dict[str, Any]:
    session = lock(conn, session_id)
    if run_id is not None:
        run = conn.execute("SELECT session_id FROM agent_runs WHERE id = %s", (run_id,)).fetchone()
        if run is None or run["session_id"] != session_id:
            raise ValueError("run_session_mismatch")
    if kind in ("user", "assistant") and run_id is not None:
        prior = conn.execute(
            "SELECT * FROM agent_session_messages WHERE run_id = %s AND kind = %s", (run_id, kind)
        ).fetchone()
        if prior:
            return prior
    if invocation_id is not None:
        prior = conn.execute(
            "SELECT * FROM agent_session_messages WHERE tool_invocation_id = %s AND kind = %s",
            (invocation_id, kind),
        ).fetchone()
        if prior:
            return prior
    body, payload = safe_data(body), safe_data(data or {})
    row = conn.execute(
        "INSERT INTO agent_session_messages (session_id, seq, kind, author_actor_id, run_id, tool_invocation_id, status, token_estimate) "
        "SELECT %s, greatest(coalesce(max(seq), 0), %s) + 1, %s, %s, %s, %s, %s, %s "
        "FROM agent_session_messages WHERE session_id = %s RETURNING *",
        (
            session_id,
            session["fork_seq"] or 0,
            kind,
            author,
            run_id,
            invocation_id,
            status,
            estimate(body + json.dumps(payload, sort_keys=True)),
            session_id,
        ),
    ).fetchone()
    assert row is not None
    conn.execute(
        "INSERT INTO agent_message_contents (message_id, body, data) VALUES (%s, %s, %s)",
        (row["id"], body, Jsonb(payload)),
    )
    title = " ".join(body.split())[:60] if kind == "user" else ""
    conn.execute(
        "UPDATE agent_sessions SET last_activity_at = now(), "
        "title = CASE WHEN title = '' AND %s <> '' THEN %s ELSE title END WHERE id = %s",
        (title, title, session_id),
    )
    return row


def messages(
    conn: Conn,
    session_id: UUID,
    *,
    through: int | None = None,
    _seen: frozenset[UUID] = frozenset(),
) -> list[dict[str, Any]]:
    if session_id in _seen:
        raise ValueError("cyclic_history")
    s = conn.execute("SELECT * FROM agent_sessions WHERE id = %s", (session_id,)).fetchone()
    if s is None:
        return []
    prefix: list[dict[str, Any]] = []
    if s["forked_from_session_id"] is not None:
        prefix = messages(
            conn,
            s["forked_from_session_id"],
            through=min(s["fork_seq"], through) if through is not None else s["fork_seq"],
            _seen=_seen | {session_id},
        )
    rows = conn.execute(
        "SELECT m.*, c.body, c.data, (d.message_id IS NOT NULL) AS forgotten FROM agent_session_messages m "
        "LEFT JOIN agent_message_contents c ON c.message_id = m.id "
        "LEFT JOIN agent_message_redactions d ON d.message_id = m.id "
        "WHERE m.session_id = %s AND (%s::int IS NULL OR m.seq <= %s) ORDER BY m.seq",
        (session_id, through, through),
    ).fetchall()
    return prefix + list(rows)


def terminal_message(conn: Conn, run: dict[str, Any], state: str, output: str) -> None:
    if output:
        append(
            conn,
            run["session_id"],
            "assistant",
            run["executing_actor_id"],
            output,
            run_id=run["id"],
            status="complete" if state == "SUCCEEDED" else "unfinished",
        )
    else:
        append(
            conn,
            run["session_id"],
            "platform_event",
            "system",
            f"Run ended: {state.lower()}.",
            data={"state": state},
            run_id=run["id"],
        )
