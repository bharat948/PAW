"""Decision-table tests for the policy engine (002D-R03): one test per step, plus the evaluation order."""

import itertools
from dataclasses import replace

import pytest

from paw_server.modules.gateway.builtin import builtin_registry
from paw_server.modules.gateway.policy import REASONS, PolicyInput, RunBinding, decide
from paw_server.modules.gateway.registry import ToolSpec

REG = builtin_registry()
CAL = REG.get("calendar_list_events")
DOCS = REG.get("docs_search")
POLICY = {
    "grants": ["calendar_list_events", "docs_search", "paw_system_status"],
    "resource_scopes": {},
    "budgets": {"per_run_usd": 0.5, "daily_usd": 3.0},
}
RUN = RunBinding("run-1", "admin-hermes", 3, POLICY)
WRITE = ToolSpec(name="ticket_cancel", description="d", side_effect_class="internal_write", approval_required=True,
                 input_schema={"type": "object", "properties": {}, "additionalProperties": False})  # fmt: skip


def ok(**kw) -> PolicyInput:
    base = dict(
        authenticated=True, active_runs=(RUN,), attestation="ok", tool=CAL, connection="active"
    )
    return PolicyInput(**{**base, **kw})


def test_a_fully_valid_call_is_allowed():
    d = decide(ok())
    assert d.allowed and d.reason == "OK" and d.decision == "allowed"
    assert decide(
        ok(tool=DOCS, connection=None)
    ).allowed  # tools without a provider need no connection


# One mutation per step: (name, input, expected reason)
CASES = [
    ("unauthenticated", ok(authenticated=False), "UNAUTHENTICATED"),
    ("no run", ok(active_runs=()), "NO_ACTIVE_RUN"),
    ("two runs", ok(active_runs=(RUN, replace(RUN, run_id="run-2"))), "NO_ACTIVE_RUN"),
    ("no attestation", ok(attestation="missing"), "ATTESTATION_MISSING"),
    ("wrong attestation", ok(attestation="mismatch"), "ATTESTATION_MISMATCH"),
    ("unknown tool", ok(tool=None), "UNKNOWN_TOOL"),
    ("tool switched off", ok(tool_enabled=False), "TOOL_DISABLED"),
    ("not granted", ok(tool=REG.get("docs_get_source")), "NOT_GRANTED"),
    ("bad arguments", ok(args_problems=("days: maximum check failed",)), "INVALID_ARGUMENTS"),
    ("never connected", ok(connection=None), "NOT_CONNECTED"),
    ("needs reauth", ok(connection="needs_reauth"), "CONNECTION_REVOKED"),
    ("revoked", ok(connection="revoked"), "CONNECTION_REVOKED"),
    ("out of scope", ok(active_runs=(replace(RUN, policy={**POLICY, "resource_scopes": {"calendar_list_events": ["a"]}}),), resource="b"), "OUT_OF_SCOPE"),
    ("no resource on a scoped tool", ok(active_runs=(replace(RUN, policy={**POLICY, "resource_scopes": {"calendar_list_events": ["a"]}}),)), "OUT_OF_SCOPE"),
    ("too many calls", ok(active_runs=(replace(RUN, calls_so_far=40),)), "RATE_LIMITED"),
    ("run budget spent", ok(active_runs=(replace(RUN, cost_run_usd=0.5),)), "BUDGET_EXCEEDED"),
    ("daily budget spent", ok(active_runs=(replace(RUN, cost_today_usd=3.0),)), "BUDGET_EXCEEDED"),
    ("approval flag", ok(tool=WRITE, active_runs=(replace(RUN, policy={**POLICY, "grants": ["ticket_cancel"]}),)), "APPROVAL_REQUIRED"),
]  # fmt: skip


@pytest.mark.parametrize(("name", "inp", "reason"), CASES, ids=[c[0] for c in CASES])
def test_each_step_denies_with_its_reason(name, inp, reason):
    d = decide(inp)
    assert (d.allowed, d.reason) == (False, reason) and d.decision == "denied"
    assert d.reason in REASONS


