from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from tests.conftest import AUTH
from tests.test_agent_integration import Harness, sql

from paw_server.modules.sessions import privacy
from paw_server.modules.sessions.journal import Journal, Receipt

pytestmark = pytest.mark.integration


@pytest.fixture
def h(env):
    x = Harness(env)
    yield x
    x.pool.close()


def history(h, sid):
    r = h.c.get(f"/v1/agent-sessions/{sid}/messages", headers=AUTH)
    assert r.status_code == 200, r.text
    return r.json()


def completed_chat(h):
    sid = h.session()
    run = h.send(sid, message="remember teal")
    h.worker.run_once()
    return sid, run, history(h, sid)["messages"]


def make_fork(h, sid, mid, key="edit", message="replacement"):
    return h.c.post(
        f"/v1/agent-sessions/{sid}/fork",
        headers=AUTH,
        json={"message_id": mid, "client_request_id": key, "message": message},
    )


def test_conversation_rename_list_messages_and_owner_activity(h):
    sid, run, msgs = completed_chat(h)
    assert (
        h.c.post(
            f"/v1/agent-sessions/{sid}/rename", headers=AUTH, json={"title": "Investigation"}
        ).status_code
        == 200
    )
    sessions = h.c.get("/v1/agent-sessions", headers=AUTH).json()["sessions"]
    assert (
        sessions[0]["title"] == "Investigation" and sessions[0]["agent_actor_id"] == "admin-hermes"
    )
    page = h.c.get(f"/v1/agent-sessions/{sid}/messages?limit=1", headers=AUTH).json()
    assert len(page["messages"]) == 1 and page["next_after"] == 1
    page2 = h.c.get(f"/v1/agent-sessions/{sid}/messages?after=1", headers=AUTH).json()
    assert page2["messages"][0]["kind"] == "assistant"
    activity = h.c.get("/v1/agent-activity?limit=1", headers=AUTH).json()
    more = h.c.get(f"/v1/agent-activity?cursor={activity['next_cursor']}", headers=AUTH).json()
    assert len(more["messages"]) == 1


def test_edit_forks_before_original_message_and_is_idempotent(h):
    sid, _, msgs = completed_chat(h)
    second = h.send(sid, message="original question")
    h.worker.run_once()
    pivot = next(
        m
        for m in history(h, sid)["messages"]
        if m["run_id"] == second["id"] and m["kind"] == "user"
    )
    fork = make_fork(h, sid, pivot["id"])
    assert fork.status_code == 201, fork.text
    child = fork.json()["session"]["id"]
    duplicate = make_fork(h, sid, pivot["id"])
    assert duplicate.status_code == 201 and duplicate.json()["session"]["id"] == child
    assert make_fork(h, sid, pivot["id"], message="different").status_code == 409
    assert [m["body"] for m in history(h, child)["messages"]] == [
        msgs[0]["body"],
        msgs[1]["body"],
        "replacement",
    ]
    h.worker.run_once()
    assert h.rt.created[-1]["history"] == [
        {"role": "user", "content": "remember teal"},
        {"role": "assistant", "content": "hello"},
    ]


def test_forget_removes_run_copies_and_invalidates_inherited_history(h, env):
    sid, run, msgs = completed_chat(h)
    later = h.send(sid, message="later")
    h.worker.run_once()
    pivot = next(
        m for m in history(h, sid)["messages"] if m["run_id"] == later["id"] and m["kind"] == "user"
    )
    child = make_fork(h, sid, pivot["id"]).json()["session"]["id"]
    r = h.c.post(f"/v1/agent-sessions/{sid}/messages/{msgs[0]['id']}/forget", headers=AUTH)
    assert r.status_code == 200, r.text
    assert all(m["body"] is None for m in history(h, sid)["messages"] if m["run_id"] == run["id"])
    assert history(h, child)["messages"][0]["forgotten"]
    assert sql(env, "SELECT user_message,output FROM agent_runs WHERE id=%s", run["id"]) == [
        ("", None)
    ]
    assert all(
        p[0] == {"redacted": True}
        for p in sql(env, "SELECT payload FROM agent_run_events WHERE run_id=%s", run["id"])
    )
    assert len(Journal(h.settings.deletion_journal_dir).receipts()) == 1


