"""A1 acceptance: spending enforcement is reported honestly, and the budget day behaves at midnight.

Complements the focused suites: test_service (atomic reserve/settle), test_proxy (denials, streams, identity),
test_reconcile (crashes), tests/gateway (tool budgets), tests/sessions/test_summaries_recovery (summaries).
"""

import asyncio
import logging
from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import httpx
import pytest
from tests.conftest import AUTH
from tests.costs.test_proxy import GATEWAY_TOKEN, PROXY_TOKEN, UPSTREAM_KEY, make_proxy
from tests.runtime.fakes import completed, delta
from tests.sessions.test_summaries_recovery import task_completed
from tests.test_agent_integration import Harness, sql

from paw_server.modules.chat.llm import Completion
from paw_server.modules.costs import readiness, tokens
from paw_server.modules.costs import service as costs
from paw_server.modules.costs.pricing import CostError
from paw_server.modules.gateway.registry import ToolRegistry
from paw_server.modules.gateway.server import build_app
from paw_server.modules.gateway.service import GatewayService
from paw_server.modules.sessions.checkpoints import CLOSE, OPEN
from paw_server.modules.sessions.summaries import SummaryWorker

pytestmark = pytest.mark.integration
SECRET = "highly-confidential-sentence-0xC0FFEE"


@pytest.fixture
def h(env):
    x = Harness(env)
    yield x
    x.pool.close()


# ---- readiness ----------------------------------------------------------------------------------------------------


def settings(h, **kw):
    return h.env.settings(
        model_proxy_token=PROXY_TOKEN,
        model_proxy_upstream_key=UPSTREAM_KEY,
        gateway_token=GATEWAY_TOKEN,
        **kw,
    )


def test_enforcement_is_ready_when_everything_it_needs_is_present(h):
    assert readiness.enforcement_problems(h.pool, settings(h)) == []


def test_each_missing_dependency_is_named_and_nothing_else(h, monkeypatch):
    s = settings(h)
    assert readiness.enforcement_problems(h.pool, h.env.settings()) == [
        "model_proxy_not_configured"
    ]
    assert readiness.enforcement_problems(
        h.pool, s.model_copy(update={"model_proxy_upstream_key": ""})
    ) == ["upstream_key"]
    assert readiness.enforcement_problems(
        h.pool, s.model_copy(update={"agent_summary_model": "no-such-model"})
    ) == ["pricing", "tokenizer"]  # an unknown model has neither a price nor a token counter
    monkeypatch.setattr(tokens, "encoding_for", lambda m: (_ for _ in ()).throw(CostError("x")))
    assert readiness.enforcement_problems(h.pool, s) == ["tokenizer"]
    monkeypatch.undo()
    sql(h.env, "UPDATE actor_profile_versions SET status = 'retired'")
    assert "profile" in readiness.enforcement_problems(h.pool, s)


def test_an_unreachable_database_is_a_problem_not_a_crash(h):
    class Broken:
        def connection(self):
            raise RuntimeError("db down")

    assert "database" in readiness.enforcement_problems(Broken(), settings(h))  # type: ignore[arg-type]


def gateway_get(h, enforcement, path="/_ready"):
    proxy = make_proxy(h, None)
    service = GatewayService(h.pool, ToolRegistry([]), proxy.settings)
    app = build_app(service, ToolRegistry([]), GATEWAY_TOKEN, proxy, PROXY_TOKEN, enforcement)

    async def go():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://t"
        ) as c:
            return await c.get(path)

    return asyncio.run(go())


def test_the_gateway_reports_503_and_the_failing_checks_when_enforcement_is_broken(h):
    ok = gateway_get(h, lambda: [])
    assert (ok.status_code, ok.json()) == (200, {"ok": True, "problems": []})
    bad = gateway_get(h, lambda: ["pricing"])
    assert (bad.status_code, bad.json()) == (503, {"ok": False, "problems": ["pricing"]})
    assert UPSTREAM_KEY not in bad.text and PROXY_TOKEN not in bad.text
    assert (
        gateway_get(h, lambda: ["pricing"], "/_health").status_code == 200
    )  # liveness is separate


