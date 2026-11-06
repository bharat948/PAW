"""The MCP endpoint end to end with the real MCP SDK client: tools/list, tools/call, bearer auth (002D-R01)."""

import asyncio
import socket
import threading
import time
from dataclasses import replace

import httpx
import pytest
import uvicorn
from mcp.client import Client

from paw_server.modules.gateway.builtin import builtin_specs
from paw_server.modules.gateway.registry import ToolRegistry, sync_registry
from paw_server.modules.gateway.server import build_app, build_mcp_server
from paw_server.modules.gateway.service import GatewayService
from tests.gateway.test_service import audit, start_run
from tests.test_agent_integration import Harness

pytestmark = pytest.mark.integration
TOKEN = "gateway-test-token-0123456789abcdef"


@pytest.fixture
def h(env):
    x = Harness(env)
    yield x
    x.pool.close()


@pytest.fixture
def service(h):
    reg = ToolRegistry(
        [
            replace(s, handler=lambda ctx, args: {"hello": "world", "tool_args": args})
            for s in builtin_specs()
        ]
    )
    with h.pool.connection() as conn:
        sync_registry(conn, reg)
    return GatewayService(h.pool, reg, h.env.settings()), reg


def run_async(coro):
    return asyncio.run(coro)


# ---- protocol, in memory ------------------------------------------------------------------------------------------


def test_tools_list_returns_strict_schemas_and_accurate_side_effect_hints(service):
    svc, reg = service

    async def go():
        async with Client(build_mcp_server(svc, reg)) as c:
            return (await c.list_tools()).tools

    tools = {t.name: t for t in run_async(go())}
    assert sorted(tools) == reg.names()
    for t in tools.values():
        assert t.input_schema["additionalProperties"] is False
        assert t.annotations.read_only_hint is (reg.get(t.name).side_effect_class == "read")
        assert t.annotations.destructive_hint is False
    assert tools["calendar_list_events"].input_schema["properties"]["days"]["maximum"] == 31


def test_tools_call_allowed_and_denied_map_to_mcp_results(h, service):
    start_run(h)
    svc, reg = service

    async def go():
        async with Client(build_mcp_server(svc, reg)) as c:
            ok = await c.call_tool("docs_search", {"query": "x"})
            bad = await c.call_tool("docs_search", {"query": "x", "workspace_id": "w"})
            unknown = await c.call_tool("run_shell_command", {"command": "ls"})
            return ok, bad, unknown

    ok, bad, unknown = run_async(go())
    assert ok.is_error is False and '"hello": "world"' in ok.content[0].text
    assert bad.is_error is True and "INVALID_ARGUMENTS" in bad.content[0].text
    assert unknown.is_error is True and "UNKNOWN_TOOL" in unknown.content[0].text


def test_every_mcp_call_is_audited_once(h, service, env):
    start_run(h)
    svc, reg = service

    async def go():
        async with Client(build_mcp_server(svc, reg)) as c:
            await c.call_tool("paw_system_status", {})
            await c.call_tool("docs_search", {})
            await c.call_tool("nope_tool", {})

    run_async(go())
    assert [r[3] for r in audit(env)] == ["OK", "INVALID_ARGUMENTS", "UNKNOWN_TOOL"]


# ---- HTTP: authentication -----------------------------------------------------------------------------------------


@pytest.fixture
def http_gateway(service):
    svc, reg = service
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    server = uvicorn.Server(
        uvicorn.Config(build_app(svc, reg, TOKEN), host="127.0.0.1", port=port, log_level="error")
    )
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    t.join(timeout=5)


def test_requests_without_the_runtime_credential_get_401(http_gateway):
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
    hdr = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
    for bearer in (None, "Bearer wrong", "Bearer", TOKEN, f"Basic {TOKEN}", f"bearer {TOKEN}"):
        headers = {**hdr, **({"Authorization": bearer} if bearer is not None else {})}
        r = httpx.post(f"{http_gateway}/mcp", json=body, headers=headers)
        assert r.status_code == 401, bearer
    assert (
        httpx.get(f"{http_gateway}/_health").status_code == 200
    )  # liveness needs no credential and shows nothing


def test_the_real_protocol_works_over_http_with_the_credential_and_rejects_a_rotated_one(
    h, http_gateway, env
):
    start_run(h)

    async def go(token):
        async with httpx.AsyncClient(headers={"Authorization": f"Bearer {token}"}) as client:
            from mcp.client.streamable_http import streamable_http_client

            async with streamable_http_client(f"{http_gateway}/mcp", http_client=client) as (
                r,
                w,
                *_,
            ):
                from mcp.client import ClientSession

                async with ClientSession(r, w) as session:
                    await session.initialize()
                    tools = await session.list_tools()
                    res = await session.call_tool("paw_system_status", {})
                    return [t.name for t in tools.tools], res.is_error

    names, is_error = run_async(go(TOKEN))
    assert "calendar_list_events" in names and is_error is False
    assert audit(env)[0][3] == "OK"
    refused = False
    try:  # a rotated or stolen-and-revoked credential cannot connect
        run_async(go("rotated-credential-0123456789abcdef"))
    except BaseException:  # noqa: BLE001  (the SDK surfaces the 401 as an ExceptionGroup)
        refused = True
    assert refused
    assert len(audit(env)) == 1  # the rejected connection never reached the policy engine


def test_a_short_token_is_refused_at_startup(service):
    svc, reg = service
    with pytest.raises(ValueError):
        build_app(svc, reg, "short")
