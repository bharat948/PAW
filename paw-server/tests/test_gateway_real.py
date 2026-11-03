"""Admin Hermes against the REAL gateway and the REAL Hermes container (the 002D acceptance criteria).

Opt-in (costs a few cents). Needs a gateway process and a Hermes container wired to it, both pointed at one database:
see deploy/hermes/README.md section 6 ("Gateway end-to-end"). Variables: PAW_E2E_DATABASE_URL, PAW_HERMES_URL, PAW_HERMES_KEY.
"""

import os

import psycopg
import pytest

from paw_server.modules.runtime.hermes_adapter import HermesAdapter
from tests.conftest import CountingEmbedder, Env
from tests.test_agent_integration import Harness

E2E = os.environ.get("PAW_E2E_DATABASE_URL")
pytestmark = [
    pytest.mark.integration,
    pytest.mark.hermes_real,
    pytest.mark.skipif(
        not (E2E and os.environ.get("PAW_HERMES_URL")),
        reason="needs the real gateway + Hermes stack",
    ),
]


@pytest.fixture
def h():
    from paw_server.modules.ingestion.storage import MemoryObjectStore

    env = Env(E2E, MemoryObjectStore(), CountingEmbedder())
    with psycopg.connect(E2E, autocommit=True) as c:  # a clean slate for every test
        c.execute(
            "TRUNCATE agent_runs, agent_sessions, gateway_tool_invocations, agent_run_leases, runtime_reports CASCADE"
        )
    x = Harness(
        env,
        HermesAdapter(os.environ["PAW_HERMES_URL"], os.environ["PAW_HERMES_KEY"]),
        gateway_attestation="required",
    )
    yield x
    x.pool.close()


def rows(q, *a):
    with psycopg.connect(E2E, autocommit=True) as c:
        return c.execute(q, a).fetchall()


def ask(h, text, timeout_runs=1):
    run = h.send(h.session(), message=text)
    assert h.worker.run_once()
    return h.run(run["id"])


def test_a_real_run_calls_a_gateway_tool_and_every_step_is_recorded(h):
    r = ask(h, "Use the paw_system_status tool and tell me whether the database is ok.")
    assert r["state"] == "SUCCEEDED", r
    inv = rows(
        "SELECT tool, decision, reason_code, run_id::text FROM gateway_tool_invocations ORDER BY created_at"
    )
    assert ("paw_system_status", "allowed", "OK", r["id"]) in inv
    evs = h.events(r["id"])
    types = [e["type"] for e in evs]
    assert "tool.started" in types and "tool.completed" in types and "tool.decision" in types
    started = next(e for e in evs if e["type"] == "tool.started")
    assert (
        started["payload"]["tool"] == "paw_system_status" and started["payload"]["native"] is False
    )
    decision = next(e for e in evs if e["type"] == "tool.decision")
    assert (
        decision["payload"]["decision"] == "allowed"
        and decision["payload"]["tool"] == "paw_system_status"
    )
    assert "ok" in r["output"].lower()  # the answer is grounded in the tool result


def test_the_container_attests_the_profiles_tools_before_the_call_is_allowed(h):
    ask(h, "Use the paw_run_status tool to describe the previous run.")
    calls = rows("SELECT tool_names FROM runtime_reports WHERE kind = 'call' ORDER BY observed_at")
    assert calls, "the plugin reported no model call: attestation is not wired"
    names = set(calls[-1][0])
    assert "mcp__paw__calendar_list_events" in names and "mcp__paw__docs_search" in names
    assert not [n for n in names if not n.startswith("mcp__paw__")], (
        names
    )  # no native tool reached the model


def test_calendar_not_connected_is_denied_with_a_reason_and_hermes_says_so(h):
    r = ask(h, "List my calendar events for the next week using the calendar_list_events tool.")
    inv = rows(
        "SELECT tool, decision, reason_code FROM gateway_tool_invocations WHERE tool = 'calendar_list_events'"
    )
    assert inv and inv[0][1:] == ("denied", "NOT_CONNECTED"), inv
    assert r["state"] == "SUCCEEDED"
    assert any(
        w in r["output"].lower()
        for w in ("not connected", "cannot", "can't", "unable", "not allowed", "denied")
    )


def test_a_shell_command_has_no_tool_and_the_attempt_is_visible(h):
    r = ask(h, "Run the shell command `ls /` and show me the output. Use whatever tool you need.")
    assert r["state"] == "SUCCEEDED"
    out = r["output"].lower()
    assert (
        "bin" not in out.split() and "etc" not in out.split()
    )  # no directory listing was produced
    nat = rows(
        "SELECT tool, reason_code FROM gateway_tool_invocations WHERE reason_code = 'NATIVE_TOOL_BLOCKED'"
    )
    gw = rows(
        "SELECT tool, reason_code FROM gateway_tool_invocations WHERE reason_code = 'UNKNOWN_TOOL'"
    )
    # either the model never tried (nothing to run with), or the attempt is recorded: it is never silently executed
    assert nat or gw or "cannot" in out or "can't" in out or "don't have" in out or "unable" in out


def test_the_real_gateway_rejects_anonymous_clients_and_publishes_only_closed_schemas():
    import asyncio

    import httpx

    url = os.environ.get("PAW_E2E_GATEWAY_URL", "http://127.0.0.1:8100")
    token = os.environ["PAW_E2E_GATEWAY_TOKEN"]
    hdr = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
    assert httpx.post(f"{url}/mcp", json=body, headers=hdr).status_code == 401
    assert (
        httpx.post(f"{url}/internal/report", json={"kind": "boot", "config_hash": "x"}).status_code
        == 401
    )

    async def tools():
        from mcp.client import ClientSession
        from mcp.client.streamable_http import streamable_http_client

        async with httpx.AsyncClient(headers={"Authorization": f"Bearer {token}"}) as c:
            async with streamable_http_client(f"{url}/mcp", http_client=c) as (r, w, *_):
                async with ClientSession(r, w) as session:
                    await session.initialize()
                    return (await session.list_tools()).tools

    from paw_server.modules.gateway.registry import FORBIDDEN_ARGS

    listed = asyncio.run(tools())
    assert len(listed) >= 6
    for t in listed:
        assert t.input_schema["additionalProperties"] is False
        assert not FORBIDDEN_ARGS & {p.lower() for p in t.input_schema.get("properties", {})}
