"""002B schema (migration 0011): the ledger's rules are enforced by PostgreSQL itself, including the narrow
maintenance exceptions for "forget" (redact) and "purge"."""

import psycopg
import pytest
from alembic import command
from psycopg.types.json import Jsonb

from paw_server.db.migrations_info import alembic_config

pytestmark = pytest.mark.integration
Check = psycopg.errors.CheckViolation


@pytest.fixture
def conn(db_url):
    command.upgrade(alembic_config(db_url), "head")
    with psycopg.connect(db_url, autocommit=True) as c:
        yield c


def ws(c):
    return c.execute("SELECT id FROM workspaces WHERE slug = 'default'").fetchone()[0]


def session(c, **kw):
    cols = {"owner": "owner", "workspace_id": ws(c), **kw}
    names, ph = ", ".join(cols), ", ".join(["%s"] * len(cols))
    return c.execute(
        f"INSERT INTO agent_sessions ({names}) VALUES ({ph}) RETURNING id", list(cols.values())
    ).fetchone()[0]


def run(c, s, state="SUCCEEDED", req="r1", output="answer"):
    fin = "now()" if state in ("SUCCEEDED", "FAILED", "CANCELLED", "INTERRUPTED") else "NULL"
    return c.execute(
        f"INSERT INTO agent_runs (session_id, actor_id, client_request_id, user_message, state, output, finished_at) "
        f"VALUES (%s, 'owner', %s, 'question', %s, %s, {fin}) RETURNING id",
        (s, req, state, output),
    ).fetchone()[0]


def message(c, s, seq, kind="user", author="owner", run_id=None, status="complete", body="hello"):
    mid = c.execute(
        "INSERT INTO agent_session_messages (session_id, seq, kind, author_actor_id, run_id, status) "
        "VALUES (%s, %s, %s, %s, %s, %s) RETURNING id",
        (s, seq, kind, author, run_id, status),
    ).fetchone()[0]
    c.execute("INSERT INTO agent_message_contents (message_id, body) VALUES (%s, %s)", (mid, body))
    return mid


def maintenance(c, mode):
    """A transaction with the maintenance mode set; it ends with the transaction."""
    tx = c.transaction()
    tx.__enter__()
    c.execute(f"SET LOCAL paw.maintenance = '{mode}'")
    return tx


class Mode:
    def __init__(self, c, mode):
        self.c, self.mode = c, mode

    def __enter__(self):
        self.tx = self.c.transaction()
        self.tx.__enter__()
        self.c.execute(f"SET LOCAL paw.maintenance = '{self.mode}'")
        return self.c

    def __exit__(self, *exc):
        return self.tx.__exit__(*exc)


# ---- actors and sessions -----------------------------------------------------------------------------------------


def test_system_actor_exists_for_platform_events(conn):
    assert conn.execute("SELECT kind FROM actors WHERE id = 'system'").fetchone() == ("system",)
    with pytest.raises(Check):
        conn.execute("INSERT INTO actors (id, kind, display_name) VALUES ('x', 'robot', 'X')")


def test_a_new_chat_has_the_three_roles_and_is_its_own_root(conn):
    s = session(conn)
    row = conn.execute(
        "SELECT owner, requester_actor_id, agent_actor_id, kind, root_session_id = id, title_source, deleted_at "
        "FROM agent_sessions WHERE id = %s",
        (s,),
    ).fetchone()
    assert row == ("owner", "owner", "admin-hermes", "chat", True, "auto", None)


def test_a_delegation_inherits_the_root_of_its_tree(conn):
    top = session(conn)
    r = run(conn, top)
    child = session(conn, kind="delegation", requester_actor_id="admin-hermes", delegated_from_session_id=top,
                    delegated_from_run_id=r)  # fmt: skip
    grandchild = session(
        conn, kind="delegation", requester_actor_id="admin-hermes", delegated_from_session_id=child
    )
    roots = dict(conn.execute("SELECT id, root_session_id FROM agent_sessions").fetchall())
    assert roots[child] == top and roots[grandchild] == top and roots[top] == top


