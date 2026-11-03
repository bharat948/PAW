"""GatewayService against PostgreSQL: run binding, every policy outcome, audit, events (002D-R02, R03, R04)."""

import threading
from dataclasses import replace
from uuid import UUID

import psycopg
import pytest

from paw_server.modules.gateway.builtin import builtin_specs
from paw_server.modules.gateway.registry import ToolRegistry, sync_registry
from paw_server.modules.gateway.service import GatewayService, redact_args
from paw_server.modules.identity import service as identity
from paw_server.modules.identity.spec import DEFAULT_ADMIN_SPEC
from paw_server.modules.runtime import lifecycle as lc
from tests.test_agent_integration import Harness

pytestmark = pytest.mark.integration
SECRET_TEXT = "TOP-SECRET-INTERNAL-DETAIL"


class Calls:
    def __init__(self) -> None:
        self.contexts: list = []
        self.args: list = []
        self.fail = False


@pytest.fixture
def h(env):
    x = Harness(env)
    yield x
    x.pool.close()


@pytest.fixture
def calls():
    return Calls()


def make_registry(calls: Calls) -> ToolRegistry:
    def handler(ctx, args):
        calls.contexts.append(ctx)
        calls.args.append(args)
        if calls.fail:
            raise RuntimeError(SECRET_TEXT)
        return {"ok": True, "echo": args}

    return ToolRegistry([replace(s, handler=handler) for s in builtin_specs()])


def gateway(h, calls, **kw) -> GatewayService:
    reg = make_registry(calls)
    with h.pool.connection() as conn:
        sync_registry(conn, reg)
    settings = h.env.settings(
        **{k: v for k, v in kw.items() if k in ("gateway_max_calls_per_run", "gateway_attestation")}
    )
    extra = {k: v for k, v in kw.items() if k in ("connection_status", "attestation")}
    return GatewayService(h.pool, reg, settings, **extra)


def start_run(h, state="RUNNING"):
    """A run that is leased to the runtime and bound to the active profile, as the worker leaves it."""
    run = h.send(h.session())
    with h.pool.connection() as conn:
        claimed = lc.claim_next(conn, h.worker.runtime_id)
        prof = identity.get_active(conn, "admin-hermes")
        lc.bind_profile(conn, claimed["id"], profile_version_id=prof.version_id, version=prof.version,
                        compiled_hash=prof.compiled.compiled_hash)  # fmt: skip
        if state == "RUNNING":
            lc.move(conn, claimed["id"], "RUNNING", hermes_run_id="h1")
        if state == "CANCELLING":
            lc.move(conn, claimed["id"], "RUNNING", hermes_run_id="h1")
            lc.request_cancel(conn, claimed["id"])
    return run


def audit(env, **where):
    cond = " AND ".join(f"{k} = %s" for k in where) or "true"
    with psycopg.connect(env.url, autocommit=True) as c:
        return c.execute(
            f"SELECT tool, tool_version, decision, reason_code, run_id::text, actor_id, latency_ms, result_bytes, "
            f"truncated, error, args_redacted, trace_id FROM gateway_tool_invocations WHERE {cond} ORDER BY created_at",
            list(where.values()),
        ).fetchall()


def events(h, run_id):
    """Stored tool.decision events (read from the table: the SSE stream of an unfinished run never ends)."""
    with psycopg.connect(h.env.url, autocommit=True) as c:
        rows = c.execute(
            "SELECT payload FROM agent_run_events WHERE run_id = %s AND type = 'tool.decision' ORDER BY seq",
            (run_id,),
        ).fetchall()
    return [{"payload": r[0]} for r in rows]


# ---- the happy path ----------------------------------------------------------------------------------------------


