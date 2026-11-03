"""Runtime reports and attestation (002C-R04, 002D-R06): fail closed when the container does not match the profile."""

import importlib.util
import json
from pathlib import Path

import psycopg
import pytest

from paw_server.modules.gateway import attestation as att
from paw_server.modules.gateway.attestation import ReportError, config_hash, expected_tool_names
from paw_server.modules.identity import service as identity
from paw_server.modules.identity.spec import DEFAULT_ADMIN_SPEC
from paw_server.profile_export import render
from tests.conftest import AUTH
from tests.gateway.test_service import audit, events, gateway, start_run
from tests.test_agent_integration import Harness

pytestmark = pytest.mark.integration
GRANTS = list(DEFAULT_ADMIN_SPEC.tools.grants)
PREFIXED = expected_tool_names(GRANTS)


@pytest.fixture
def h(env):
    x = Harness(env)
    yield x
    x.pool.close()


@pytest.fixture
def calls():
    from tests.gateway.test_service import Calls

    return Calls()


def report(h, body):
    with h.pool.connection() as conn:
        return att.record_report(conn, "hermes", body)


def call_report(run_id="h1", names=None, **kw):
    return {
        "kind": "call",
        "hermes_run_id": run_id,
        "tool_names": PREFIXED if names is None else names,
        "model": "m",
        **kw,
    }


# ---- storing reports ----------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "body",
    ["text", None, [], {}, {"kind": "other"}, {"kind": "call"}, {"kind": "call", "hermes_run_id": "h"},
     {"kind": "call", "hermes_run_id": "h", "tool_names": "x"}, {"kind": "call", "hermes_run_id": "h", "tool_names": [1]},
     {"kind": "call", "hermes_run_id": "h", "tool_names": ["a"] * 65}, {"kind": "call", "hermes_run_id": "h", "tool_names": ["x" * 101]},
     {"kind": "call", "hermes_run_id": "x" * 101, "tool_names": []}, {"kind": "blocked"}, {"kind": "boot"},
     {"kind": "boot", "config_hash": 5}, {"kind": "boot", "config_hash": "h" * 129}],
)  # fmt: skip
def test_malformed_reports_are_rejected_and_nothing_is_stored(h, env, body):
    with pytest.raises(ReportError):
        report(h, body)
    with psycopg.connect(env.url, autocommit=True) as c:
        assert c.execute("SELECT count(*) FROM runtime_reports").fetchone() == (0,)


def test_valid_reports_of_each_kind_are_stored(h, env):
    assert report(h, {"kind": "boot", "config_hash": "abc"}) == "boot"
    assert report(h, call_report()) == "call"
    assert (
        report(h, {"kind": "blocked", "hermes_run_id": "h1", "tool_name": "terminal"}) == "blocked"
    )
    with psycopg.connect(env.url, autocommit=True) as c:
        assert c.execute("SELECT kind, tool_names IS NOT NULL FROM runtime_reports ORDER BY kind").fetchall() == [
            ("blocked", False), ("boot", False), ("call", True)]  # fmt: skip


def test_tool_names_are_stored_sorted_and_deduplicated(h, env):
    report(h, call_report(names=["b", "a", "a"]))
    with psycopg.connect(env.url, autocommit=True) as c:
        assert c.execute("SELECT tool_names FROM runtime_reports").fetchone() == (["a", "b"],)


# ---- per-call attestation -----------------------------------------------------------------------------------------


def verdict(h, run_id, grants=GRANTS):
    with h.pool.connection() as conn:
        return att.call_attestation(conn, run_id, grants)


def test_attestation_is_ok_only_when_the_model_call_carried_exactly_the_granted_tools(h):
    assert (
        verdict(h, "h1") == "missing"
        and verdict(h, None) == "missing"
        and verdict(h, "") == "missing"
    )
    report(h, call_report())
    assert verdict(h, "h1") == "ok"
    assert verdict(h, "other-run") == "missing"  # reports are per run
    report(h, call_report(run_id="h2", names=PREFIXED + ["terminal"]))  # a native tool slipped in
    assert verdict(h, "h2") == "mismatch"
    report(
        h, call_report(run_id="h3", names=PREFIXED[:-1])
    )  # a granted tool missing: config drift too
    assert verdict(h, "h3") == "mismatch"
    report(
        h, call_report(run_id="h4", names=[g for g in GRANTS])
    )  # unprefixed names are not the gateway's
    assert verdict(h, "h4") == "mismatch"