def test_lineage_rules(conn):
    top = session(conn)
    with pytest.raises(Check):  # a delegation must say where it came from
        session(conn, kind="delegation")
    with pytest.raises(Check):  # a chat is never delegated
        session(conn, delegated_from_session_id=top)
    with pytest.raises(Check):  # fork link and fork point go together
        session(conn, forked_from_session_id=top)
    with pytest.raises(Check):
        session(conn, fork_seq=3)
    with pytest.raises(Check):  # a session is a fork or a delegation, never both
        session(
            conn,
            kind="delegation",
            delegated_from_session_id=top,
            forked_from_session_id=top,
            fork_seq=1,
        )
    fork = session(conn, forked_from_session_id=top, fork_seq=2)
    assert conn.execute(
        "SELECT root_session_id FROM agent_sessions WHERE id = %s", (fork,)
    ).fetchone() == (fork,)


def test_a_session_cannot_be_its_own_parent(conn):
    s = session(conn)
    with pytest.raises(Check):
        conn.execute(
            "UPDATE agent_sessions SET forked_from_session_id = id, fork_seq = 1 WHERE id = %s",
            (s,),
        )


@pytest.mark.parametrize(
    "assignment",
    ["owner = 'admin-hermes'", "requester_actor_id = 'admin-hermes'", "agent_actor_id = 'owner'", "kind = 'delegation'",
     "root_session_id = gen_random_uuid()", "created_at = now() - interval '1 day'"],
)  # fmt: skip
def test_roles_and_lineage_are_immutable(conn, assignment):
    s = session(conn)
    with pytest.raises(Check):
        conn.execute(f"UPDATE agent_sessions SET {assignment} WHERE id = %s", (s,))


def test_title_and_activity_can_change_and_deletion_is_one_way(conn):
    s = session(conn)
    conn.execute(
        "UPDATE agent_sessions SET title = 'Trip plans', title_source = 'user', last_activity_at = now() WHERE id = %s",
        (s,),
    )
    with pytest.raises(Check):  # deleted_at and purge_after go together
        conn.execute("UPDATE agent_sessions SET deleted_at = now() WHERE id = %s", (s,))
    conn.execute(
        "UPDATE agent_sessions SET deleted_at = now(), purge_after = now() + interval '30 days' WHERE id = %s",
        (s,),
    )
    with pytest.raises(Check):  # cannot be undeleted
        conn.execute(
            "UPDATE agent_sessions SET deleted_at = NULL, purge_after = NULL WHERE id = %s", (s,)
        )
    with pytest.raises(Check):  # nor its deletion re-dated
        conn.execute(
            "UPDATE agent_sessions SET deleted_at = now() - interval '9 days' WHERE id = %s", (s,)
        )


def test_sessions_are_deleted_only_by_purge(conn):
    s = session(conn)
    with pytest.raises(Check):
        conn.execute("DELETE FROM agent_sessions WHERE id = %s", (s,))
    with Mode(conn, "redact"), pytest.raises(Check):
        conn.execute("DELETE FROM agent_sessions WHERE id = %s", (s,))
    with Mode(conn, "purge"):
        conn.execute("DELETE FROM agent_sessions WHERE id = %s", (s,))
    assert conn.execute("SELECT count(*) FROM agent_sessions").fetchone() == (0,)


def test_the_maintenance_mode_ends_with_its_transaction(conn):
    s = session(conn)
    with Mode(conn, "purge"):
        pass
    with pytest.raises(Check):
        conn.execute("DELETE FROM agent_sessions WHERE id = %s", (s,))


# ---- messages and content -----------------------------------------------------------------------------------------


def test_sequence_is_unique_per_session(conn):
    s, other = session(conn), session(conn)
    message(conn, s, 1)
    with pytest.raises(psycopg.errors.UniqueViolation):
        message(conn, s, 1)
    message(conn, other, 1)