def test_an_allowed_call_runs_the_handler_with_identity_derived_from_the_lease(h, calls, env):
    run = start_run(h)
    out = gateway(h, calls).handle_call("docs_search", {"query": "invoices", "top_k": 3})
    assert (out.is_error, out.decision, out.reason) == (
        False,
        "allowed",
        "OK",
    ) and '"ok": true' in out.text
    ctx = calls.contexts[0]
    assert (
        str(ctx.run_id) == run["id"] and ctx.actor_id == "admin-hermes" and ctx.requester == "owner"
    )
    assert (
        ctx.session_id
        and ctx.workspace_id
        and ctx.invocation_id == out.invocation_id
        and ctx.trace_id
    )
    row = audit(env)[0]
    assert row[:6] == ("docs_search", 1, "allowed", "OK", run["id"], "admin-hermes")
    assert (
        row[6] >= 0
        and row[7] > 0
        and row[8] is False
        and row[9] is None
        and row[10] == {"query": "invoices", "top_k": 3}
    )
    ev = events(h, run["id"])
    assert (
        len(ev) == 1
        and ev[0]["payload"]["tool"] == "docs_search"
        and ev[0]["payload"]["invocation_id"] == str(out.invocation_id)
    )
    assert ev[0]["payload"]["decision"] == "allowed" and ev[0]["payload"]["reason"] == "OK"


def test_a_call_during_dispatching_is_allowed_because_hermes_may_call_before_the_worker_records_running(
    h, calls
):
    start_run(h, state="DISPATCHING")
    assert gateway(h, calls).handle_call("docs_search", {"query": "x"}).decision == "allowed"


# ---- run binding -------------------------------------------------------------------------------------------------


def test_no_active_run_is_denied_and_still_audited_without_a_run(h, calls, env):
    out = gateway(h, calls).handle_call("docs_search", {"query": "x"})
    assert (out.decision, out.reason, out.is_error) == ("denied", "NO_ACTIVE_RUN", True)
    assert calls.contexts == []
    row = audit(env)[0]
    assert row[3] == "NO_ACTIVE_RUN" and row[4] is None and row[5] is None


@pytest.mark.parametrize("state", ["QUEUED", "CANCELLING"])
def test_only_dispatching_or_running_runs_can_call_tools(h, calls, state):
    if state == "QUEUED":
        h.send(h.session())
    else:
        start_run(h, state="CANCELLING")
    assert gateway(h, calls).handle_call("docs_search", {"query": "x"}).reason == "NO_ACTIVE_RUN"


def test_a_finished_run_cannot_call_tools(h, calls):
    run = start_run(h)
    with h.pool.connection() as conn:
        lc.finish(conn, UUID(run["id"]), "SUCCEEDED", output="done")
    assert gateway(h, calls).handle_call("docs_search", {"query": "x"}).reason == "NO_ACTIVE_RUN"


def test_a_run_without_a_lease_is_not_active(h, calls, env):
    start_run(h)
    with psycopg.connect(env.url, autocommit=True) as c:
        c.execute("DELETE FROM agent_run_leases")
    assert gateway(h, calls).handle_call("docs_search", {"query": "x"}).reason == "NO_ACTIVE_RUN"


def test_a_lease_on_another_runtime_is_not_ours(h, calls, env):
    start_run(h)
    with psycopg.connect(env.url, autocommit=True) as c:
        c.execute("UPDATE agent_runtimes SET name = 'other-runtime'")
    assert gateway(h, calls).handle_call("docs_search", {"query": "x"}).reason == "NO_ACTIVE_RUN"


def test_unauthenticated_calls_are_denied_before_any_run_lookup_and_audited(h, calls, env):
    start_run(h)
    out = gateway(h, calls).handle_call("docs_search", {"query": "x"}, authenticated=False)
    assert out.reason == "UNAUTHENTICATED" and calls.contexts == []
    assert audit(env)[0][3:5] == ("UNAUTHENTICATED", None)


# ---- tool and grant checks ---------------------------------------------------------------------------------------


def test_unknown_tools_are_denied_and_the_requested_name_is_recorded(h, calls, env):
    run = start_run(h)
    out = gateway(h, calls).handle_call("run_shell_command", {"command": "ls"})
    assert out.reason == "UNKNOWN_TOOL" and calls.contexts == []
    row = audit(env)[0]
    assert row[:6] == (
        "run_shell_command",
        None,
        "denied",
        "UNKNOWN_TOOL",
        run["id"],
        "admin-hermes",
    )
    assert events(h, run["id"])[0]["payload"]["reason"] == "UNKNOWN_TOOL"  # visible in the chat


def test_a_tool_the_owner_switched_off_is_denied(h, calls, env):
    start_run(h)
    g = gateway(h, calls)
    with psycopg.connect(env.url, autocommit=True) as c:
        c.execute("UPDATE gateway_tools SET enabled = false WHERE name = 'docs_search'")
    assert g.handle_call("docs_search", {"query": "x"}).reason == "TOOL_DISABLED"


