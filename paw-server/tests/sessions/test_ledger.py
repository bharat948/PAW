import threading
from uuid import UUID

import pytest
from tests.runtime.fakes import completed, delta
from tests.test_agent_integration import Harness, sql

from paw_server.modules.runtime import lifecycle as lc
from paw_server.modules.sessions import service as ledger
from paw_server.modules.sessions.checkpoints import CLOSE, OPEN

pytestmark = pytest.mark.integration


@pytest.fixture
def h(env):
    x = Harness(env)
    yield x
    x.pool.close()


def test_admission_and_completion_create_exactly_one_message_each(h, env):
    sid = h.session()
    run = h.send(sid, cid="same")
    h.send(sid, cid="same", expect=200)
    h.worker.run_once()
    with h.pool.connection() as c:
        lc.finish(c, UUID(run["id"]), "SUCCEEDED", output="duplicate")
        rows = ledger.messages(c, UUID(sid))
    assert [(r["seq"], r["kind"], r["body"]) for r in rows] == [
        (1, "user", "hello"),
        (2, "assistant", "hello"),
    ]
    assert sql(env, "SELECT assembly_hash IS NOT NULL FROM agent_runs") == [(True,)]


def test_failed_transaction_rolls_back_message_and_run(h, env):
    sid = UUID(h.session())
    with h.pool.connection() as c:
        with pytest.raises(RuntimeError), c.transaction():
            lc.admit(
                c,
                session_id=sid,
                actor_id="owner",
                client_request_id="rollback",
                message="not committed",
                queue_limit=10,
                model="test-model",
            )
            raise RuntimeError("rollback")
    assert sql(env, "SELECT count(*) FROM agent_session_messages") == [(0,)]
    assert sql(env, "SELECT count(*) FROM agent_runs") == [(0,)]
    assert sql(env, "SELECT count(*) FROM agent_run_events") == [(0,)]


def test_concurrent_appends_have_contiguous_sequences(h, env):
    sid = UUID(h.session())
    errors = []

    def write(i):
        try:
            with h.pool.connection() as c, c.transaction():
                ledger.append(c, sid, "platform_event", "system", str(i))
        except Exception as e:
            errors.append(e)

    threads = [threading.Thread(target=write, args=(i,)) for i in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    assert sql(env, "SELECT seq FROM agent_session_messages ORDER BY seq") == [
        (i,) for i in range(1, 21)
    ]


def test_cancel_keeps_partial_text_as_unfinished(h, env):
    run = h.send(h.session())
    with h.pool.connection() as c:
        lc.claim_next(c, h.worker.runtime_id)
        lc.move(c, UUID(run["id"]), "RUNNING", hermes_run_id="h")
        lc.append_event(c, UUID(run["id"]), "message.delta", {"text": "half answer"}, "h:0")
        lc.request_cancel(c, UUID(run["id"]))
        lc.finish(c, UUID(run["id"]), "CANCELLED")
    assert sql(
        env,
        "SELECT b.body, m.status FROM agent_session_messages m JOIN agent_message_contents b ON b.message_id=m.id WHERE m.kind='assistant'",
    ) == [("half answer", "unfinished")]


def test_task_checkpoint_is_private_and_has_only_valid_evidence(h, env):
    cp = '{"task_id":"investigate","status":"completed","goal":"investigate","summary":"done","evidence":["invented"],"next_steps":[]}'
    text = "Found it." + OPEN + cp + CLOSE
    h.rt.script = lambda _: [delta(text[:12]), delta(text[12:]), completed(text)]
    run = h.send(h.session())
    h.worker.run_once()
    assert h.run(run["id"])["output"] == "Found it."
    assert OPEN not in str(h.events(run["id"]))
    checkpoints = sql(
        env, "SELECT data FROM agent_message_contents WHERE data->>'type'='task_checkpoint'"
    )
    assert len(checkpoints) == 1 and checkpoints[0][0]["evidence"] == []


def test_credential_values_are_not_persisted_by_the_ledger(h):
    sid = UUID(h.session())
    with h.pool.connection() as c, c.transaction():
        ledger.append(
            c,
            sid,
            "platform_event",
            "system",
            "Bearer abcdefghijklmnopqrstuvwxyz",
            data={"refresh_token": "secret"},
        )
        rows = ledger.messages(c, sid)
    assert "abcdefghijklmnopqrstuvwxyz" not in str(rows) and "secret" not in str(rows)