def test_every_reason_code_is_exercised_by_the_table():
    covered = {c[2] for c in CASES} | {"OK"}
    assert covered == set(REASONS)


def test_external_writes_always_need_approval_even_if_the_flag_is_missing():
    ext = ToolSpec(name="issue_create", description="d", side_effect_class="external_write", approval_required=True,
                   input_schema={"type": "object", "properties": {}, "additionalProperties": False})  # fmt: skip
    policy = {**POLICY, "grants": ["issue_create"]}
    d = decide(ok(tool=ext, active_runs=(replace(RUN, policy=policy),)))
    assert d.reason == "APPROVAL_REQUIRED"


# ---- order: when several steps would fail, the earliest wins -----------------------------------------------------

ORDERED = [  # (mutation that makes exactly that step fail), in the spec's evaluation order
    ("UNAUTHENTICATED", dict(authenticated=False)),
    ("NO_ACTIVE_RUN", dict(active_runs=())),
    ("ATTESTATION_MISSING", dict(attestation="missing")),
    ("UNKNOWN_TOOL", dict(tool=None)),
    ("TOOL_DISABLED", dict(tool_enabled=False)),
    ("NOT_GRANTED", dict(tool=REG.get("docs_get_source"))),
    ("INVALID_ARGUMENTS", dict(args_problems=("x",))),
    ("NOT_CONNECTED", dict(connection=None)),
]


@pytest.mark.parametrize(("first", "second"), list(itertools.combinations(ORDERED, 2)),
                         ids=lambda c: c[0] if isinstance(c, tuple) else None)  # fmt: skip
def test_the_earlier_failure_always_wins(first, second):
    # tool=None and tool=docs_get_source conflict as mutations of the same field: resolve in favour of the earlier step
    kw = {**second[1], **first[1]}
    assert decide(ok(**kw)).reason == first[0]


def test_ungranted_tools_do_not_reveal_argument_problems():
    d = decide(
        ok(tool=REG.get("docs_get_source"), args_problems=("chunk_id: pattern check failed",))
    )
    assert d.reason == "NOT_GRANTED" and "chunk_id" not in d.detail


def test_policy_comes_from_the_runs_bound_profile_version_only():
    other = replace(RUN, profile_version=9, policy={**POLICY, "grants": []})
    assert decide(ok(active_runs=(other,))).reason == "NOT_GRANTED"
    assert decide(
        ok(active_runs=(RUN,))
    ).allowed  # a later activation of another version cannot change this run


def test_missing_policy_sections_deny_by_default():
    bare = replace(RUN, policy={})
    assert decide(ok(active_runs=(bare,))).reason == "NOT_GRANTED"
    only_grants = replace(RUN, policy={"grants": ["calendar_list_events"]})
    assert (
        decide(ok(active_runs=(only_grants,))).reason == "BUDGET_EXCEEDED"
    )  # a policy without budgets is broken
    no_limits = replace(RUN, policy={"grants": ["calendar_list_events"], "budgets": {}})
    assert decide(ok(active_runs=(no_limits,))).reason == "BUDGET_EXCEEDED"


def test_a_limit_spent_is_named_and_decimals_compare_exactly():
    from decimal import Decimal

    spent = replace(RUN, cost_run_usd=Decimal("0.5"))
    assert decide(ok(active_runs=(spent,))).detail == "run budget spent"
    shared = replace(RUN, cost_shared_usd=Decimal("3"))
    assert decide(ok(active_runs=(shared,))).detail == "shared daily budget spent"
    assert decide(ok(active_runs=(replace(RUN, cost_run_usd=Decimal("0.499999")),))).reason == "OK"
    assert (
        decide(ok(active_runs=(replace(RUN, cost_unavailable=True),))).detail
        == "accounting unavailable"
    )
    bad = replace(RUN, policy={**RUN.policy, "budgets": {"per_run_usd": "x", "daily_usd": 3}})
    assert decide(ok(active_runs=(bad,))).detail == "malformed budget"
