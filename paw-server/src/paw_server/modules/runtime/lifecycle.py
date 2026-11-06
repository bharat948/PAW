"""Agent run lifecycle in PostgreSQL. Every rule here is also enforced by the schema (migration 0006), so a bug in
this file cannot produce an illegal state: the database refuses it.

States: QUEUED -> DISPATCHING -> RUNNING -> SUCCEEDED | FAILED | CANCELLING -> CANCELLED | INTERRUPTED.
A state change and the event that explains it are written in one transaction, so a client that has seen the terminal
event can always read the final state.
"""

from typing import Any
from uuid import UUID

import psycopg
from psycopg.types.json import Jsonb

from paw_server.modules.ingestion.jobs import Conn
from paw_server.modules.runtime.protocol import TERMINAL_TYPES
from paw_server.modules.sessions import checkpoints
from paw_server.modules.sessions import service as ledger

TERMINAL = frozenset({"SUCCEEDED", "FAILED", "CANCELLED", "INTERRUPTED"})
ACTIVE = frozenset({"QUEUED", "DISPATCHING", "RUNNING", "CANCELLING"})
LEGAL: dict[str, frozenset[str]] = {
    "QUEUED": frozenset({"DISPATCHING", "CANCELLED"}),
    "DISPATCHING": frozenset({"RUNNING", "FAILED", "INTERRUPTED"}),
    "RUNNING": frozenset({"SUCCEEDED", "FAILED", "CANCELLING", "INTERRUPTED"}),
    "CANCELLING": frozenset({"CANCELLED", "INTERRUPTED"}),
}
STATE_FOR_TERMINAL_EVENT = {
    "run.completed": "SUCCEEDED",
    "run.failed": "FAILED",
    "run.cancelled": "CANCELLED",
    "run.interrupted": "INTERRUPTED",
}
EVENT_FOR_STATE = {v: k for k, v in STATE_FOR_TERMINAL_EVENT.items()}
RUN_COLUMNS = (
    "id, session_id, actor_id, executing_actor_id, runtime_id, client_request_id, user_message, state, hermes_run_id, requested_model, "
    "tokens_in, tokens_out, cache_read, cache_write, error_code, output, queued_at, started_at, finished_at"
)
FIELDS = {
    "hermes_run_id", "error_code", "output", "tokens_in", "tokens_out", "cache_read", "cache_write",
    "requested_model", "served_model", "cost_usd", "rate_card_version",
}  # fmt: skip


class AdmissionError(Exception):
    """`kind`: busy (a run is already active in this session) · full (queue limit) · conflict (key reused)."""

    def __init__(self, kind: str) -> None:
        super().__init__(kind)
        self.kind = kind


# ---- sessions -----------------------------------------------------------------------------------------------
def create_session(
    conn: Conn,
    *,
    owner: str,
    workspace_id: UUID,
    title: str = "",
    agent_actor_id: str = "admin-hermes",
) -> dict[str, Any]:
    row = conn.execute(
        "INSERT INTO agent_sessions (owner, workspace_id, title, agent_actor_id) VALUES (%s, %s, %s, %s) RETURNING *",
        (owner, workspace_id, title[:200], agent_actor_id),
    ).fetchone()
    conn.commit()
    assert row is not None
    return row


def get_session(conn: Conn, session_id: UUID, owner: str) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT * FROM agent_sessions WHERE id = %s AND owner = %s AND deleted_at IS NULL",
        (session_id, owner),
    ).fetchone()
    conn.commit()
    return row


def list_sessions(conn: Conn, owner: str, limit: int = 30) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT s.*, a.display_name AS actor_name FROM agent_sessions s JOIN actors a ON a.id = s.agent_actor_id "
        "WHERE s.owner = %s AND s.status = 'active' AND s.deleted_at IS NULL AND s.kind = 'chat' "
        "ORDER BY s.last_activity_at DESC, s.id DESC LIMIT %s",
        (owner, limit),
    ).fetchall()
    conn.commit()
    return rows


# ---- admission ----------------------------------------------------------------------------------------------
def _existing(conn: Conn, session_id: UUID, client_request_id: str) -> dict[str, Any] | None:
    return conn.execute(
        f"SELECT {RUN_COLUMNS} FROM agent_runs WHERE session_id = %s AND client_request_id = %s",
        (session_id, client_request_id),
    ).fetchone()


