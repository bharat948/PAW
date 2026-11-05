"""Owner-scoped spending reads (A1-10): amounts only, confirmed apart from held and unconfirmed."""

from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import pytest
from tests.conftest import AUTH
from tests.costs.test_reconcile import attempt
from tests.gateway.test_service import start_run
from tests.test_agent_integration import Harness, sql

from paw_server.modules.costs import service as costs

pytestmark = pytest.mark.integration
USAGE = {"prompt_tokens": 100, "completion_tokens": 10}


@pytest.fixture
def h(env):
    x = Harness(env)
    yield x
    x.pool.close()


def test_a_run_with_no_attempts_reports_unknown_never_zero(h):
    run = h.send(h.session())
    cost = h.run(run["id"])["cost"]
    assert cost["status"] == "unknown" and cost["confirmed_usd"] is None


def test_a_runs_cost_separates_confirmed_from_held_and_unconfirmed(h, env):
    run = start_run(h)
    attempt(h, run, state="settled", usage=USAGE)
    attempt(h, run, state="uncertain")
    attempt(h, run, state="forwarded")
    cost = h.run(run["id"])["cost"]
    assert cost["status"] == "unconfirmed"
    confirmed = Decimal(cost["confirmed_usd"])
    held, unconfirmed = Decimal(cost["reserved_usd"]), Decimal(cost["unconfirmed_usd"])
    assert (
        confirmed > 0 and unconfirmed > 0 and held > unconfirmed
    )  # held = uncertain + still in flight
    assert cost["rate_versions"] == ["openai-2026-11-04"]


def test_the_run_list_carries_each_runs_own_cost(h):
    first = start_run(h)
    attempt(h, first, state="settled", usage=USAGE)
    runs = h.c.get(f"/v1/agent-sessions/{first['session_id']}/runs", headers=AUTH).json()["runs"]
    assert [r["cost"]["status"] for r in runs] == ["confirmed"]


def test_the_cancel_and_submit_responses_include_cost_too(h):
    s = h.session()
    run = h.send(s)
    assert "cost" in run
    assert "cost" in h.c.post(f"/v1/agent-runs/{run['id']}/cancel", headers=AUTH).json()


def test_today_spend_shows_confirmed_held_and_unconfirmed_beside_the_limits(h):
    run = start_run(h)
    attempt(h, run, state="settled", usage=USAGE)
    attempt(h, run, state="uncertain")
    out = h.c.get("/v1/agent-spend", headers=AUTH).json()
    assert out["day"] == str(costs.day_at(datetime.now(UTC)))
    assert out["resets_at"].endswith("+00:00") and out["resets_at"] > datetime.now(UTC).isoformat()
    assert out["actor"]["limit_usd"] == "3.0" or Decimal(out["actor"]["limit_usd"]) == 3
    assert Decimal(out["shared"]["limit_usd"]) == 3
    assert Decimal(out["actor"]["confirmed_usd"]) > 0
    assert Decimal(out["actor"]["unconfirmed_usd"]) == Decimal(out["actor"]["reserved_usd"]) > 0
    assert out["actor"]["confirmed_usd"] == out["shared"]["confirmed_usd"]


def test_spend_never_includes_another_workspaces_money(h, env):
    run = start_run(h)
    attempt(h, run, state="settled", usage=USAGE)
    other_ws = uuid4()
    with h.pool.connection() as conn, conn.transaction():
        costs.reserve(
            conn, workspace=other_ws, owner="x", actor="admin-hermes", session=uuid4(), profile=uuid4(),
            model="gpt-4o-mini", output=100, daily_cap=Decimal(1000), global_cap=Decimal(1000),
        )  # fmt: skip
    mine = h.c.get("/v1/agent-spend", headers=AUTH).json()
    assert mine["shared"]["reserved_usd"] == "0E-9" or Decimal(mine["shared"]["reserved_usd"]) == 0


def test_spend_and_run_costs_need_sign_in_and_hold_no_content(h):
    assert h.c.get("/v1/agent-spend").status_code == 401
    run = start_run(h)
    attempt(h, run, state="settled", usage=USAGE)
    body = h.c.get("/v1/agent-spend", headers=AUTH).text + str(h.run(run["id"])["cost"])
    assert "hello" not in body and "message" not in body


def test_no_active_profile_is_reported_not_guessed(h, env):
    sql(env, "UPDATE actor_profile_versions SET status = 'retired'")
    assert h.c.get("/v1/agent-spend", headers=AUTH).status_code == 503