def test_the_latest_report_for_a_run_wins(h):
    report(h, call_report(names=PREFIXED + ["terminal"]))
    assert verdict(h, "h1") == "mismatch"
    report(h, call_report())
    assert verdict(h, "h1") == "ok"


def test_expected_names_follow_the_runs_own_grants(h):
    report(h, call_report(names=expected_tool_names(["docs_search"])))
    assert verdict(h, "h1", grants=["docs_search"]) == "ok"
    assert verdict(h, "h1", grants=GRANTS) == "mismatch"


# ---- the gateway uses it, and fails closed ------------------------------------------------------------------------


def required_gateway(h, calls):
    return gateway(h, calls, gateway_attestation="required")


def hermes_run(h):
    run = start_run(h)  # RUNNING with hermes_run_id 'h1'
    return run


def test_a_tool_call_without_a_report_is_denied(h, calls):
    hermes_run(h)
    out = required_gateway(h, calls).handle_call("paw_system_status", {})
    assert out.reason == "ATTESTATION_MISSING" and calls.contexts == []


def test_a_matching_report_lets_the_call_through(h, calls):
    hermes_run(h)
    report(h, call_report())
    assert required_gateway(h, calls).handle_call("paw_system_status", {}).decision == "allowed"


def test_a_report_with_an_extra_tool_denies_every_call_of_that_run(h, calls):
    hermes_run(h)
    report(h, call_report(names=PREFIXED + ["terminal"]))
    g = required_gateway(h, calls)
    assert g.handle_call("paw_system_status", {}).reason == "ATTESTATION_MISMATCH"
    assert g.handle_call("docs_search", {"query": "x"}).reason == "ATTESTATION_MISMATCH"
    report(h, call_report())  # the next model call is clean again
    assert g.handle_call("paw_system_status", {}).decision == "allowed"


def test_a_report_for_another_run_does_not_attest_this_one(h, calls):
    hermes_run(h)
    report(h, call_report(run_id="somebody-elses-run"))
    assert (
        required_gateway(h, calls).handle_call("paw_system_status", {}).reason
        == "ATTESTATION_MISSING"
    )


def test_attestation_off_ignores_reports(h, calls):
    hermes_run(h)
    assert (
        gateway(h, calls, gateway_attestation="off").handle_call("paw_system_status", {}).decision
        == "allowed"
    )


# ---- blocked native tool calls become visible ---------------------------------------------------------------------


def test_a_blocked_native_call_is_audited_and_shown_in_the_runs_events(h, calls, env):
    run = hermes_run(h)
    g = required_gateway(h, calls)
    g.receive_report({"kind": "blocked", "hermes_run_id": "h1", "tool_name": "terminal"})
    row = audit(env)[0]
    assert (row[0], row[2], row[3], row[4]) == (
        "terminal",
        "denied",
        "NATIVE_TOOL_BLOCKED",
        run["id"],
    )
    ev = events(h, run["id"])[0]["payload"]
    assert (
        ev["tool"] == "terminal"
        and ev["decision"] == "denied"
        and ev["reason"] == "NATIVE_TOOL_BLOCKED"
    )


def test_a_blocked_call_from_an_unknown_run_is_still_audited(h, calls, env):
    required_gateway(h, calls).receive_report(
        {"kind": "blocked", "hermes_run_id": "nope", "tool_name": "web_search"}
    )
    assert audit(env)[0][3:5] == ("NATIVE_TOOL_BLOCKED", None)


# ---- the container's applied config vs the active profile ---------------------------------------------------------


def active_fragment(h):
    with h.pool.connection() as conn:
        identity.ensure_default_profile(conn)
        return identity.get_active(conn, "admin-hermes").compiled.config_fragment


def status(h):
    with h.pool.connection() as conn:
        a = identity.get_active(conn, "admin-hermes")
        return att.runtime_status(conn, "hermes", a.compiled.config_fragment if a else None)


def test_runtime_status_unknown_matches_mismatch(h):
    frag = active_fragment(h)
    assert status(h).state == "unknown"
    report(h, {"kind": "boot", "config_hash": "stale-hash"})
    assert status(h).state == "mismatch"
    report(h, {"kind": "boot", "config_hash": config_hash(frag)})
    assert status(h).state == "matches"  # the latest boot report wins


