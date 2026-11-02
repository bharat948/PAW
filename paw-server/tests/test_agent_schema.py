"""002A schema invariants are enforced by PostgreSQL itself, not only by application code."""

import psycopg
import pytest
from alembic import command

from paw_server.db.migrations_info import alembic_config

pytestmark = pytest.mark.integration

TERMINAL = ("SUCCEEDED", "FAILED", "CANCELLED", "INTERRUPTED")


@pytest.fixture
def conn(db_url):
    command.upgrade(alembic_config(db_url), "head")
    with psycopg.connect(db_url, autocommit=True) as c:
        yield c


def _session(c) -> str:
    ws = c.execute("SELECT id FROM workspaces WHERE slug='default'").fetchone()[0]
    return str(
        c.execute(
            "INSERT INTO agent_sessions (owner, workspace_id) VALUES ('owner', %s) RETURNING id",
            (ws,),
        ).fetchone()[0]
    )


def _run(c, session, req="r1", state="QUEUED") -> str:
    fin = "now()" if state in TERMINAL else "NULL"
    return str(
        c.execute(
            f"INSERT INTO agent_runs (session_id, actor_id, client_request_id, user_message, state, finished_at) "
            f"VALUES (%s, 'owner', %s, 'hi', %s, {fin}) RETURNING id",
            (session, req, state),
        ).fetchone()[0]
    )


def _set(c, run, state):
    fin = ", finished_at = now()" if state in TERMINAL else ""
    c.execute(f"UPDATE agent_runs SET state=%s{fin} WHERE id=%s", (state, run))


def test_downgrade_and_upgrade_roundtrip(db_url):
    cfg = alembic_config(db_url)
    command.upgrade(cfg, "head")
    command.downgrade(cfg, "0005")
    command.upgrade(cfg, "head")


def test_one_non_terminal_run_per_session(conn):
    s = _session(conn)
    _run(conn, s, "a")
    with pytest.raises(psycopg.errors.UniqueViolation):
        _run(conn, s, "b")


def test_new_run_allowed_once_previous_is_terminal(conn):
    s = _session(conn)
    r = _run(conn, s, "a")
    _set(conn, r, "DISPATCHING")
    _set(conn, r, "FAILED")
    _run(conn, s, "b")
    other = _session(conn)
    _run(conn, other, "a")  # another session is independent


def test_client_request_id_unique_per_session(conn):
    s = _session(conn)
    r = _run(conn, s, "same")
    _set(conn, r, "CANCELLED")
    with pytest.raises(psycopg.errors.UniqueViolation):
        _run(conn, s, "same")


LEGAL = [
    ("QUEUED", "DISPATCHING"),
    ("QUEUED", "CANCELLED"),
    ("DISPATCHING", "RUNNING"),
    ("DISPATCHING", "FAILED"),
    ("DISPATCHING", "INTERRUPTED"),
    ("RUNNING", "SUCCEEDED"),
    ("RUNNING", "FAILED"),
    ("RUNNING", "CANCELLING"),
    ("RUNNING", "INTERRUPTED"),
    ("CANCELLING", "CANCELLED"),
    ("CANCELLING", "INTERRUPTED"),
]
PATH = {
    "QUEUED": [],
    "DISPATCHING": ["DISPATCHING"],
    "RUNNING": ["DISPATCHING", "RUNNING"],
    "CANCELLING": ["DISPATCHING", "RUNNING", "CANCELLING"],
}


@pytest.mark.parametrize(("src", "dst"), LEGAL)
def test_legal_transitions(conn, src, dst):
    r = _run(conn, _session(conn))
    for step in PATH[src]:
        _set(conn, r, step)
    _set(conn, r, dst)
    assert conn.execute("SELECT state FROM agent_runs WHERE id=%s", (r,)).fetchone() == (dst,)


ALL = ("QUEUED", "DISPATCHING", "RUNNING", "CANCELLING", *TERMINAL)


@pytest.mark.parametrize(
    ("src", "dst"),
    [(s, d) for s in PATH for d in ALL if d != s and (s, d) not in LEGAL],
)
def test_illegal_transitions_rejected(conn, src, dst):
    r = _run(conn, _session(conn))
    for step in PATH[src]:
        _set(conn, r, step)
    with pytest.raises(psycopg.errors.CheckViolation):
        _set(conn, r, dst)


@pytest.mark.parametrize("terminal", TERMINAL)
def test_terminal_runs_are_immutable(conn, terminal):
    r = _run(conn, _session(conn), state=terminal)
    with pytest.raises(psycopg.errors.CheckViolation):
        conn.execute("UPDATE agent_runs SET output='tampered' WHERE id=%s", (r,))
    with pytest.raises(psycopg.errors.CheckViolation):
        _set(conn, r, "RUNNING")


def test_finished_at_must_match_terminal_state(conn):
    s = _session(conn)
    with pytest.raises(psycopg.errors.CheckViolation):
        conn.execute(
            "INSERT INTO agent_runs (session_id, actor_id, client_request_id, user_message, state) "
            "VALUES (%s,'owner','x','hi','FAILED')",
            (s,),
        )


def test_unknown_state_rejected(conn):
    with pytest.raises(psycopg.errors.CheckViolation):
        _run(conn, _session(conn), state="DONE")


def test_events_unique_seq_and_source_id_and_append_only(conn):
    r = _run(conn, _session(conn))
    ins = "INSERT INTO agent_run_events (run_id, seq, type, source_event_id) VALUES (%s,%s,'message.delta',%s)"
    conn.execute(ins, (r, 1, "e1"))
    with pytest.raises(psycopg.errors.UniqueViolation):
        conn.execute(ins, (r, 1, "e2"))  # same seq
    with pytest.raises(psycopg.errors.UniqueViolation):
        conn.execute(ins, (r, 2, "e1"))  # replayed source event
    conn.execute(ins, (r, 2, None))
    conn.execute(ins, (r, 3, None))  # NULL source ids never collide
    with pytest.raises(psycopg.errors.CheckViolation):
        conn.execute("UPDATE agent_run_events SET type='x' WHERE run_id=%s", (r,))
    with pytest.raises(psycopg.errors.CheckViolation):
        conn.execute("DELETE FROM agent_run_events WHERE run_id=%s", (r,))
    with pytest.raises(psycopg.errors.CheckViolation):
        conn.execute(ins, (r, 0, None))


def test_lease_is_exclusive_per_runtime_and_per_run(conn):
    rt = conn.execute(
        "INSERT INTO agent_runtimes (name) VALUES ('hermes') RETURNING id"
    ).fetchone()[0]
    a = _run(conn, _session(conn))
    b = _run(conn, _session(conn))
    conn.execute("INSERT INTO agent_run_leases (runtime_id, run_id) VALUES (%s,%s)", (rt, a))
    with pytest.raises(psycopg.errors.UniqueViolation):
        conn.execute("INSERT INTO agent_run_leases (runtime_id, run_id) VALUES (%s,%s)", (rt, b))


def test_rate_card_rejects_negative_prices(conn):
    with pytest.raises(psycopg.errors.CheckViolation):
        conn.execute("INSERT INTO ops_rate_cards VALUES ('v1','m',-1,1,'2026-01-01')")