def admit(
    conn: Conn,
    *,
    session_id: UUID,
    actor_id: str,
    client_request_id: str,
    message: str,
    queue_limit: int,
    model: str,
) -> tuple[dict[str, Any], bool]:
    """(run, created). The same client_request_id returns the same run; it can never create a second one."""
    try:
        with conn.transaction():
            conn.execute("SELECT pg_advisory_xact_lock(hashtextextended('agent:admission', 0))")
            session = ledger.lock(conn, session_id)
            if session["deleted_at"] is not None or session["owner"] != actor_id:
                raise AdmissionError("missing")
            prior = _existing(conn, session_id, client_request_id)
            if prior is not None:
                if prior["user_message"] != message:
                    raise AdmissionError("conflict")
                return prior, False
            queued = conn.execute(
                "SELECT count(*) AS n FROM agent_runs WHERE state = 'QUEUED'"
            ).fetchone()
            if queued is not None and queued["n"] >= queue_limit:
                raise AdmissionError("full")
            row = conn.execute(
                f"INSERT INTO agent_runs (session_id, actor_id, executing_actor_id, client_request_id, user_message, requested_model) "
                f"VALUES (%s, %s, %s, %s, %s, %s) RETURNING {RUN_COLUMNS}",
                (
                    session_id,
                    session["requester_actor_id"],
                    session["agent_actor_id"],
                    client_request_id,
                    message,
                    model,
                ),
            ).fetchone()
            assert row is not None
            ledger.append(
                conn, session_id, "user", session["requester_actor_id"], message, run_id=row["id"]
            )
            _insert_event(conn, row["id"], "run.queued", {}, None)
        return row, True
    except psycopg.errors.UniqueViolation:
        raise AdmissionError("busy") from None


# ---- events -------------------------------------------------------------------------------------------------
def _insert_event(
    conn: Conn, run_id: UUID, type_: str, payload: dict[str, Any], source_event_id: str | None
) -> int | None:
    """Insert the run's next event inside the caller's transaction. The worker, the gateway and the lifecycle all write
    events for the same run, so seq allocation is serialised with a per-run advisory lock (released at commit).
    Returns the seq, or None when this source event was already stored (a replay)."""
    conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (str(run_id),))
    row = conn.execute(
        "INSERT INTO agent_run_events (run_id, seq, type, payload, source_event_id) "
        "SELECT %s, coalesce(max(seq), 0) + 1, %s, %s, %s FROM agent_run_events WHERE run_id = %s "
        "ON CONFLICT (run_id, source_event_id) WHERE source_event_id IS NOT NULL DO NOTHING RETURNING seq",
        (run_id, type_, Jsonb(payload), source_event_id, run_id),
    ).fetchone()
    return row["seq"] if row else None


def append_event(
    conn: Conn, run_id: UUID, type_: str, payload: dict[str, Any], source_event_id: str | None
) -> int | None:
    with conn.transaction():
        run = ledger.lock_for_run(conn, run_id)
        if run is None or run["state"] in TERMINAL:
            return None
        if conn.execute("SELECT 1 FROM agent_run_redactions WHERE run_id=%s", (run_id,)).fetchone():
            payload = {"redacted": True}
        return _insert_event(conn, run_id, type_, ledger.safe_data(payload), source_event_id)


def events_after(
    conn: Conn, run_id: UUID, after_seq: int, limit: int = 200
) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT seq, type, payload, occurred_at FROM agent_run_events WHERE run_id = %s AND seq > %s "
        "ORDER BY seq LIMIT %s",
        (run_id, after_seq, limit),
    ).fetchall()
    conn.commit()
    return rows


# ---- queue and lease ----------------------------------------------------------------------------------------
def ensure_runtime(conn: Conn, name: str = "hermes") -> UUID:
    row = conn.execute(
        "INSERT INTO agent_runtimes (name) VALUES (%s) ON CONFLICT (name) DO UPDATE SET name = excluded.name "
        "RETURNING id",
        (name,),
    ).fetchone()
    conn.commit()
    assert row is not None
    rid: UUID = row["id"]
    return rid