def test_delete_hides_runs_and_requires_explicit_child_choice(h):
    sid, run, _ = completed_chat(h)
    assert h.c.post(f"/v1/agent-sessions/{sid}/delete", headers=AUTH, json={}).status_code == 422
    r = h.c.post(
        f"/v1/agent-sessions/{sid}/delete", headers=AUTH, json={"include_delegated": False}
    )
    assert r.status_code == 200
    assert h.c.get(f"/v1/agent-runs/{run['id']}", headers=AUTH).status_code == 404
    assert h.c.get(f"/v1/agent-sessions/{sid}/messages", headers=AUTH).status_code == 404
    assert h.c.get("/v1/agent-sessions", headers=AUTH).json()["sessions"] == []


def test_purge_preserves_fork_prefix_then_forget_propagates_to_materialized_copy(h, env):
    sid, _, msgs = completed_chat(h)
    last = h.send(sid, message="branch here")
    h.worker.run_once()
    pivot = next(
        m for m in history(h, sid)["messages"] if m["run_id"] == last["id"] and m["kind"] == "user"
    )
    child = make_fork(h, sid, pivot["id"]).json()["session"]["id"]
    h.worker.run_once()
    h.c.post(f"/v1/agent-sessions/{sid}/delete", headers=AUTH, json={"include_delegated": False})
    sql(env, "UPDATE agent_sessions SET purge_after=now()-interval '1 day' WHERE id=%s", sid)
    with h.pool.connection() as c:
        assert privacy.purge_due(c) == 1
    retained = history(h, child)["messages"]
    assert (
        retained[0]["body"] == "remember teal" and retained[0]["source_message_id"] == msgs[0]["id"]
    )
    r = h.c.post(f"/v1/agent-sessions/{child}/messages/{retained[0]['id']}/forget", headers=AUTH)
    assert r.status_code == 200, r.text
    assert all(m["forgotten"] for m in history(h, child)["messages"][:2])
    assert sql(env, "SELECT count(*) FROM agent_sessions WHERE id=%s", sid) == [(0,)]


def test_receipt_survives_database_rollback_and_is_replayed_before_read(h):
    sid, _, msgs = completed_chat(h)
    journal = Journal(h.settings.deletion_journal_dir)
    with pytest.raises(RuntimeError), h.pool.connection() as c, c.transaction():
        ws = c.execute("SELECT workspace_id FROM agent_sessions WHERE id=%s", (sid,)).fetchone()[
            "workspace_id"
        ]
        privacy.request(
            c,
            journal,
            session_id=UUID(sid),
            owner="owner",
            workspace_id=ws,
            message_id=UUID(msgs[0]["id"]),
        )
        raise RuntimeError("crash before commit")
    assert history(h, sid)["messages"][0]["forgotten"]


def test_restore_reapplies_deletion_after_old_content_is_restored(h, env):
    sid, run, msgs = completed_chat(h)
    with h.pool.connection() as c:
        ws = c.execute("SELECT workspace_id FROM agent_sessions WHERE id=%s", (sid,)).fetchone()[
            "workspace_id"
        ]
        c.commit()
    receipt = Receipt(
        id=uuid4(),
        scope="message",
        session_id=UUID(sid),
        workspace_id=ws,
        message_id=UUID(msgs[0]["id"]),
        run_ids=[UUID(run["id"])],
        requested_by="owner",
        requested_at=datetime.now(UTC),
    )
    journal = Journal(h.settings.deletion_journal_dir)
    with journal.guard():
        journal.write(receipt)
    # This database is the old snapshot, with original content still present.
    assert sql(
        env, "SELECT count(*) FROM agent_message_contents WHERE message_id=%s", msgs[0]["id"]
    ) == [(1,)]
    with h.pool.connection() as c:
        assert privacy.replay(c, journal, force=True) == 1
        assert privacy.replay(c, journal) == 0
    assert history(h, sid)["messages"][0]["forgotten"]


def test_unknown_or_cross_workspace_targets_are_not_mutated(h, env):
    sid, _, msgs = completed_chat(h)
    assert (
        h.c.post(f"/v1/agent-sessions/{sid}/messages/{uuid4()}/forget", headers=AUTH).status_code
        == 404
    )
    sql(env, "UPDATE workspaces SET owner_actor_id='other' WHERE slug='default'")
    assert h.c.get(f"/v1/agent-sessions/{sid}/messages", headers=AUTH).status_code != 200