def test_a_gateway_with_no_model_proxy_is_not_ready_to_spend(h):
    r = gateway_get(h, None)
    assert (r.status_code, r.json()["problems"]) == (503, ["model_proxy_not_configured"])


def test_the_api_asks_the_gateway_and_an_unreachable_one_is_a_problem(h, monkeypatch):
    s = settings(h)

    def unreachable(*a, **k):
        raise httpx.ConnectError("refused")

    monkeypatch.setattr(readiness.httpx, "get", unreachable)
    assert readiness.gateway_enforcement(s) == ["proxy_unreachable"]
    monkeypatch.setattr(
        readiness.httpx,
        "get",
        lambda *a, **k: httpx.Response(503, json={"problems": ["pricing", "tokenizer"]}),
    )
    assert readiness.gateway_enforcement(s) == ["pricing", "tokenizer"]
    monkeypatch.setattr(readiness.httpx, "get", lambda *a, **k: httpx.Response(503, text="oops"))
    assert readiness.gateway_enforcement(s) == ["proxy_not_ready"]
    monkeypatch.setattr(
        readiness.httpx, "get", lambda *a, **k: httpx.Response(200, json={"ok": True})
    )
    assert readiness.gateway_enforcement(s) == []


def test_the_runtime_is_not_ready_while_configured_enforcement_is_unavailable(env, monkeypatch):
    x = Harness(env, model_proxy_token=PROXY_TOKEN, model_proxy_upstream_key=UPSTREAM_KEY)
    try:
        monkeypatch.setattr("paw_server.api.agent.gateway_enforcement", lambda s: ["pricing"])
        out = x.c.get("/v1/runtime/health", headers=AUTH).json()
        assert out["ready"] is False and "enforcement_pricing" in out["missing"]
        monkeypatch.setattr("paw_server.api.agent.gateway_enforcement", lambda s: [])
        assert x.c.get("/v1/runtime/health", headers=AUTH).json()["ready"] is True
    finally:
        x.pool.close()


# ---- the budget day at midnight (Asia/Kolkata) --------------------------------------------------------------------

BEFORE = datetime(2026, 11, 4, 18, 29, 59, tzinfo=UTC)  # 23:59:59 on the 4th in India
AFTER = datetime(2026, 11, 4, 18, 30, 0, tzinfo=UTC)  # 00:00:00 on the 5th


def reserve(conn, ws, now, *, cap, run=None, run_cap=None, actor="admin-hermes"):
    return costs.reserve(
        conn, workspace=ws, owner="owner", actor=actor, session=uuid4(), profile=uuid4(),
        model="gpt-4o-mini", output=100, daily_cap=cap, global_cap=cap, run=run, run_cap=run_cap, now=now,
    )  # fmt: skip


def one_call_cap(pool):
    with pool.connection() as conn:
        rate = costs.rate_for(conn, "gpt-4o-mini")
    return rate.reserve(100, None)


def test_a_spent_day_admits_again_at_exactly_the_reset_instant(h):
    cap, ws = one_call_cap(h.pool), uuid4()
    with h.pool.connection() as conn, conn.transaction():
        reserve(conn, ws, BEFORE, cap=cap)
    with h.pool.connection() as conn:
        with pytest.raises(CostError) as e, conn.transaction():
            reserve(conn, ws, BEFORE, cap=cap)
        assert e.value.code == "budget_exceeded"
        assert costs.reset_at(BEFORE) == AFTER
    with h.pool.connection() as conn, conn.transaction():
        assert reserve(conn, ws, AFTER, cap=cap)["budget_day"].isoformat() == "2026-11-05"


