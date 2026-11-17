"""Crash accounting and late evidence (A1-09): what a crash leaves behind is resolved, and nothing becomes free
unless it was provably never sent."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID

import pytest
from tests.gateway.test_service import start_run
from tests.test_agent_integration import Harness, sql

from paw_server.modules.costs import service as costs
from paw_server.modules.runtime import lifecycle as lc

pytestmark = pytest.mark.integration
LATER = timedelta(minutes=10)


@pytest.fixture
def h(env):
    x = Harness(env)
    yield x
    x.pool.close()


def attempt(h, run, *, state="reserved", usage=None):
    """One priced attempt of the run, driven to `state` through the real service calls."""
    with h.pool.connection() as conn:
        r = conn.execute(
            "SELECT r.session_id, r.executing_actor_id, r.profile_version_id, s.owner, s.workspace_id "
            "FROM agent_runs r JOIN agent_sessions s ON s.id = r.session_id WHERE r.id = %s",
            (run["id"],),
        ).fetchone()
        with conn.transaction():
            a = costs.reserve(
                conn, workspace=r["workspace_id"], owner=r["owner"], actor=r["executing_actor_id"],
                session=r["session_id"], profile=r["profile_version_id"], model="gpt-4o-mini", output=2000,
                daily_cap=Decimal(1000), global_cap=Decimal(1000), run=UUID(str(run["id"])),
                run_cap=Decimal(1000), input_tokens=5000,
            )  # fmt: skip
        with conn.transaction():
            if state in ("forwarded", "uncertain", "settled"):
                assert costs.forward(conn, a["id"])
            if state == "uncertain":
                costs.uncertain(conn, a["id"])
            if state == "settled":
                assert costs.settle(conn, a["id"], "gpt-4o-mini", usage)
    return a["id"]


def states(env):
    return [
        r[0]
        for r in sql(
            env,
            "SELECT r.state FROM actor_cost_results r JOIN actor_cost_attempts a ON a.id=r.attempt_id ORDER BY a.authorized_at",
        )
    ]


def run_sweep(h, **kw):
    with h.pool.connection() as conn, conn.transaction():
        return costs.sweep(conn, datetime.now(UTC) + kw.get("later", timedelta(0)))


def finish(h, run, tokens_in=None, tokens_out=None, **extra):
    fields = {
        k: v
        for k, v in {"tokens_in": tokens_in, "tokens_out": tokens_out, **extra}.items()
        if v is not None
    }
    with h.pool.connection() as conn:
        lc.finish(conn, UUID(str(run["id"])), "SUCCEEDED", **fields)


def reconcile_run(h, run):
    with h.pool.connection() as conn, conn.transaction():
        return costs.reconcile_run(conn, UUID(str(run["id"])))


# ---- the sweep ----------------------------------------------------------------------------------------------------


def test_a_hold_that_was_never_sent_is_released_but_only_after_the_grace_period(h, env):
    run = start_run(h)
    attempt(h, run)
    assert run_sweep(h)["released"] == [] and states(env) == ["reserved"]
    assert len(run_sweep(h, later=LATER)["released"]) == 1 and states(env) == ["released"]
    assert run_sweep(h, later=LATER)["released"] == []  # idempotent


def test_a_released_hold_can_never_be_sent_afterwards(h, env):
    run = start_run(h)
    aid = attempt(h, run)
    run_sweep(h, later=LATER)
    with h.pool.connection() as conn:
        assert (
            costs.forward(conn, aid) is False
        )  # the sender lost the race and must not call the provider


def test_a_sent_attempt_is_never_released_only_labelled_uncertain_and_still_holds_money(h, env):
    run = start_run(h)
    attempt(h, run, state="forwarded")
    assert run_sweep(h)["uncertain"] == []  # too recent: it may still be in flight
    assert len(run_sweep(h, later=LATER)["uncertain"]) == 1 and states(env) == ["uncertain"]
    with h.pool.connection() as conn:
        ws = conn.execute("SELECT workspace_id FROM agent_sessions").fetchone()["workspace_id"]
        t = costs.totals(conn, ws, costs.day_at(datetime.now(UTC)))
    assert t["reserved"] > 0 and t["unconfirmed"] == t["reserved"]


def test_settled_and_released_attempts_are_untouched_by_the_sweep(h, env):
    run = start_run(h)
    attempt(h, run, state="settled", usage={"prompt_tokens": 100, "completion_tokens": 10})
    out = run_sweep(h, later=LATER)
    assert out == {"released": [], "uncertain": []} and states(env) == ["settled"]


# ---- late evidence ------------------------------------------------------------------------------------------------


def test_exactly_one_uncertain_attempt_is_settled_from_the_runs_remaining_usage(h, env):
    run = start_run(h)
    attempt(h, run, state="settled", usage={"prompt_tokens": 100, "completion_tokens": 10})
    attempt(h, run, state="uncertain")
    finish(
        h, run, tokens_in=400, tokens_out=60
    )  # the runtime saw 300 in / 50 out more than the ledger
    assert reconcile_run(h, run) == "settled"
    assert states(env) == ["settled", "settled"]
    assert sql(
        env, "SELECT tokens_in, tokens_out FROM actor_cost_results ORDER BY updated_at DESC LIMIT 1"
    ) == [(300, 50)]
    assert reconcile_run(h, run) == "no_candidate"  # nothing left to do


@pytest.mark.parametrize(
    "setup, expected",
    [
        ("no usage reported", "no_evidence"),
        ("zero usage", "no_usage"),
        ("less than settled", "inconsistent"),
        ("cache fields present", "ambiguous_cache"),
        ("beyond the attempt's bounds", "out_of_bounds"),
        ("still running", "no_evidence"),
    ],
)
def test_evidence_that_is_missing_zero_contradictory_or_ambiguous_never_frees_the_hold(
    h, env, setup, expected
):
    run = start_run(h)
    attempt(h, run, state="settled", usage={"prompt_tokens": 100, "completion_tokens": 10})
    attempt(h, run, state="uncertain")
    if setup == "no usage reported":
        finish(h, run)
    elif setup == "zero usage":
        finish(h, run, tokens_in=100, tokens_out=10)
    elif setup == "less than settled":
        finish(h, run, tokens_in=50, tokens_out=5)
    elif setup == "cache fields present":
        finish(h, run, tokens_in=400, tokens_out=60, cache_read=20)
    elif setup == "beyond the attempt's bounds":
        finish(h, run, tokens_in=10**7, tokens_out=60)
    assert reconcile_run(h, run) == expected
    assert states(env) == ["settled", "uncertain"]  # the hold is still there


def test_two_uncertain_attempts_cannot_be_told_apart_so_both_stay_held(h, env):
    run = start_run(h)
    attempt(h, run, state="uncertain")
    attempt(h, run, state="uncertain")
    finish(h, run, tokens_in=300, tokens_out=50)
    assert reconcile_run(h, run) == "not_attributable" and states(env) == ["uncertain", "uncertain"]


def test_nothing_is_attributed_while_another_call_of_the_run_may_still_report(h, env):
    run = start_run(h)
    attempt(h, run, state="uncertain")
    attempt(h, run, state="forwarded")
    finish(h, run, tokens_in=300, tokens_out=50)
    assert reconcile_run(h, run) == "pending" and states(env) == ["uncertain", "forwarded"]


# ---- the whole pass, as a fresh worker after a crash --------------------------------------------------------------


def test_a_fresh_worker_resolves_everything_a_crash_left_behind(h, env):
    run = start_run(h)
    attempt(h, run)  # crashed before the send fence
    attempt(h, run, state="forwarded")  # crashed mid-call
    finish(h, run, tokens_in=300, tokens_out=50)
    with h.pool.connection() as conn, conn.transaction():
        first = costs.reconcile(conn, datetime.now(UTC) + LATER)
    assert len(first["released"]) == 1 and len(first["uncertain"]) == 1
    assert first["runs"] == {
        "settled": 1
    }  # the one remaining call is the only candidate for the run's usage
    assert sorted(states(env)) == ["released", "settled"]
    with h.pool.connection() as conn, conn.transaction():
        again = costs.reconcile(conn, datetime.now(UTC) + LATER)
    assert again == {"released": [], "uncertain": [], "runs": {}}


def test_the_worker_entry_point_runs_a_pass_and_reports_it(h, env):
    run = start_run(h)
    attempt(h, run)
    assert h.worker.reconcile_costs() == {
        "released": [],
        "uncertain": [],
        "runs": {},
    }  # nothing is old enough yet
    assert states(env) == ["reserved"]