def test_message_kind_rules(conn):
    s = session(conn)
    r = run(conn, s)
    with pytest.raises(Check):
        message(conn, s, 1, kind="chatter")
    with pytest.raises(Check):  # only an assistant answer can be unfinished
        message(conn, s, 1, kind="user", status="unfinished")
    with pytest.raises(Check):  # tool rows always belong to a run
        message(conn, s, 1, kind="tool_result", author="admin-hermes")
    message(conn, s, 1, kind="assistant", author="admin-hermes", run_id=r, status="unfinished")
    message(conn, s, 2, kind="tool_result", author="admin-hermes", run_id=r)
    message(conn, s, 3, kind="platform_event", author="system")
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        message(conn, s, 4, author="ghost")


def test_messages_are_never_updated_and_deleted_only_by_purge(conn):
    s = session(conn)
    m = message(conn, s, 1)
    for sql in ("UPDATE agent_session_messages SET seq = 9 WHERE id = %s", "UPDATE agent_session_messages SET kind = 'assistant' WHERE id = %s",
                "DELETE FROM agent_session_messages WHERE id = %s"):  # fmt: skip
        with pytest.raises(Check):
            conn.execute(sql, (m,))
    with Mode(conn, "redact"), pytest.raises(Check):  # forgetting never removes the message row
        conn.execute("DELETE FROM agent_session_messages WHERE id = %s", (m,))


def test_content_is_never_edited_and_removed_only_by_forget_or_purge(conn):
    s = session(conn)
    m = message(conn, s, 1, body="my password is hunter2")
    with pytest.raises(Check):
        conn.execute(
            "UPDATE agent_message_contents SET body = 'something else' WHERE message_id = %s", (m,)
        )
    with Mode(conn, "redact"), pytest.raises(Check):  # not even in maintenance
        conn.execute("UPDATE agent_message_contents SET body = '' WHERE message_id = %s", (m,))
    with pytest.raises(Check):
        conn.execute("DELETE FROM agent_message_contents WHERE message_id = %s", (m,))
    with Mode(conn, "redact"):
        conn.execute("DELETE FROM agent_message_contents WHERE message_id = %s", (m,))
        conn.execute(
            "INSERT INTO agent_message_redactions (message_id, redacted_by) VALUES (%s, 'owner')",
            (m,),
        )
    # the tombstone: the message and its place in the sequence remain, its text is gone
    assert conn.execute(
        "SELECT seq FROM agent_session_messages WHERE id = %s", (m,)
    ).fetchone() == (1,)
    assert conn.execute("SELECT count(*) FROM agent_message_contents").fetchone() == (0,)
    with pytest.raises(Check):
        conn.execute("DELETE FROM agent_message_redactions WHERE message_id = %s", (m,))


# ---- summaries ----------------------------------------------------------------------------------------------------


def summary(c, s, m, frm=1, to=4):
    return c.execute(
        "INSERT INTO agent_summaries (session_id, from_seq, to_seq, message_id, model, prompt_version, charged_actor_id) "
        "VALUES (%s, %s, %s, %s, 'gpt-4o-mini', 'v1', 'admin-hermes') RETURNING id",
        (s, frm, to, m),
    ).fetchone()[0]


def test_summaries_can_only_be_invalidated_once(conn):
    s = session(conn)
    sm = summary(conn, s, message(conn, s, 5, kind="summary", author="system"))
    with pytest.raises(Check):
        conn.execute("UPDATE agent_summaries SET to_seq = 9 WHERE id = %s", (sm,))
    with pytest.raises(Check):
        conn.execute(
            "UPDATE agent_summaries SET invalidated_at = now(), model = 'other' WHERE id = %s",
            (sm,),
        )
    conn.execute("UPDATE agent_summaries SET invalidated_at = now() WHERE id = %s", (sm,))
    with pytest.raises(Check):
        conn.execute("UPDATE agent_summaries SET invalidated_at = now() WHERE id = %s", (sm,))
    with pytest.raises(Check):
        conn.execute("DELETE FROM agent_summaries WHERE id = %s", (sm,))


def test_summary_range_and_message_rules(conn):
    s = session(conn)
    m = message(conn, s, 5, kind="summary", author="system")
    with pytest.raises(Check):
        summary(conn, s, m, frm=4, to=2)
    summary(conn, s, m)
    with pytest.raises(psycopg.errors.UniqueViolation):  # one summary per summary message
        summary(conn, s, m)