def activate_without(h, tool):
    d = DEFAULT_ADMIN_SPEC.model_dump(mode="json")
    d["tools"]["grants"] = [g for g in d["tools"]["grants"] if g != tool]
    from paw_server.modules.identity.spec import ActorProfileSpec

    with h.pool.connection() as conn:
        v = identity.create_version(
            conn, "admin-hermes", ActorProfileSpec.model_validate(d), "owner"
        )
        identity.activate(conn, "admin-hermes", v["id"], "owner")


def test_a_tool_not_in_the_runs_profile_is_denied(h, calls):
    activate_without(h, "docs_search")
    start_run(h)
    out = gateway(h, calls).handle_call("docs_search", {"query": "x"})
    assert out.reason == "NOT_GRANTED" and calls.contexts == []
    assert (
        gateway(h, calls).handle_call("paw_system_status", {}).decision == "allowed"
    )  # other grants still work


def test_the_policy_is_the_one_bound_at_claim_time_even_if_a_new_version_is_activated_mid_run(
    h, calls
):
    start_run(h)
    activate_without(h, "docs_search")  # activated while the run is in flight
    assert gateway(h, calls).handle_call("docs_search", {"query": "x"}).decision == "allowed"


# ---- arguments ---------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "args",
    [{}, {"query": ""}, {"query": "x", "workspace_id": "w"}, {"query": "x", "run_id": "other"}, {"query": "x", "extra": 1},
     {"query": 5}, {"query": "x", "top_k": 99}, "not an object", None],
)  # fmt: skip
def test_invalid_or_forged_arguments_are_denied_and_never_reach_the_handler(h, calls, env, args):
    start_run(h)
    out = gateway(h, calls).handle_call("docs_search", args)
    assert out.reason == "INVALID_ARGUMENTS" and calls.contexts == []
    assert audit(env)[0][3] == "INVALID_ARGUMENTS"


def test_a_forged_run_id_argument_cannot_change_whose_run_it_is(h, calls):
    run = start_run(h)
    gateway(h, calls).handle_call(
        "docs_search", {"query": "x", "run_id": "00000000-0000-0000-0000-000000000000"}
    )
    assert calls.contexts == []  # denied, so no handler ran with the forged value
    gateway(h, calls).handle_call("docs_search", {"query": "x"})
    assert str(calls.contexts[0].run_id) == run["id"]


def test_the_model_sees_the_reason_code_but_not_the_validation_details(h, calls):
    start_run(h)
    out = gateway(h, calls).handle_call("docs_search", {"query": "x", "workspace_id": "w"})
    assert out.text == "This tool call was not allowed (INVALID_ARGUMENTS)."


def test_audit_keeps_short_argument_values_only(h, calls, env):
    start_run(h)
    gateway(h, calls).handle_call("docs_search", {"query": "x" * 1000, "top_k": 3})
    args = audit(env)[0][10]
    assert len(args["query"]) == 200 and args["top_k"] == 3
    assert redact_args({"a": {"nested": 1}, "b": [1]}) == {"a": "[dict]", "b": "[list]"}
    assert redact_args("raw") == {"_invalid": "str"}


# ---- connections, limits, attestation ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "reason"),
    [
        (None, "NOT_CONNECTED"),
        ("needs_reauth", "CONNECTION_REVOKED"),
        ("revoked", "CONNECTION_REVOKED"),
    ],
)
def test_provider_tools_need_an_active_connection(h, calls, status, reason):
    start_run(h)
    out = gateway(h, calls, connection_status=lambda owner, provider: status).handle_call(
        "calendar_list_events", {}
    )
    assert out.reason == reason and calls.contexts == []


def test_an_active_connection_lets_the_calendar_tool_run(h, calls):
    start_run(h)
    seen = []
    g = gateway(
        h,
        calls,
        connection_status=lambda owner, provider: (seen.append((owner, provider)), "active")[1],
    )
    assert g.handle_call("calendar_list_events", {"days": 3}).decision == "allowed"
    assert seen == [
        ("owner", "google_calendar")
    ]  # the owner of the run's session, not anything the model said