def claim_next(conn: Conn, runtime_id: UUID) -> dict[str, Any] | None:
    """Oldest queued run, if the runtime's lease is free. Concurrency is one run per runtime, enforced by the lease row."""
    with conn.transaction():
        runtime = conn.execute(
            "SELECT rt.actor_id FROM agent_runtimes rt JOIN actors a ON a.id=rt.actor_id "
            "WHERE rt.id=%s AND rt.desired_state='running' AND a.status='active' FOR UPDATE OF a, rt SKIP LOCKED",
            (runtime_id,),
        ).fetchone()
        if runtime is None:
            return None
        if conn.execute(
            "SELECT 1 FROM agent_runs WHERE executing_actor_id=%s "
            "AND state IN ('DISPATCHING','RUNNING','CANCELLING')",
            (runtime["actor_id"],),
        ).fetchone():
            return None
        nxt = conn.execute(
            "SELECT s.id AS session_id, r.id FROM agent_runs r JOIN agent_sessions s ON s.id = r.session_id "
            "WHERE r.state = 'QUEUED' AND r.executing_actor_id=%s AND s.agent_actor_id=%s "
            "AND s.deleted_at IS NULL ORDER BY r.queued_at, r.id "
            "FOR UPDATE OF s SKIP LOCKED LIMIT 1",
            (runtime["actor_id"], runtime["actor_id"]),
        ).fetchone()
        if nxt is None:
            return None
        run = conn.execute(
            "SELECT state FROM agent_runs WHERE id = %s FOR UPDATE", (nxt["id"],)
        ).fetchone()
        if run is None or run["state"] != "QUEUED":
            return None
        lease = conn.execute(
            "INSERT INTO agent_run_leases (runtime_id, run_id) VALUES (%s, %s) "
            "ON CONFLICT (runtime_id) DO NOTHING RETURNING run_id",
            (runtime_id, nxt["id"]),
        ).fetchone()
        if lease is None:
            return None
        result = conn.execute(
            f"UPDATE agent_runs SET state = 'DISPATCHING', runtime_id = %s, started_at = now() "
            f"WHERE id = %s RETURNING {RUN_COLUMNS}",
            (runtime_id, nxt["id"]),
        ).fetchone()
        assert result is not None
        _insert_event(conn, result["id"], "run.state", {"state": "DISPATCHING"}, None)
        return result


def bind_profile(
    conn: Conn,
    run_id: UUID,
    *,
    profile_version_id: UUID,
    version: int,
    compiled_hash: str,
    max_run_seconds: int,
) -> None:
    """Record which profile version a run uses, and when its time is up. Set once at claim, and persisted so a
    restart cannot extend it; a later activation never changes either."""
    conn.execute(
        "UPDATE agent_runs SET profile_version_id = %s, profile_version = %s, compiled_hash = %s, "
        "deadline_at = now() + make_interval(secs => %s) WHERE id = %s AND profile_version_id IS NULL",
        (profile_version_id, version, compiled_hash, max_run_seconds, run_id),
    )
    conn.commit()


def heartbeat(conn: Conn, run_id: UUID) -> None:
    conn.execute("UPDATE agent_run_leases SET heartbeat_at = now() WHERE run_id = %s", (run_id,))
    conn.commit()


# ---- transitions --------------------------------------------------------------------------------------------
def move(conn: Conn, run_id: UUID, to: str, **fields: Any) -> bool:
    """RUNNING etc. (non-terminal). False if the run is not in a state that may move to `to`."""
    bad = set(fields) - FIELDS
    assert not bad, bad
    sources = [s for s, tos in LEGAL.items() if to in tos]
    sets = ", ".join(["state = %s", *(f"{k} = %s" for k in fields)])
    row = conn.execute(
        f"UPDATE agent_runs SET {sets} WHERE id = %s AND state = ANY(%s) RETURNING id",
        (to, *fields.values(), run_id, sources),
    ).fetchone()
    if row is not None:
        _insert_event(conn, run_id, "run.state", {"state": to}, None)
    conn.commit()
    return row is not None


