from decimal import Decimal
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


# ---- summaries spend from the same budget as runs (A1-07) ---------------------------------------------------------


def summary_attempts(env):
    return sql(
        env,
        "SELECT a.kind, a.run_id IS NULL, a.actor_id, r.state FROM actor_cost_attempts a "
        "JOIN actor_cost_results r ON r.attempt_id = a.id ORDER BY a.authorized_at",
    )


def ok_completion(calls=None):
    def complete(messages, max_tokens):
        if calls is not None:
            calls.append(messages)
        return Completion("Task one complete.", "gpt-4o-mini", 120, 20)

    return complete


def test_a_summary_reserves_before_the_call_and_settles_from_usage(h, env):
    task_completed(h)
    seen = []

    def complete(messages, max_tokens):
        seen.append(
            summary_attempts(env)
        )  # visible and fenced as forwarded while the request is in flight
        return Completion("Task one complete.", "gpt-4o-mini", 120, 20)

    assert SummaryWorker(h.pool, h.settings, complete).run_once()
    assert seen == [[("summary", True, "admin-hermes", "forwarded")]]
    assert summary_attempts(env) == [("summary", True, "admin-hermes", "settled")]
    assert sql(env, "SELECT cost_usd > 0 FROM actor_cost_results") == [(True,)]


def test_a_spent_day_defers_the_summary_to_the_next_midnight_without_calling_the_provider(h, env):
    task_completed(h)
    calls = []
    spent = h.settings.model_copy(update={"cost_shared_daily_usd": Decimal("0.0000001")})
    assert SummaryWorker(h.pool, spent, ok_completion(calls)).run_once()
    assert calls == [] and summary_attempts(env) == []
    assert sql(
        env,
        "SELECT state, error_code, attempts, available_at > now() + interval '1 minute' FROM agent_session_jobs",
    ) == [("pending", "budget_deferred", 0, True)]
    # Nothing is claimable until the reset, however often the worker polls.
    assert SummaryWorker(h.pool, spent, ok_completion(calls)).run_once() is False
    # After the reset it runs and spends normally.
    sql(env, "UPDATE agent_session_jobs SET available_at = now()")
    assert SummaryWorker(h.pool, h.settings, ok_completion(calls)).run_once()
    assert len(calls) == 1 and summary_attempts(env)[0][3] == "settled"
    assert sql(env, "SELECT count(*) FROM agent_summaries") == [(1,)]


def test_repeated_deferrals_never_use_up_the_retry_attempts(h, env):
    task_completed(h)
    spent = h.settings.model_copy(update={"cost_shared_daily_usd": Decimal("0.0000001")})
    for _ in range(5):
        sql(env, "UPDATE agent_session_jobs SET available_at = now()")
        assert SummaryWorker(h.pool, spent, ok_completion()).run_once()
    assert sql(env, "SELECT state, attempts FROM agent_session_jobs") == [("pending", 0)]


def test_a_failed_request_keeps_its_reservation_as_uncertain_spend(h, env):
    task_completed(h)

    def fail(*args):
        raise LLMError("timeout after send")

    assert SummaryWorker(h.pool, h.settings, fail).run_once()
    assert summary_attempts(env) == [("summary", True, "admin-hermes", "uncertain")]
    assert sql(env, "SELECT error_code FROM agent_session_jobs") == [("summary_unavailable",)]


@pytest.mark.parametrize(
    "usage", [(None, None), (10**9, 20), (120, 10**9)], ids=["missing", "input-over", "output-over"]
)
def test_unusable_usage_leaves_the_spend_uncertain_not_free(h, env, usage):
    task_completed(h)

    def complete(messages, max_tokens):
        return Completion("Task one complete.", "gpt-4o-mini", *usage)

    assert SummaryWorker(h.pool, h.settings, complete).run_once()
    assert summary_attempts(env) == [("summary", True, "admin-hermes", "uncertain")]


def test_an_unpriced_summary_model_fails_closed_without_calling_or_deferring(h, env):
    task_completed(h)
    calls = []
    unpriced = h.settings.model_copy(update={"agent_summary_model": "no-such-model"})
    assert SummaryWorker(h.pool, unpriced, ok_completion(calls)).run_once()
    assert calls == [] and summary_attempts(env) == []
    assert sql(env, "SELECT state, error_code, attempts FROM agent_session_jobs") == [
        ("pending", "budget_unavailable", 1)
    ]


def test_a_stale_summary_is_still_settled_because_it_was_billed(h, env):
    sid, _ = task_completed(h)
    mid = sql(env, "SELECT id FROM agent_session_messages WHERE kind='user'")[0][0]

    def forget_then_answer(messages, max_tokens):
        h.c.post(f"/v1/agent-sessions/{sid}/messages/{mid}/forget", headers=AUTH)
        return Completion("Task one complete.", "gpt-4o-mini", 120, 20)

    assert SummaryWorker(h.pool, h.settings, forget_then_answer).run_once()
    assert summary_attempts(env) == [("summary", True, "admin-hermes", "settled")]
    assert sql(env, "SELECT count(*) FROM agent_summaries") == [(0,)]