def test_the_hash_is_stable_and_order_independent():
    a = {"model": "m", "max_iterations": 5, "mcp_include": ["x"]}
    assert config_hash(a) == config_hash(dict(reversed(list(a.items())))) and config_hash(
        a
    ) != config_hash({**a, "model": "n"})


def test_the_exported_container_settings_carry_the_same_hash_the_gateway_expects():
    frag = {
        "model": "gpt-4o-mini",
        "max_iterations": 25,
        "mcp_include": ["docs_search", "paw_run_status"],
    }
    lines = dict(line.split("=", 1) for line in render(frag))
    assert lines["PAW_MCP_INCLUDE"] == "docs_search, paw_run_status"
    assert (
        lines["HERMES_CONFIG_SET"]
        == "model.provider=openai-api;model.default=gpt-4o-mini;agent.max_turns=25"
    )
    assert lines["PAW_CONFIG_HASH"] == config_hash(frag)
    assert (
        "model.provider=other"
        in dict(line.split("=", 1) for line in render(frag, "other"))["HERMES_CONFIG_SET"]
    )


def test_runtime_health_and_profile_show_a_config_mismatch_as_not_ready_and_restart_required(
    h, env
):
    c = env.client(runtime=h.rt, gateway_attestation="required")
    base = c.get("/v1/runtime/health", headers=AUTH).json()
    assert (
        base["config"] == "unknown"
        and base["ready"] is False
        and "config_unknown" in base["missing"]
    )
    frag = active_fragment(h)
    report(h, {"kind": "boot", "config_hash": config_hash(frag)})
    ok = c.get("/v1/runtime/health", headers=AUTH).json()
    assert ok["config"] == "matches" and ok["ready"] is True and ok["missing"] == []
    assert c.get("/v1/agent/profile", headers=AUTH).json()["restart_required"] is False
    # the owner activates a version with another tool list; the container still runs the old one
    d = DEFAULT_ADMIN_SPEC.model_dump(mode="json")
    d["tools"]["grants"] = ["docs_search"]
    v = c.post("/v1/agent/profile/versions", json={"spec": d}, headers=AUTH).json()
    c.post(f"/v1/agent/profile/versions/{v['id']}/activate", json={"confirm": True}, headers=AUTH)
    body = c.get("/v1/agent/profile", headers=AUTH).json()
    assert body["runtime_config"] == "mismatch" and body["restart_required"] is True
    health = c.get("/v1/runtime/health", headers=AUTH).json()
    assert health["ready"] is False and "config_mismatch" in health["missing"]
    # restart: the container boots with the new settings and reports the new hash
    with h.pool.connection() as conn:
        new = identity.get_active(conn, "admin-hermes").compiled.config_fragment
    report(h, {"kind": "boot", "config_hash": config_hash(new)})
    assert c.get("/v1/agent/profile", headers=AUTH).json()["restart_required"] is False


def test_with_attestation_off_a_missing_boot_report_does_not_block_readiness(h, env):
    c = env.client(runtime=h.rt)  # default: off
    assert c.get("/v1/runtime/health", headers=AUTH).json()["ready"] is True


# ---- HTTP endpoint ------------------------------------------------------------------------------------------------

TOKEN = "gateway-test-token-0123456789abcdef"


@pytest.fixture
def http(h, calls):
    import asyncio

    from paw_server.modules.gateway.server import build_app
    from tests.gateway.test_service import make_registry

    svc = gateway(h, calls)
    app = build_app(svc, make_registry(calls), TOKEN)

    class Client:
        def post(self, path, body=None, token=TOKEN, raw=None):
            async def go():
                import httpx

                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app), base_url="http://gw"
                ) as c:
                    headers = {"Authorization": f"Bearer {token}"} if token else {}
                    return await c.post(
                        path, content=raw if raw is not None else json.dumps(body), headers=headers
                    )

            return asyncio.run(go())

    return Client()


def test_the_report_endpoint_needs_the_runtime_credential(http):
    assert http.post("/internal/report", call_report(), token=None).status_code == 401
    assert (
        http.post(
            "/internal/report", call_report(), token="wrong-token-0123456789abcdef"
        ).status_code
        == 401
    )