def finish(
    conn: Conn,
    run_id: UUID,
    to: str,
    *,
    event_type: str | None = None,
    payload: dict[str, Any] | None = None,
    source_event_id: str | None = None,
    **fields: Any,
) -> str | None:
    """Terminal transition + its terminal event + lease release, atomically. Returns the final state, or None if
    the run was already terminal (terminal states are immutable). A completion that arrives after a cancel request
    settles as CANCELLED with the output kept; any other terminal outcome of a cancelling run is INTERRUPTED."""
    assert to in TERMINAL
    bad = set(fields) - FIELDS
    assert not bad, bad
    with conn.transaction():
        cur = ledger.lock_for_run(conn, run_id)
        if cur is None or cur["state"] in TERMINAL:
            return None
        state = cur["state"]
        if state == "CANCELLING" and to == "SUCCEEDED":
            to = "CANCELLED"
        elif state == "CANCELLING" and to not in ("CANCELLED", "INTERRUPTED"):
            to = "INTERRUPTED"
        elif state == "QUEUED" and to != "CANCELLED":
            return None
        etype = event_type or EVENT_FOR_STATE[to]
        if etype in TERMINAL_TYPES and STATE_FOR_TERMINAL_EVENT[etype] != to:
            etype = EVENT_FOR_STATE[
                to
            ]  # keep event and state consistent after the cancel mapping above
        redacted = (
            conn.execute("SELECT 1 FROM agent_run_redactions WHERE run_id=%s", (run_id,)).fetchone()
            is not None
        )
        if not fields.get("output") and not redacted:
            partial = conn.execute(
                "SELECT coalesce(string_agg(coalesce(payload->>'raw',payload->>'text'), '' ORDER BY seq), '') AS t FROM agent_run_events "
                "WHERE run_id = %s AND type = 'message.delta'",
                (run_id,),
            ).fetchone()
            fields["output"] = partial["t"] if partial else ""
        clean, checkpoint = checkpoints.extract(str(fields.get("output") or ""))
        if redacted:
            clean, checkpoint = "", None
        fields["output"] = ledger.safe_data(clean)
        terminal_payload = {"redacted": True} if redacted else ledger.safe_data(dict(payload or {}))
        if "output" in terminal_payload:
            terminal_payload["output"] = fields["output"]
        sets = ", ".join(["state = %s", "finished_at = now()", *(f"{k} = %s" for k in fields)])
        conn.execute(f"UPDATE agent_runs SET {sets} WHERE id = %s", (to, *fields.values(), run_id))
        _insert_event(conn, run_id, etype, terminal_payload, source_event_id)
        if not redacted:
            ledger.terminal_message(conn, cur, to, str(fields.get("output") or ""))
        if checkpoint is not None:
            details = ledger.safe_data(checkpoint.model_dump())
            available = {
                str(r["id"])
                for r in conn.execute(
                    "SELECT id FROM agent_session_messages WHERE session_id = %s",
                    (cur["session_id"],),
                ).fetchall()
            }
            available.update(
                str(r["id"])
                for r in conn.execute(
                    "SELECT id FROM gateway_tool_attempts WHERE run_id = %s", (run_id,)
                ).fetchall()
            )
            details["evidence"] = [eid for eid in details["evidence"] if eid in available]
            if to != "SUCCEEDED":
                details["status"] = "blocked"
            ledger.append(
                conn,
                cur["session_id"],
                "platform_event",
                cur["executing_actor_id"],
                details["summary"],
                data={"type": "task_checkpoint", "agent_reported": True, **details},
                run_id=run_id,
            )
        conn.execute("DELETE FROM agent_run_leases WHERE run_id = %s", (run_id,))
        conn.execute(
            "INSERT INTO agent_session_jobs (session_id) VALUES (%s) ON CONFLICT (session_id) DO UPDATE "
            "SET state = CASE WHEN agent_session_jobs.state = 'working' THEN 'working' ELSE 'pending' END, "
            "attempts = CASE WHEN agent_session_jobs.state = 'working' THEN agent_session_jobs.attempts ELSE 0 END, "
            "available_at = now(), updated_at = now()",
            (cur["session_id"],),
        )
    return to


