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
    "id, session_id, actor_id, executing_actor_id, client_request_id, user_message, state, hermes_run_id, requested_model, "
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
    conn: Conn, *, owner: str, workspace_id: UUID, title: str = ""
) -> dict[str, Any]:
    row = conn.execute(
        "INSERT INTO agent_sessions (owner, workspace_id, title) VALUES (%s, %s, %s) "
        "RETURNING id, title, status, created_at",
        (owner, workspace_id, title[:200]),
    ).fetchone()
    conn.commit()
    assert row is not None
    return row


def get_session(conn: Conn, session_id: UUID, owner: str) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT id, title, status, created_at FROM agent_sessions WHERE id = %s AND owner = %s",
        (session_id, owner),
    ).fetchone()
    conn.commit()
    return row


def list_sessions(conn: Conn, owner: str, limit: int = 30) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT s.id, s.title, s.status, s.created_at, "
        "(SELECT max(queued_at) FROM agent_runs r WHERE r.session_id = s.id) AS last_run_at "
        "FROM agent_sessions s WHERE s.owner = %s AND s.status = 'active' "
        "ORDER BY coalesce((SELECT max(queued_at) FROM agent_runs r WHERE r.session_id = s.id), s.created_at) DESC "
        "LIMIT %s",
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
    prior = _existing(conn, session_id, client_request_id)
    conn.commit()
    if prior is not None:
        if prior["user_message"] != message:
            raise AdmissionError(
                "conflict"
            )  # same key, different message: a client bug, not a retry
        return prior, False
    queued = conn.execute("SELECT count(*) AS n FROM agent_runs WHERE state = 'QUEUED'").fetchone()
    conn.commit()
    if queued is not None and queued["n"] >= queue_limit:
        raise AdmissionError("full")
    try:
        row = conn.execute(
            f"INSERT INTO agent_runs (session_id, actor_id, client_request_id, user_message, requested_model) "
            f"VALUES (%s, %s, %s, %s, %s) RETURNING {RUN_COLUMNS}",
            (session_id, actor_id, client_request_id, message, model),
        ).fetchone()
        conn.commit()
    except psycopg.errors.UniqueViolation:
        conn.rollback()
        prior = _existing(
            conn, session_id, client_request_id
        )  # a concurrent duplicate won the race
        conn.commit()
        if prior is not None:
            return prior, False
        raise AdmissionError("busy") from None
    assert row is not None
    append_event(conn, row["id"], "run.queued", {}, None)
    return row, True


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
    seq = _insert_event(conn, run_id, type_, payload, source_event_id)
    conn.commit()
    return seq


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
        nxt = conn.execute(
            "SELECT id FROM agent_runs WHERE state = 'QUEUED' ORDER BY queued_at, id "
            "FOR UPDATE SKIP LOCKED LIMIT 1"
        ).fetchone()
        if nxt is None:
            return None
        lease = conn.execute(
            "INSERT INTO agent_run_leases (runtime_id, run_id) VALUES (%s, %s) "
            "ON CONFLICT (runtime_id) DO NOTHING RETURNING run_id",
            (runtime_id, nxt["id"]),
        ).fetchone()
        if lease is None:
            return None  # the runtime is busy
        run = conn.execute(
            f"UPDATE agent_runs SET state = 'DISPATCHING', runtime_id = %s, started_at = now() "
            f"WHERE id = %s RETURNING {RUN_COLUMNS}",
            (runtime_id, nxt["id"]),
        ).fetchone()
        assert run is not None
    append_event(conn, run["id"], "run.state", {"state": "DISPATCHING"}, None)
    return run