def test_receipt_written_after_assembly_prevents_dispatch(h, env, monkeypatch):
    from paw_server.modules.sessions import assembler

    sid, _, msgs = completed_chat(h)
    run = h.send(sid, message="continue")
    original = assembler.assemble
    journal = Journal(h.settings.deletion_journal_dir)

    def assemble_then_forget(conn, *args, **kwargs):
        result = original(conn, *args, **kwargs)
        session = conn.execute("SELECT * FROM agent_sessions WHERE id=%s", (sid,)).fetchone()
        receipt = Receipt(
            id=uuid4(),
            scope="message",
            session_id=UUID(sid),
            workspace_id=session["workspace_id"],
            message_id=UUID(msgs[0]["id"]),
            requested_by=session["owner"],
            requested_at=datetime.now(UTC),
        )
        with journal.guard():
            journal.write(receipt)
        return result

    monkeypatch.setattr(assembler, "assemble", assemble_then_forget)
    before = len(h.rt.created)
    assert h.worker.run_once()
    assert len(h.rt.created) == before
    assert h.run(run["id"])["state"] == "INTERRUPTED"
    assert history(h, sid)["messages"][0]["forgotten"]


def test_conversation_controls_require_csrf_for_cookie_sessions(env):
    from tests.runtime.fakes import FakeRuntime
    from tests.test_auth_integration import FakeGoogle, csrf, make, sign_in

    google = FakeGoogle()
    c = make(env, google, runtime=FakeRuntime())
    try:
        assert sign_in(c, google)[0].status_code == 302
        headers = csrf(c)
        response = c.post("/v1/agent-sessions", headers=headers, json={"title": "Private"})
        assert response.status_code == 201, response.text
        sid = response.json()["id"]
        operations = [
            (f"/v1/agent-sessions/{sid}/rename", {"title": "Changed"}),
            (
                f"/v1/agent-sessions/{sid}/fork",
                {"message_id": str(uuid4()), "client_request_id": "edit", "message": "edit"},
            ),
            (f"/v1/agent-sessions/{sid}/messages/{uuid4()}/forget", None),
            (f"/v1/agent-sessions/{sid}/delete", {"include_delegated": False}),
        ]
        for url, body in operations:
            assert c.post(url, json=body).status_code == 403
        assert c.post(operations[0][0], json=operations[0][1], headers=headers).status_code == 200
        c.cookies.clear()
        assert c.get(f"/v1/agent-sessions/{sid}/messages").status_code == 401
        assert c.get("/v1/agent-activity").status_code == 401
    finally:
        c.__exit__(None, None, None)


@pytest.mark.parametrize("include", [False, True])
def test_delete_requires_and_respects_delegated_children_choice(h, env, include):
    sid, run, _ = completed_chat(h)
    child = str(uuid4())
    sql(
        env,
        "INSERT INTO agent_sessions (id,owner,workspace_id,requester_actor_id,agent_actor_id,kind,delegated_from_session_id,delegated_from_run_id) SELECT %s,owner,workspace_id,'admin-hermes',agent_actor_id,'delegation',id,%s FROM agent_sessions WHERE id=%s",
        child,
        run["id"],
        sid,
    )
    assert h.c.post(f"/v1/agent-sessions/{sid}/delete", headers=AUTH, json={}).status_code == 422
    response = h.c.post(
        f"/v1/agent-sessions/{sid}/delete", headers=AUTH, json={"include_delegated": include}
    )
    assert response.status_code == 200, response.text
    assert sql(env, "SELECT deleted_at IS NOT NULL FROM agent_sessions WHERE id=%s", child) == [
        (include,)
    ]


def test_forget_discards_tool_result_returning_after_the_request(h, env):
    from dataclasses import replace

    from tests.gateway.test_service import start_run

    from paw_server.modules.gateway.builtin import builtin_specs
    from paw_server.modules.gateway.registry import ToolRegistry, sync_registry
    from paw_server.modules.gateway.service import GatewayService

    run = start_run(h)
    sid = run["session_id"]
    mid = history(h, sid)["messages"][0]["id"]

    def handler(ctx, args):
        response = h.c.post(f"/v1/agent-sessions/{sid}/messages/{mid}/forget", headers=AUTH)
        assert response.status_code == 200
        return {"private": "late private result"}

    reg = ToolRegistry([replace(spec, handler=handler) for spec in builtin_specs()])
    with h.pool.connection() as conn:
        sync_registry(conn, reg)
    outcome = GatewayService(h.pool, reg, h.settings).handle_call("paw_system_status", {})
    assert outcome.decision == "denied" and outcome.reason == "CONTEXT_CHANGED"
    assert "late private result" not in str(history(h, sid))