def request_cancel(
    conn: Conn, run_id: UUID, *, reason: str | None = None
) -> tuple[str, str | None]:
    """(outcome, hermes_run_id). outcome: cancelled (was queued) · stopping (stop must now be sent) ·
    already (cancelling or finished) · not_yet (dispatching: retry in a moment). `reason` is recorded as the
    run's error code and in the state event (deadline_exceeded), and survives to the terminal state."""
    with conn.transaction():
        row = ledger.lock_for_run(conn, run_id)
        if row is None:
            return "missing", None
        state = row["state"]
        if state == "QUEUED":
            pass
        elif state == "RUNNING":
            conn.execute(
                "UPDATE agent_runs SET state = 'CANCELLING', cancel_requested_at=now(), "
                "error_code = coalesce(%s, error_code) WHERE id = %s",
                (reason, run_id),
            )
            _insert_event(
                conn,
                run_id,
                "run.state",
                {"state": "CANCELLING", **({"reason": reason} if reason else {})},
                None,
            )
            return "stopping", row["hermes_run_id"]
        elif state == "DISPATCHING":
            return "not_yet", None
        else:
            return "already", row["hermes_run_id"]
    finish(conn, run_id, "CANCELLED")
    return "cancelled", None


# ---- reads and recovery -------------------------------------------------------------------------------------
def get_run(
    conn: Conn, run_id: UUID, owner: str, workspace_id: UUID | None = None
) -> dict[str, Any] | None:
    row = conn.execute(
        f"SELECT {RUN_COLUMNS} FROM agent_runs WHERE id = %s AND session_id IN "
        f"(SELECT id FROM agent_sessions WHERE owner = %s AND deleted_at IS NULL "
        f"AND (%s::uuid IS NULL OR workspace_id = %s))",
        (run_id, owner, workspace_id, workspace_id),
    ).fetchone()
    conn.commit()
    return row


def list_runs(conn: Conn, session_id: UUID, limit: int = 50) -> list[dict[str, Any]]:
    rows = conn.execute(
        f"SELECT {RUN_COLUMNS} FROM (SELECT * FROM agent_runs WHERE session_id = %s "
        f"ORDER BY queued_at DESC, id DESC LIMIT %s) r ORDER BY queued_at, id",
        (session_id, limit),
    ).fetchall()
    conn.commit()
    return rows


def partial_output(conn: Conn, run_id: UUID) -> str:
    row = conn.execute(
        "SELECT coalesce(string_agg(coalesce(payload->>'raw',payload->>'text'), '' ORDER BY seq), '') AS t FROM agent_run_events "
        "WHERE run_id = %s AND type = 'message.delta'",
        (run_id,),
    ).fetchone()
    conn.commit()
    return checkpoints.extract(str(row["t"]))[0] if row else ""


def is_terminal(conn: Conn, run_id: UUID) -> bool:
    row = conn.execute("SELECT state FROM agent_runs WHERE id = %s", (run_id,)).fetchone()
    conn.commit()
    return row is None or row["state"] in TERMINAL


def expired_running(conn: Conn, runtime_id: UUID) -> list[UUID]:
    """Runs on this runtime that are RUNNING past their persisted deadline and have not been asked to stop."""
    rows = conn.execute(
        "SELECT id FROM agent_runs WHERE runtime_id = %s AND state = 'RUNNING' AND deadline_at < now() "
        "ORDER BY deadline_at, id",
        (runtime_id,),
    ).fetchall()
    conn.commit()
    return [r["id"] for r in rows]


def expire_cancelling(conn: Conn, deadline_s: int) -> int:
    """A stop that Hermes has not confirmed within the deadline ends the run as INTERRUPTED (spec 9: "stop not
    confirmed in time"). The output streamed so far is kept. Returns how many runs were ended."""
    rows = conn.execute(
        "SELECT r.id, r.error_code FROM agent_runs r WHERE r.state = 'CANCELLING' AND coalesce(r.cancel_requested_at, (SELECT max(occurred_at) FROM agent_run_events e "
        "WHERE e.run_id = r.id AND e.type = 'run.state' AND e.payload->>'state' = 'CANCELLING')) "
        "< now() - make_interval(secs => %s)",
        (deadline_s,),
    ).fetchall()
    conn.commit()
    n = 0
    for r in rows:
        ended = finish(
            conn,
            r["id"],
            "INTERRUPTED",
            payload={"reason": "stop_not_confirmed"},
            error_code=r["error_code"]
            or "stop_not_confirmed",  # a deadline stays the stated reason
            output=partial_output(conn, r["id"]),
        )
        n += ended is not None
    return n