def test_the_report_endpoint_stores_valid_reports_and_rejects_bad_ones(http, h, env):
    r = http.post("/internal/report", call_report())
    assert r.status_code == 200 and r.json() == {"ok": True, "kind": "call"}
    assert http.post("/internal/report", {"kind": "nope"}).status_code == 400
    assert http.post("/internal/report", raw="not json").status_code == 400
    assert (
        http.post(
            "/internal/report",
            raw=json.dumps({"kind": "call", "hermes_run_id": "h", "tool_names": ["x" * 20000]}),
        ).status_code
        == 400
    )
    with psycopg.connect(env.url, autocommit=True) as c:
        assert c.execute("SELECT count(*) FROM runtime_reports").fetchone() == (1,)


# ---- the Hermes plugin (runs inside the container; stdlib only) ---------------------------------------------------

PLUGIN = (
    Path(__file__).resolve().parents[3]
    / "deploy"
    / "hermes"
    / "plugins"
    / "paw-guard"
    / "__init__.py"
)


@pytest.fixture
def plugin(monkeypatch):
    spec = importlib.util.spec_from_file_location("paw_guard_under_test", PLUGIN)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    sent = []
    monkeypatch.setattr(mod, "_report", sent.append)
    monkeypatch.delenv("PAW_GUARD_MODE", raising=False)
    mod.sent = sent
    return mod


def test_plugin_reports_the_exact_tool_names_of_each_model_call(plugin):
    body = {
        "tools": [
            {"type": "function", "function": {"name": "mcp__paw__docs_search"}},
            {"type": "function", "name": "terminal"},
        ]
    }
    plugin._attest(
        request={"body": body}, session_id="run_1", model="gpt-4o-mini", provider="openai-api"
    )
    assert plugin.sent == [{"kind": "call", "hermes_run_id": "run_1", "tool_names": ["mcp__paw__docs_search", "terminal"],
                            "model": "gpt-4o-mini", "provider": "openai-api"}]  # fmt: skip


def test_plugin_reports_nothing_without_a_run_id_and_handles_odd_payloads(plugin):
    plugin._attest(request={"body": {"tools": []}}, model="m")
    plugin._attest(request=None, session_id="r")
    plugin._attest(request={"body": {"tools": ["junk", 5, None]}}, session_id="r")
    assert [s["tool_names"] for s in plugin.sent] == [[], []]


def test_plugin_blocks_native_tools_and_tells_the_gateway(plugin):
    res = plugin._guard(tool_name="terminal", args={"command": "ls"}, session_id="run_9")
    assert res["action"] == "block" and "not permitted" in res["message"]
    assert plugin.sent == [{"kind": "blocked", "hermes_run_id": "run_9", "tool_name": "terminal"}]


def test_plugin_allows_gateway_tools_silently(plugin):
    assert plugin._guard(tool_name="mcp__paw__docs_search", args={}, session_id="r") is None
    assert plugin.sent == []


def test_plugin_modes(plugin, monkeypatch):
    monkeypatch.setenv("PAW_GUARD_MODE", "off")
    assert plugin._guard(tool_name="terminal") is None
    monkeypatch.setenv("PAW_GUARD_MODE", "raise")
    with pytest.raises(RuntimeError):
        plugin._guard(tool_name="terminal")


def test_plugin_report_url_and_failures_never_raise(plugin, monkeypatch):
    monkeypatch.setenv("PAW_MCP_URL", "http://host.docker.internal:8100/mcp")
    assert plugin._report_url() == "http://host.docker.internal:8100/internal/report"
    monkeypatch.delenv("PAW_MCP_URL")
    assert plugin._report_url() is None
    monkeypatch.setenv(
        "PAW_MCP_URL", "http://127.0.0.1:1/mcp"
    )  # nothing listens: must be swallowed
    monkeypatch.setenv("PAW_MCP_TOKEN", "t")
    plugin._send({"kind": "boot", "config_hash": "h"})


def test_plugin_registers_both_hooks_and_the_boot_report(plugin, monkeypatch):
    hooks = []
    ctx = type("Ctx", (), {"register_hook": lambda self, name, fn: hooks.append(name)})()
    monkeypatch.setenv("PAW_CONFIG_HASH", "abc123")
    plugin.register(ctx)
    assert hooks == ["pre_tool_call", "pre_api_request"]
    assert plugin.sent == [{"kind": "boot", "config_hash": "abc123"}]
    plugin.sent.clear()
    monkeypatch.delenv("PAW_CONFIG_HASH")
    plugin.register(ctx)
    assert plugin.sent == []