def test_money_stays_on_the_day_it_was_authorized_even_when_settled_after_midnight(h):
    cap, ws = one_call_cap(h.pool), uuid4()
    with h.pool.connection() as conn, conn.transaction():
        a = reserve(conn, ws, BEFORE, cap=cap)
        assert costs.forward(conn, a["id"])
    with h.pool.connection() as conn, conn.transaction():
        costs.uncertain(conn, a["id"])  # nothing was reported; the midnight passes
    day1, day2 = costs.day_at(BEFORE), costs.day_at(AFTER)
    with h.pool.connection() as conn:
        assert costs.totals(conn, ws, day1)["unconfirmed"] == a["reserved_usd"]
        assert costs.totals(conn, ws, day2)["reserved"] == 0  # the new day starts clean
        assert costs.settle(
            conn, a["id"], "gpt-4o-mini", {"prompt_tokens": 10, "completion_tokens": 5}
        )
        conn.commit()
        assert (
            costs.totals(conn, ws, day1)["confirmed"] > 0
            and costs.totals(conn, ws, day2)["confirmed"] == 0
        )


def test_a_runs_own_cap_spans_midnight_though_the_daily_bucket_resets(h):
    cap, ws, run = one_call_cap(h.pool), uuid4(), uuid4()
    run_cap = cap * Decimal("1.5")  # room for one call, not two
    with h.pool.connection() as conn, conn.transaction():
        reserve(conn, ws, BEFORE, cap=cap * 10, run=run, run_cap=run_cap)
    with h.pool.connection() as conn:
        with pytest.raises(CostError) as e, conn.transaction():
            reserve(conn, ws, AFTER, cap=cap * 10, run=run, run_cap=run_cap)
        assert (
            e.value.code == "budget_exceeded"
        )  # a new day does not give the same run a new allowance


def test_the_daily_cap_of_one_actor_does_not_spend_another_actors(h):
    cap, ws = one_call_cap(h.pool), uuid4()
    with h.pool.connection() as conn, conn.transaction():
        costs.reserve(
            conn, workspace=ws, owner="owner", actor="a", session=uuid4(), profile=uuid4(),
            model="gpt-4o-mini", output=100, daily_cap=cap, global_cap=cap * 10, now=BEFORE,
        )  # fmt: skip
    with h.pool.connection() as conn, conn.transaction():
        costs.reserve(
            conn, workspace=ws, owner="owner", actor="b", session=uuid4(), profile=uuid4(),
            model="gpt-4o-mini", output=100, daily_cap=cap, global_cap=cap * 10, now=BEFORE,
        )  # fmt: skip


# ---- nothing private in the logs, whichever part of A1 logs ---------------------------------------------------------


def test_no_conversation_text_or_credential_reaches_the_logs_from_summaries_or_reconciliation(
    env, caplog
):
    x = Harness(env)
    try:
        text = (
            "Done."
            + OPEN
            + '{"task_id":"one","status":"completed","goal":"g","summary":"s"}'
            + CLOSE
        )
        x.rt.script = lambda _: [delta(text), completed(text)]
        run = x.send(x.session(), message=SECRET)
        x.worker.run_once()
        with caplog.at_level(logging.DEBUG):
            unpriced = x.settings.model_copy(update={"agent_summary_model": "no-such-model"})
            SummaryWorker(
                x.pool, unpriced, lambda m, t: Completion("s", "gpt-4o-mini", 1, 1)
            ).run_once()
            sql(env, "UPDATE agent_session_jobs SET available_at = now()")
            SummaryWorker(
                x.pool, x.settings, lambda m, t: Completion("s", "gpt-4o-mini", None, None)
            ).run_once()
            x.worker.reconcile_costs()
            x.worker.expire_deadlines()
        logged = "\n".join(f"{r.getMessage()} {r.__dict__}" for r in caplog.records)
        assert SECRET not in logged and run["id"] and UPSTREAM_KEY not in logged
    finally:
        x.pool.close()


def test_task_completed_helper_still_builds_a_summarizable_conversation(h):
    task_completed(h)  # guards the shared fixture this module relies on
    assert sql(h.env, "SELECT count(*) FROM agent_session_jobs") == [(1,)]