# ---- existing tables gain the same exceptions ---------------------------------------------------------------------


def test_run_events_can_only_be_redacted_to_a_fixed_marker(conn):
    s = session(conn)
    r = run(conn, s)
    conn.execute(
        "INSERT INTO agent_run_events (run_id, seq, type, payload) VALUES (%s, 1, 'message.delta', %s)",
        (r, Jsonb({"text": "secret"})),
    )
    with pytest.raises(Check):
        conn.execute(
            "UPDATE agent_run_events SET payload = '{\"redacted\": true}' WHERE run_id = %s", (r,)
        )
    with Mode(conn, "redact"), pytest.raises(Check):
        conn.execute(
            'UPDATE agent_run_events SET payload = \'{"text": "other"}\' WHERE run_id = %s', (r,)
        )
    with Mode(conn, "redact"), pytest.raises(Check):
        conn.execute(
            "UPDATE agent_run_events SET payload = '{\"redacted\": true}', type = 'x' WHERE run_id = %s",
            (r,),
        )
    with Mode(conn, "redact"):
        conn.execute(
            "UPDATE agent_run_events SET payload = '{\"redacted\": true}' WHERE run_id = %s", (r,)
        )
    assert conn.execute("SELECT payload FROM agent_run_events").fetchone() == ({"redacted": True},)
    with Mode(conn, "redact"), pytest.raises(Check):
        conn.execute("DELETE FROM agent_run_events WHERE run_id = %s", (r,))


def test_a_terminal_run_can_only_have_its_text_blanked(conn):
    s = session(conn)
    r = run(conn, s, output="the secret answer")
    with pytest.raises(Check):
        conn.execute("UPDATE agent_runs SET output = NULL WHERE id = %s", (r,))
    with Mode(conn, "redact"), pytest.raises(Check):  # blank only, never rewrite
        conn.execute("UPDATE agent_runs SET output = 'different' WHERE id = %s", (r,))
    with Mode(conn, "redact"), pytest.raises(Check):
        conn.execute("UPDATE agent_runs SET output = NULL, state = 'FAILED' WHERE id = %s", (r,))
    with Mode(conn, "redact"), pytest.raises(Check):
        conn.execute("UPDATE agent_runs SET user_message = 'edited' WHERE id = %s", (r,))
    with Mode(conn, "redact"):
        conn.execute("UPDATE agent_runs SET output = NULL, user_message = '' WHERE id = %s", (r,))
    assert conn.execute(
        "SELECT user_message, output, state FROM agent_runs WHERE id = %s", (r,)
    ).fetchone() == ("", None, "SUCCEEDED")


def test_runs_and_audit_rows_are_deleted_only_by_purge(conn):
    s = session(conn)
    r = run(conn, s)
    conn.execute(
        "INSERT INTO gateway_tool_invocations (run_id, tool, decision, reason_code, trace_id) VALUES (%s, 'docs_search', 'allowed', 'OK', 't')",
        (r,),
    )
    for sql in ("DELETE FROM gateway_tool_invocations", "DELETE FROM agent_runs"):
        with pytest.raises(Check):
            conn.execute(sql)
    with Mode(conn, "purge"):
        conn.execute("DELETE FROM gateway_tool_invocations")
        conn.execute("DELETE FROM agent_runs")
    assert conn.execute("SELECT count(*) FROM agent_runs").fetchone() == (0,)


def test_live_run_transitions_are_still_enforced(conn):
    s = session(conn)
    r = run(conn, s, state="QUEUED", output=None)
    with pytest.raises(Check):
        conn.execute(
            "UPDATE agent_runs SET state = 'SUCCEEDED', finished_at = now() WHERE id = %s", (r,)
        )
    with (
        Mode(conn, "redact"),
        pytest.raises(Check),
    ):  # redact mode is not a way around the state machine
        conn.execute("UPDATE agent_runs SET state = 'RUNNING' WHERE id = %s", (r,))


