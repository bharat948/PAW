from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

from paw_server.db.pool import create_pool
from paw_server.modules.costs import service as costs
from paw_server.modules.costs.pricing import CostError


def test_atomic_shared_reservation_settlement_and_uncertainty(env):
    pool = create_pool(env.settings())
    pool.open()
    ws = uuid4()
    args = dict(
        workspace=ws,
        owner="owner",
        actor="admin-hermes",
        session=uuid4(),
        profile=uuid4(),
        model="gpt-4o-mini",
        output=100,
        daily_cap=Decimal(3),
        global_cap=Decimal(".03"),
    )

    def attempt():
        try:
            with pool.connection() as c, c.transaction():
                return costs.reserve(c, **args)
        except CostError as e:
            assert e.code == "budget_exceeded"
            return None

    try:
        with ThreadPoolExecutor(max_workers=4) as ex:
            results = list(ex.map(lambda _: attempt(), range(4)))
        admitted = [r for r in results if r]
        assert len(admitted) == 1
        aid = admitted[0]["id"]
        with pool.connection() as c:
            assert costs.forward(c, aid)
        with pool.connection() as c:
            costs.uncertain(c, aid)
            costs.release(c, aid)
            t = costs.totals(c, ws, admitted[0]["budget_day"])
            assert (
                t["reserved"] == admitted[0]["reserved_usd"] and t["unconfirmed"] == t["reserved"]
            )
            assert costs.settle(
                c, aid, "gpt-4o-mini", {"prompt_tokens": 100, "completion_tokens": 10}
            )
            assert not costs.settle(
                c, aid, "gpt-4o-mini", {"prompt_tokens": 100, "completion_tokens": 10}
            )
        assert attempt() is not None
    finally:
        pool.close()


def test_budget_day_and_reset():
    before = datetime(2026, 11, 4, 18, 29, 59, tzinfo=UTC)
    after = datetime(2026, 11, 4, 18, 30, tzinfo=UTC)
    assert str(costs.day_at(before)) == "2026-11-04"
    assert str(costs.day_at(after)) == "2026-11-05"
    assert costs.reset_at(before) == after


def test_missing_usage_remains_reserved_and_released_attempt_cannot_forward(env):
    pool = create_pool(env.settings())
    pool.open()
    try:
        with pool.connection() as c:
            a = costs.reserve(
                c,
                workspace=uuid4(),
                owner="owner",
                actor="admin-hermes",
                session=uuid4(),
                profile=uuid4(),
                model="gpt-4o-mini",
                output=100,
                daily_cap=Decimal(3),
                global_cap=Decimal(3),
            )
        with pool.connection() as c:
            assert costs.forward(c, a["id"])
            assert not costs.settle(c, a["id"], "gpt-4o-mini", None)
            assert (
                costs.totals(c, a["workspace_id"], a["budget_day"])["unconfirmed"]
                == a["reserved_usd"]
            )
    finally:
        pool.close()


def _reserve(c, **over):
    args = dict(
        workspace=uuid4(),
        owner="owner",
        actor="admin-hermes",
        session=uuid4(),
        profile=uuid4(),
        model="gpt-4o-mini",
        output=100,
        daily_cap=Decimal(3),
        global_cap=Decimal(3),
    )
    args.update(over)
    return costs.reserve(c, **args)


def test_released_attempt_frees_budget_and_can_never_forward(env):
    pool = create_pool(env.settings())
    pool.open()
    try:
        with pool.connection() as c:
            a = _reserve(c)
            costs.release(c, a["id"])
            assert not costs.forward(c, a["id"])
            assert costs.totals(c, a["workspace_id"], a["budget_day"])["reserved"] == 0
            assert not costs.settle(
                c, a["id"], "gpt-4o-mini", {"prompt_tokens": 1, "completion_tokens": 1}
            )
    finally:
        pool.close()


def test_forwarded_attempt_cannot_be_released(env):
    pool = create_pool(env.settings())
    pool.open()
    try:
        with pool.connection() as c:
            a = _reserve(c)
            assert costs.forward(c, a["id"])
            costs.release(c, a["id"])
            assert (
                costs.totals(c, a["workspace_id"], a["budget_day"])["reserved"] == a["reserved_usd"]
            )
    finally:
        pool.close()


def test_usage_outside_bound_or_unknown_model_stays_reserved(env):
    pool = create_pool(env.settings())
    pool.open()
    try:
        with pool.connection() as c:
            a = _reserve(c)
            assert costs.forward(c, a["id"])
            too_many_out = {"prompt_tokens": 1, "completion_tokens": a["output_bound"] + 1}
            assert not costs.settle(c, a["id"], "gpt-4o-mini", too_many_out)
            t = costs.totals(c, a["workspace_id"], a["budget_day"])
            assert t["confirmed"] == 0 and t["unconfirmed"] == a["reserved_usd"]
            assert not costs.settle(
                c, a["id"], "some-other-model", {"prompt_tokens": 1, "completion_tokens": 1}
            )
            bad = {
                "prompt_tokens": 10,
                "completion_tokens": 1,
                "prompt_tokens_details": {"cached_tokens": 11},
            }
            assert not costs.settle(c, a["id"], "gpt-4o-mini", bad)
            assert costs.totals(c, a["workspace_id"], a["budget_day"])["confirmed"] == 0
    finally:
        pool.close()


def test_settlement_cost_is_exact_and_replaces_the_reservation(env):
    pool = create_pool(env.settings())
    pool.open()
    try:
        with pool.connection() as c:
            a = _reserve(c)
            rate = costs.rate_for(c, "gpt-4o-mini", a["rate_version"])
            assert costs.forward(c, a["id"])
            usage = {
                "prompt_tokens": 2000,
                "completion_tokens": 50,
                "prompt_tokens_details": {"cached_tokens": 500},
            }
            assert costs.settle(c, a["id"], "gpt-4o-mini", usage)
            t = costs.totals(c, a["workspace_id"], a["budget_day"])
            assert t["confirmed"] == rate.cost(2000, 50, 500) and t["reserved"] == 0
            summary = costs.run_cost(c, a["id"])  # not a run id: no attempts bound to it
            assert summary["status"] == "unknown"
    finally:
        pool.close()


def test_run_cap_limits_a_run_and_run_cost_reports_status(env):
    pool = create_pool(env.settings())
    pool.open()
    run = uuid4()
    try:
        with pool.connection() as c:
            first = _reserve(c, run=run, run_cap=Decimal(1))
            rate = costs.rate_for(c, "gpt-4o-mini", first["rate_version"])
            tight = first["reserved_usd"] * 2 - Decimal("0.000000001")
            try:
                _reserve(c, workspace=first["workspace_id"], run=run, run_cap=tight)
                raise AssertionError("second reservation should exceed the run cap")
            except CostError as e:
                assert e.code == "budget_exceeded"
            try:
                _reserve(c, workspace=first["workspace_id"], run=run)
                raise AssertionError("a run reservation needs a run cap")
            except CostError as e:
                assert e.code == "missing_run_budget"
            assert costs.run_cost(c, run)["status"] == "reserved"
            assert costs.forward(c, first["id"])
            costs.uncertain(c, first["id"])
            assert costs.run_cost(c, run)["status"] == "unconfirmed"
            assert costs.settle(
                c, first["id"], "gpt-4o-mini", {"prompt_tokens": 10, "completion_tokens": 5}
            )
            s = costs.run_cost(c, run)
            assert s["status"] == "confirmed" and s["confirmed_usd"] == str(rate.cost(10, 5))
            assert s["rate_versions"] == [first["rate_version"]]
    finally:
        pool.close()