def bind_profile(
    conn: Conn, run_id: UUID, *, profile_version_id: UUID, version: int, compiled_hash: str
) -> None:
    """Record which profile version a run uses. Set once at claim; a later activation never changes it."""
    conn.execute(
        "UPDATE agent_runs SET profile_version_id = %s, profile_version = %s, compiled_hash = %s "
        "WHERE id = %s AND profile_version_id IS NULL",
        (profile_version_id, version, compiled_hash, run_id),
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
        cur = conn.execute(
            "SELECT state FROM agent_runs WHERE id = %s FOR UPDATE", (run_id,)
        ).fetchone()
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
        sets = ", ".join(["state = %s", "finished_at = now()", *(f"{k} = %s" for k in fields)])
        conn.execute(f"UPDATE agent_runs SET {sets} WHERE id = %s", (to, *fields.values(), run_id))
        _insert_event(conn, run_id, etype, payload or {}, source_event_id)
        conn.execute("DELETE FROM agent_run_leases WHERE run_id = %s", (run_id,))
    return to


def request_cancel(conn: Conn, run_id: UUID) -> tuple[str, str | None]:
    """(outcome, hermes_run_id). outcome: cancelled (was queued) · stopping (stop must now be sent) ·
    already (cancelling or finished) · not_yet (dispatching: retry in a moment)."""
    with conn.transaction():
        row = conn.execute(
            "SELECT state, hermes_run_id FROM agent_runs WHERE id = %s FOR UPDATE", (run_id,)
        ).fetchone()
        if row is None:
            return "missing", None
        state = row["state"]
        if state == "QUEUED":
            pass
        elif state == "RUNNING":
            conn.execute("UPDATE agent_runs SET state = 'CANCELLING' WHERE id = %s", (run_id,))
            _insert_event(conn, run_id, "run.state", {"state": "CANCELLING"}, None)
            return "stopping", row["hermes_run_id"]
        elif state == "DISPATCHING":
            return "not_yet", None
        else:
            return "already", row["hermes_run_id"]
    finish(conn, run_id, "CANCELLED")
    return "cancelled", None


# ---- reads and recovery -------------------------------------------------------------------------------------
def get_run(conn: Conn, run_id: UUID, owner: str) -> dict[str, Any] | None:
    row = conn.execute(
        f"SELECT {RUN_COLUMNS} FROM agent_runs WHERE id = %s AND session_id IN "
        f"(SELECT id FROM agent_sessions WHERE owner = %s)",
        (run_id, owner),
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


def history_for(conn: Conn, session_id: UUID, before_run_id: UUID) -> list[dict[str, str]]:
    """Compact text history rebuilt from PostgreSQL: previous completed turns of this session only (002B refines it)."""
    rows = conn.execute(
        "SELECT user_message, output FROM agent_runs WHERE session_id = %s AND state = 'SUCCEEDED' AND id <> %s "
        "AND queued_at < (SELECT queued_at FROM agent_runs WHERE id = %s) ORDER BY queued_at, id",
        (session_id, before_run_id, before_run_id),
    ).fetchall()
    conn.commit()
    turns: list[dict[str, str]] = []
    for r in rows:
        turns += [
            {"role": "user", "content": r["user_message"]},
            {"role": "assistant", "content": r["output"] or ""},
        ]
    return turns


def partial_output(conn: Conn, run_id: UUID) -> str:
    row = conn.execute(
        "SELECT coalesce(string_agg(payload->>'text', '' ORDER BY seq), '') AS t FROM agent_run_events "
        "WHERE run_id = %s AND type = 'message.delta'",
        (run_id,),
    ).fetchone()
    conn.commit()
    return str(row["t"]) if row else ""


def is_terminal(conn: Conn, run_id: UUID) -> bool:
    row = conn.execute("SELECT state FROM agent_runs WHERE id = %s", (run_id,)).fetchone()
    conn.commit()
    return row is None or row["state"] in TERMINAL


def expire_cancelling(conn: Conn, deadline_s: int) -> int:
    """A stop that Hermes has not confirmed within the deadline ends the run as INTERRUPTED (spec 9: "stop not
    confirmed in time"). The output streamed so far is kept. Returns how many runs were ended."""
    rows = conn.execute(
        "SELECT r.id FROM agent_runs r WHERE r.state = 'CANCELLING' AND (SELECT max(occurred_at) FROM agent_run_events e "
        "WHERE e.run_id = r.id AND e.type = 'run.state' AND e.payload->>'state' = 'CANCELLING') "
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
            error_code="stop_not_confirmed",
            output=partial_output(conn, r["id"]),
        )
        n += ended is not None
    return n


def recover_interrupted(conn: Conn) -> int:
    """Run at worker start. With one worker, any run still DISPATCHING/RUNNING/CANCELLING belonged to a worker that
    died: it becomes INTERRUPTED with whatever output was streamed. Never replayed, never reported as success."""
    rows = conn.execute(
        "SELECT id FROM agent_runs WHERE state IN ('DISPATCHING', 'RUNNING', 'CANCELLING')"
    ).fetchall()
    conn.commit()
    for r in rows:
        finish(
            conn,
            r["id"],
            "INTERRUPTED",
            payload={"reason": "worker_restart"},
            error_code="worker_restart",
            output=partial_output(conn, r["id"]),
        )
    return len(rows)