def test_calls_per_run_are_limited_and_denials_count(h, calls, env):
    start_run(h)
    g = gateway(h, calls, gateway_max_calls_per_run=3)
    assert [g.handle_call("paw_system_status", {}).decision for _ in range(2)] == ["allowed"] * 2
    assert g.handle_call("nope_tool", {}).reason == "UNKNOWN_TOOL"  # a denial still uses up a call
    assert g.handle_call("paw_system_status", {}).reason == "RATE_LIMITED"
    assert len(audit(env)) == 4


@pytest.mark.parametrize(
    ("report", "reason"), [("missing", "ATTESTATION_MISSING"), ("mismatch", "ATTESTATION_MISMATCH")]
)
def test_required_attestation_fails_closed(h, calls, report, reason):
    start_run(h)
    g = gateway(h, calls, gateway_attestation="required", attestation=lambda run_id: report)
    out = g.handle_call("paw_system_status", {})
    assert out.reason == reason and calls.contexts == []
    ok = gateway(h, calls, gateway_attestation="required", attestation=lambda run_id: "ok")
    assert ok.handle_call("paw_system_status", {}).decision == "allowed"


def test_attestation_is_not_consulted_when_switched_off(h, calls):
    start_run(h)
    g = gateway(h, calls, gateway_attestation="off", attestation=lambda run_id: "missing")
    assert g.handle_call("paw_system_status", {}).decision == "allowed"


# ---- failures inside a tool --------------------------------------------------------------------------------------


def test_a_crashing_handler_is_an_error_row_and_leaks_nothing_to_the_model(h, calls, env):
    run = start_run(h)
    calls.fail = True
    out = gateway(h, calls).handle_call("docs_search", {"query": "x"})
    assert (out.is_error, out.decision, out.reason) == (True, "error", "HANDLER_ERROR")
    assert SECRET_TEXT not in out.text
    row = audit(env)[0]
    assert (
        row[2:4] == ("error", "HANDLER_ERROR")
        and row[9] == "RuntimeError"
        and SECRET_TEXT not in str(row)
    )
    assert events(h, run["id"])[0]["payload"]["decision"] == "error"


def test_a_tool_without_a_handler_is_an_error_not_a_crash(h, env):
    start_run(h)
    reg = ToolRegistry(builtin_specs())  # no handlers attached
    with h.pool.connection() as conn:
        sync_registry(conn, reg)
    out = GatewayService(h.pool, reg, h.env.settings()).handle_call("paw_system_status", {})
    assert out.reason == "HANDLER_ERROR" and audit(env)[0][9] == "NotImplementedError"


# ---- audit completeness ------------------------------------------------------------------------------------------


def test_audit_row_count_equals_call_count_across_every_outcome(h, calls, env):
    start_run(h)
    g = gateway(h, calls, connection_status=lambda o, p: None)
    attempts = [("docs_search", {"query": "a"}), ("docs_search", {}), ("nope", {}), ("calendar_list_events", {}),
                ("paw_system_status", {}), ("docs_search", {"query": "b", "actor_id": "x"})]  # fmt: skip
    outcomes = [g.handle_call(t, a) for t, a in attempts]
    rows = audit(env)
    assert len(rows) == len(attempts)
    assert {str(o.invocation_id) for o in outcomes}  # distinct ids
    assert len({r[11] for r in rows}) == len(rows)  # distinct trace ids
    assert [r[3] for r in rows] == [
        "OK",
        "INVALID_ARGUMENTS",
        "UNKNOWN_TOOL",
        "NOT_CONNECTED",
        "OK",
        "INVALID_ARGUMENTS",
    ]


def test_gateway_and_worker_can_append_events_to_the_same_run_at_once_without_losing_any(h):
    run = start_run(h)
    rid = UUID(run["id"])
    errors = []

    def writer(prefix):
        try:
            for i in range(25):
                with h.pool.connection() as conn:
                    lc.append_event(
                        conn, rid, "message.delta", {"text": f"{prefix}{i}"}, f"{prefix}:{i}"
                    )
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    ts = [threading.Thread(target=writer, args=(p,)) for p in "abc"]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert errors == []
    with psycopg.connect(h.env.url, autocommit=True) as c:
        seqs = [
            r[0]
            for r in c.execute(
                "SELECT seq FROM agent_run_events WHERE run_id = %s ORDER BY seq", (rid,)
            )
        ]
    assert seqs == list(range(1, len(seqs) + 1)) and len(seqs) >= 75
