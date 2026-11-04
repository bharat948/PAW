from uuid import UUID

import pytest
from tests.conftest import AUTH
from tests.runtime.fakes import completed, delta
from tests.test_agent_integration import Harness, sql

from paw_server.modules.chat.llm import Completion, LLMError
from paw_server.modules.runtime import lifecycle as lc
from paw_server.modules.runtime.agent_worker import AgentWorker
from paw_server.modules.runtime.protocol import RuntimeError_
from paw_server.modules.sessions.checkpoints import CLOSE, OPEN
from paw_server.modules.sessions.summaries import SummaryWorker

pytestmark = pytest.mark.integration


@pytest.fixture
def h(env):
    x = Harness(env)
    yield x
    x.pool.close()


def task_completed(h):
    text = (
        "Findings."
        + OPEN
        + '{"task_id":"one","status":"completed","goal":"investigate","summary":"findings recorded"}'
        + CLOSE
    )
    h.rt.script = lambda _: [delta(text), completed(text)]
    sid = h.session()
    run = h.send(sid)
    h.worker.run_once()
    return sid, run


def test_completed_task_checkpoint_triggers_summary_below_size_limit(h, env):
    sid, _ = task_completed(h)
    calls = []
    worker = SummaryWorker(
        h.pool,
        h.settings,
        lambda messages, tokens: (
            calls.append(messages),
            Completion("Task one complete. Findings retained.", "actual-model", 33, 12),
        )[1],
    )
    assert worker.run_once()
    assert len(calls) == 1 and "agent-reported task checkpoint" in calls[0][1]["content"]
    assert sql(env, "SELECT model,tokens_in,tokens_out,charged_actor_id FROM agent_summaries") == [
        ("actual-model", 33, 12, "admin-hermes")
    ]
    assert worker.run_once() is False
    h.send(sid, message="next task")
    h.worker.run_once()
    assert "Task one complete" in h.rt.created[-1]["instructions"]
    assert h.rt.created[-1]["history"] == []


def test_summary_failure_retries_and_does_not_invent_a_summary(h, env):
    task_completed(h)

    def fail(*args):
        raise LLMError("provider down")

    worker = SummaryWorker(h.pool, h.settings, fail)
    for _ in range(3):
        sql(env, "UPDATE agent_session_jobs SET available_at=now()")
        assert worker.run_once()
    assert sql(env, "SELECT state,attempts,error_code FROM agent_session_jobs") == [
        ("failed", 3, "summary_unavailable")
    ]
    assert sql(env, "SELECT count(*) FROM agent_summaries") == [(0,)]


def test_forget_during_summary_request_rejects_the_stale_result(h, env):
    sid, _ = task_completed(h)
    mid = sql(env, "SELECT id FROM agent_session_messages WHERE kind='user'")[0][0]

    def complete(*args):
        assert (
            h.c.post(f"/v1/agent-sessions/{sid}/messages/{mid}/forget", headers=AUTH).status_code
            == 200
        )
        return Completion("stale private facts", "model", 10, 5)

    assert SummaryWorker(h.pool, h.settings, complete).run_once()
    assert sql(env, "SELECT count(*) FROM agent_summaries") == [(0,)]
    assert sql(env, "SELECT error_code FROM agent_session_jobs") == [("source_changed",)]


def test_worker_restart_resumes_known_run_without_duplicate_events_or_messages(h, env):
    run = h.send(h.session())
    rid = UUID(run["id"])
    with h.pool.connection() as c:
        lc.claim_next(c, h.worker.runtime_id)
        handle = h.rt.create_run(run_id=str(rid), input="hello")
        lc.move(c, rid, "RUNNING", hermes_run_id=handle.hermes_run_id)
        lc.append_event(
            c, rid, "message.delta", {"text": "he", "raw": "he"}, handle.hermes_run_id + ":0"
        )
    restarted = AgentWorker(h.pool, h.rt, h.settings)
    assert restarted.recover() == 1
    assert h.run(run["id"])["state"] == "SUCCEEDED"
    assert len(h.rt.created) == 1 and restarted.recover() == 0
    assert sql(env, "SELECT count(*) FROM agent_session_messages WHERE kind='assistant'") == [(1,)]
    assert sql(
        env,
        "SELECT count(*) FROM agent_run_events WHERE source_event_id=%s",
        handle.hermes_run_id + ":0",
    ) == [(1,)]


def test_missing_and_temporarily_unavailable_runtime_are_distinguished(h, env):
    run = h.send(h.session())
    rid = UUID(run["id"])
    with h.pool.connection() as c:
        lc.claim_next(c, h.worker.runtime_id)
        lc.move(c, rid, "RUNNING", hermes_run_id="absent")
    h.worker.recover()
    assert h.run(run["id"])["error_code"] == "runtime_missing"
    run = h.send(h.session())
    rid = UUID(run["id"])
    with h.pool.connection() as c:
        lc.claim_next(c, h.worker.runtime_id)
        handle = h.rt.create_run(run_id=str(rid), input="hello")
        lc.move(c, rid, "RUNNING", hermes_run_id=handle.hermes_run_id)
    real = h.rt.get_run
    calls = []

    def flaky(hid):
        calls.append(hid)
        if len(calls) == 1:
            raise RuntimeError_("unavailable")
        return real(hid)

    h.rt.get_run = flaky
    assert h.worker.recover() == 1 and len(calls) == 2
    assert h.run(run["id"])["state"] == "SUCCEEDED"


def test_terminal_run_during_summary_keeps_followup_job_pending(h, env):
    sid, _ = task_completed(h)

    def complete(*args):
        h.send(sid, message="second task")
        h.worker.run_once()
        return Completion("First task complete.", "model", 10, 5)

    assert SummaryWorker(h.pool, h.settings, complete).run_once()
    assert sql(env, "SELECT state FROM agent_session_jobs") == [("pending",)]
    worker = SummaryWorker(
        h.pool, h.settings, lambda *args: Completion("Both tasks complete.", "model", 12, 6)
    )
    assert worker.run_once()
    assert sql(env, "SELECT state FROM agent_session_jobs") == [("done",)]
    assert sql(env, "SELECT count(*) FROM agent_summaries WHERE invalidated_at IS NULL") == [(1,)]