def test_deletion_log_shape(conn):
    s = session(conn)
    conn.execute(
        "INSERT INTO data_deletions (scope, session_id, requested_by) VALUES ('session', %s, 'owner')",
        (s,),
    )
    with pytest.raises(Check):  # a message deletion names the message
        conn.execute(
            "INSERT INTO data_deletions (scope, session_id, requested_by) VALUES ('message', %s, 'owner')",
            (s,),
        )
    # no foreign keys: the log outlives what it deleted
    conn.execute(
        "INSERT INTO data_deletions (scope, session_id, requested_by) VALUES ('session', gen_random_uuid(), 'owner')"
    )


# ---- the migration keeps existing chats -----------------------------------------------------------------------------


def test_existing_chats_are_backfilled_into_the_ledger(db_url):
    cfg = alembic_config(db_url)
    command.upgrade(cfg, "0010")
    with psycopg.connect(db_url, autocommit=True) as c:
        w = c.execute("SELECT id FROM workspaces LIMIT 1").fetchone()[0]
        s = c.execute(
            "INSERT INTO agent_sessions (owner, workspace_id) VALUES ('owner', %s) RETURNING id",
            (w,),
        ).fetchone()[0]
        ins = ("INSERT INTO agent_runs (session_id, actor_id, client_request_id, user_message, state, output, queued_at, "
               "finished_at) VALUES (%s, 'owner', %s, %s, %s, %s, now() + make_interval(secs => %s), %s)")  # fmt: skip
        c.execute(
            ins,
            (
                s,
                "a",
                "My   favourite colour\nis teal and here is more text to cut",
                "SUCCEEDED",
                "Noted.",
                1,
                "now()",
            ),
        )
        c.execute(ins, (s, "b", "And?", "INTERRUPTED", "Half an ans", 2, "now()"))
        c.execute(ins, (s, "c", "Third", "FAILED", None, 3, "now()"))
    command.upgrade(cfg, "head")
    with psycopg.connect(db_url, autocommit=True) as c:
        rows = c.execute(
            "SELECT m.seq, m.kind, m.author_actor_id, m.status, b.body FROM agent_session_messages m "
            "JOIN agent_message_contents b ON b.message_id = m.id ORDER BY m.seq"
        ).fetchall()
        title = c.execute("SELECT title FROM agent_sessions").fetchone()[0]
    assert rows == [
        (
            1,
            "user",
            "owner",
            "complete",
            "My   favourite colour\nis teal and here is more text to cut",
        ),
        (2, "assistant", "admin-hermes", "complete", "Noted."),
        (3, "user", "owner", "complete", "And?"),
        (4, "assistant", "admin-hermes", "unfinished", "Half an ans"),
        (5, "user", "owner", "complete", "Third"),
    ]
    assert title == "My favourite colour is teal and here is more text to cut"[:60]


def test_downgrade_roundtrip(db_url):
    cfg = alembic_config(db_url)
    command.upgrade(cfg, "head")
    command.downgrade(cfg, "0010")
    command.upgrade(cfg, "head")


def test_one_user_and_assistant_message_per_run(conn):
    s = session(conn)
    r = run(conn, s)
    message(conn, s, 1, run_id=r)
    with pytest.raises(psycopg.errors.UniqueViolation):
        message(conn, s, 2, run_id=r)
    message(conn, s, 2, kind="assistant", author="admin-hermes", run_id=r)
    with pytest.raises(psycopg.errors.UniqueViolation):
        message(conn, s, 3, kind="assistant", author="admin-hermes", run_id=r)


def test_forgotten_content_cannot_be_reinserted(conn):
    s = session(conn)
    mid = message(conn, s, 1)
    with Mode(conn, "redact"):
        conn.execute("DELETE FROM agent_message_contents WHERE message_id = %s", (mid,))
        conn.execute(
            "INSERT INTO agent_message_redactions (message_id, redacted_by) VALUES (%s, 'owner')",
            (mid,),
        )
    with pytest.raises(Check):
        conn.execute(
            "INSERT INTO agent_message_contents (message_id, body) VALUES (%s, 'restored')", (mid,)
        )
