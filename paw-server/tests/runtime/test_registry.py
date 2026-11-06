"""Two-actor acceptance with PostgreSQL, real MCP HTTP, and a fake model provider.

These tests exercise the registry boundary rather than merely testing its helpers.
"""

import asyncio
import json
import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from uuid import UUID

import httpx
import psycopg
import pytest
import uvicorn
from mcp.client import ClientSession
from mcp.client.streamable_http import streamable_http_client

from paw_server.modules.gateway import attestation
from paw_server.modules.gateway.builtin import builtin_specs
from paw_server.modules.gateway.registry import ToolRegistry, sync_registry
from paw_server.modules.gateway.server import build_app
from paw_server.modules.gateway.service import GatewayService
from paw_server.modules.identity import service as identity
from paw_server.modules.identity.spec import DEFAULT_ADMIN_SPEC
from paw_server.modules.runtime import lifecycle as lc
from paw_server.modules.runtime import registry
from paw_server.modules.runtime.agent_worker import AgentWorker
from tests.conftest import AUTH
from tests.costs.test_proxy import GATEWAY_TOKEN, PROXY_TOKEN, Provider, body, make_proxy
from tests.runtime.fakes import FakeRuntime, completed, delta
from tests.test_agent_integration import Harness

pytestmark = pytest.mark.integration
SECOND_GATEWAY = "second-runtime-gateway-" + "s" * 24
SECOND_MODEL = "second-runtime-model-" + "m" * 24


@pytest.fixture
def pair(env):
    h = Harness(env)
    with h.pool.connection() as conn:
        conn.execute(
            "INSERT INTO actors(id,kind,display_name) VALUES ('research-hermes','agent','Research')"
        )
        conn.commit()
        p = identity.create_version(conn, "research-hermes", DEFAULT_ADMIN_SPEC, "owner")
        identity.activate(conn, "research-hermes", p["id"], "owner")
        rid = registry.register(
            conn,
            "research",
            "research-hermes",
            endpoint_url="http://127.0.0.1:9999",
            key_env="PAW_RESEARCH_KEY",
        )
        registry.credential(conn, rid, "gateway", SECOND_GATEWAY)
        registry.credential(conn, rid, "model", SECOND_MODEL)
    rt = FakeRuntime()
    worker = AgentWorker(h.pool, rt, env.settings(agent_runtime_name="research"))
    yield h, worker, rt
    h.c.__exit__(None, None, None)
    h.pool.close()


def enqueue(h, actor="admin-hermes"):
    response = h.c.post("/v1/agent-sessions", json={"agent_actor_id": actor}, headers=AUTH)
    assert response.status_code == 201, response.text
    return h.send(response.json()["id"])


def claim(h, worker, run):
    with h.pool.connection() as conn:
        got = lc.claim_next(conn, worker.runtime_id)
        assert str(got["id"]) == run["id"]
        p = identity.get_active(conn, got["executing_actor_id"])
        lc.bind_profile(
            conn,
            got["id"],
            profile_version_id=p.version_id,
            version=p.version,
            compiled_hash=p.compiled.compiled_hash,
            max_run_seconds=600,
        )
        lc.move(conn, got["id"], "RUNNING", hermes_run_id="same-hermes-id")


def identity_for(h, token, purpose="gateway"):
    with h.pool.connection() as conn:
        return registry.authenticate(conn, token, purpose)


def test_workers_route_to_the_session_actor_and_execute_concurrently(pair):
    h, second, rt = pair
    b = enqueue(h, "research-hermes")  # foreign work is older: Admin must skip it
    a = enqueue(h)
    barrier = threading.Barrier(2)

    def script(_):
        barrier.wait(timeout=5)
        return [delta("answer"), completed("answer")]

    h.rt.script = rt.script = script
    with ThreadPoolExecutor(2) as executor:
        results = list(executor.map(lambda w: w.run_once(), [h.worker, second]))
    assert results == [True, True]
    assert [r["run_id"] for r in h.rt.created] == [a["id"]]
    assert [r["run_id"] for r in rt.created] == [b["id"]]
    for run, actor in [(a, "admin-hermes"), (b, "research-hermes")]:
        assert h.run(run["id"])["state"] == "SUCCEEDED"
        with h.pool.connection() as conn:
            rows = conn.execute(
                "SELECT DISTINCT author_actor_id FROM agent_session_messages WHERE run_id=%s AND kind='assistant'",
                (run["id"],),
            ).fetchall()
        assert rows == [{"author_actor_id": actor}]


def test_one_executing_run_per_actor_even_with_two_registered_runtimes(pair):
    h, _, _ = pair
    first, second = enqueue(h), enqueue(h)
    with h.pool.connection() as conn:
        spare = registry.register(conn, "spare-admin", "admin-hermes")

    def take(rid):
        with h.pool.connection() as conn:
            return lc.claim_next(conn, rid)

    with ThreadPoolExecutor(2) as executor:
        results = list(executor.map(take, [h.worker.runtime_id, spare]))
    assert sum(r is not None for r in results) == 1
    active = next(r for r in results if r)
    with h.pool.connection() as conn:
        lc.finish(conn, active["id"], "INTERRUPTED")
        nxt = lc.claim_next(conn, spare)
    assert str(nxt["id"]) in {first["id"], second["id"]} - {str(active["id"])}


def test_schema_refuses_wrong_actor_and_direct_concurrent_execution(pair):
    h, second, _ = pair
    a, b = enqueue(h), enqueue(h)
    with h.pool.connection() as conn:
        with pytest.raises(psycopg.errors.ForeignKeyViolation), conn.transaction():
            conn.execute(
                "UPDATE agent_runs SET runtime_id=%s WHERE id=%s", (second.runtime_id, a["id"])
            )
        lc.claim_next(conn, h.worker.runtime_id)
        with pytest.raises(psycopg.errors.UniqueViolation), conn.transaction():
            conn.execute(
                "UPDATE agent_runs SET state='DISPATCHING',runtime_id=%s WHERE id=%s",
                (h.worker.runtime_id, b["id"]),
            )
        with pytest.raises(psycopg.errors.CheckViolation), conn.transaction():
            conn.execute(
                "UPDATE agent_runtimes SET actor_id='admin-hermes' WHERE id=%s",
                (second.runtime_id,),
            )


def test_hashes_purpose_separation_rotation_and_revocation_survive_bootstrap(pair):
    h, second, _ = pair
    assert identity_for(h, SECOND_GATEWAY).id == second.runtime_id
    assert identity_for(h, SECOND_GATEWAY, "model") is None
    assert identity_for(h, SECOND_MODEL) is None
    fresh = "fresh-research-gateway-" + "f" * 24
    with h.pool.connection() as conn:
        registry.credential(conn, second.runtime_id, "gateway", fresh, rotate=True)
        registry.credential(conn, second.runtime_id, "gateway", SECOND_GATEWAY)
        rows = conn.execute("SELECT token_hash FROM runtime_credentials").fetchall()
    assert all(len(r["token_hash"]) == 64 and SECOND_GATEWAY not in r["token_hash"] for r in rows)
    assert identity_for(h, SECOND_GATEWAY) is None
    assert identity_for(h, fresh).id == second.runtime_id
    with h.pool.connection() as conn:
        registry.stop(conn, second.runtime_id)
        conn.execute(
            "UPDATE agent_runtimes SET desired_state='running' WHERE id=%s", (second.runtime_id,)
        )
        conn.commit()
        registry.credential(conn, second.runtime_id, "gateway", fresh)
    assert identity_for(h, fresh) is None
    with h.pool.connection() as conn:
        registry.credential(conn, h.worker.runtime_id, "gateway", GATEWAY_TOKEN)
        registry.stop(conn, h.worker.runtime_id)
        registry.bootstrap(conn, h.env.settings(gateway_token=GATEWAY_TOKEN))
    assert identity_for(h, GATEWAY_TOKEN) is None


@pytest.fixture
def gateway(pair):
    h, second, rt = pair
    reg = ToolRegistry(
        [
            replace(s, handler=lambda ctx, args: {"actor": ctx.actor_id, "run": str(ctx.run_id)})
            for s in builtin_specs()
        ]
    )
    with h.pool.connection() as conn:
        sync_registry(conn, reg)
    service = GatewayService(h.pool, reg, h.settings)
    provider = Provider()
    proxy = make_proxy(h, provider)
    app = build_app(service, reg, GATEWAY_TOKEN, proxy, PROXY_TOKEN)
    yield app, service, provider
    asyncio.run(proxy.http.aclose())


def request(app, path, token, data):
    async def go():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as c:
            return await c.post(path, headers={"Authorization": f"Bearer {token}"}, json=data)

    return asyncio.run(go())


def test_model_credentials_cannot_spend_for_another_runtime_or_call_tools(pair, gateway):
    h, second, _ = pair
    a, b = enqueue(h), enqueue(h, "research-hermes")
    claim(h, h.worker, a)
    claim(h, second, b)
    app, _, provider = gateway

    async def go():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as c:

            async def call(token, run):
                return await c.post(
                    "/v1/chat/completions",
                    headers={"Authorization": f"Bearer {token}", "X-PAW-Run-Id": run["id"]},
                    json=body(),
                )

            assert (await call(SECOND_MODEL, a)).status_code == 403
            assert (await call(PROXY_TOKEN, b)).status_code == 403
            assert (await call(SECOND_GATEWAY, b)).status_code == 401
            assert provider.requests == []
            assert (await call(SECOND_MODEL, b)).status_code == 200
            assert (await call(PROXY_TOKEN, a)).status_code == 200
            with h.pool.connection() as conn:
                conn.execute(
                    "UPDATE runtime_credentials SET revoked_at=now() WHERE token_hash=%s",
                    (registry.digest(SECOND_MODEL),),
                )
                conn.commit()
            assert (await call(SECOND_MODEL, b)).status_code == 401

    asyncio.run(go())
    assert len(provider.requests) == 2
    with h.pool.connection() as conn:
        charged = conn.execute(
            "SELECT actor_id, run_id FROM actor_cost_attempts WHERE kind='run' ORDER BY actor_id"
        ).fetchall()
    assert {(r["actor_id"], str(r["run_id"])) for r in charged} == {
        ("admin-hermes", a["id"]),
        ("research-hermes", b["id"]),
    }


def test_reports_and_attestation_are_runtime_scoped_even_with_colliding_hermes_ids(pair, gateway):
    h, second, _ = pair
    a, b = enqueue(h), enqueue(h, "research-hermes")
    claim(h, h.worker, a)
    claim(h, second, b)
    app, service, _ = gateway
    malicious = {"kind": "blocked", "hermes_run_id": a["id"], "tool_name": "shell"}
    assert request(app, "/internal/report", SECOND_GATEWAY, malicious).status_code == 400
    malicious.update(kind="call", tool_names=[])
    assert request(app, "/internal/report", SECOND_GATEWAY, malicious).status_code == 400
    own = {
        "kind": "call",
        "hermes_run_id": b["id"],
        "tool_names": attestation.expected_tool_names(DEFAULT_ADMIN_SPEC.tools.grants),
    }
    assert request(app, "/internal/report", SECOND_GATEWAY, own).status_code == 200
    with h.pool.connection() as conn:
        assert (
            attestation.call_attestation(
                conn, None, DEFAULT_ADMIN_SPEC.tools.grants, b["id"], runtime="research"
            )
            == "ok"
        )
        assert (
            attestation.call_attestation(
                conn, None, DEFAULT_ADMIN_SPEC.tools.grants, b["id"], runtime="hermes"
            )
            == "missing"
        )
        assert not conn.execute(
            "SELECT 1 FROM runtime_reports WHERE hermes_run_id=%s", (a["id"],)
        ).fetchone()
    blocked = {"kind": "blocked", "hermes_run_id": "same-hermes-id", "tool_name": "shell"}
    assert request(app, "/internal/report", SECOND_GATEWAY, blocked).status_code == 200
    with h.pool.connection() as conn:
        row = conn.execute(
            "SELECT run_id,actor_id FROM gateway_tool_invocations WHERE reason_code='NATIVE_TOOL_BLOCKED'"
        ).fetchone()
    assert str(row["run_id"]) == b["id"] and row["actor_id"] == "research-hermes"


def test_real_mcp_http_uses_each_requests_identity_and_revocation(pair, gateway):
    h, second, _ = pair
    a, b = enqueue(h), enqueue(h, "research-hermes")
    claim(h, h.worker, a)
    claim(h, second, b)
    app, _, _ = gateway
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, log_level="error"))
    thread = threading.Thread(target=lambda: server.run(sockets=[sock]), daemon=True)
    thread.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)

    async def go():
        replay_done = asyncio.Event()

        async def own(token):
            owned_session = []

            async def capture(response):
                if response.headers.get("mcp-session-id"):
                    owned_session.append(response.headers["mcp-session-id"])

            async with httpx.AsyncClient(
                headers={"Authorization": f"Bearer {token}"}, event_hooks={"response": [capture]}
            ) as client:
                async with streamable_http_client(
                    f"http://127.0.0.1:{port}/mcp", http_client=client
                ) as (read, write):
                    async with ClientSession(read, write) as session:
                        await session.initialize()
                        result = await session.call_tool("paw_system_status", {})
                        if token == GATEWAY_TOKEN:
                            headers = {
                                "Authorization": f"Bearer {SECOND_GATEWAY}",
                                "Mcp-Session-Id": owned_session[0],
                                "Accept": "application/json, text/event-stream",
                            }
                            replay = await client.post(
                                f"http://127.0.0.1:{port}/mcp",
                                headers=headers,
                                json={
                                    "jsonrpc": "2.0",
                                    "id": 999,
                                    "method": "tools/call",
                                    "params": {"name": "paw_system_status", "arguments": {}},
                                },
                            )
                            assert replay.status_code == 404
                            replay = await client.get(
                                f"http://127.0.0.1:{port}/mcp", headers=headers
                            )
                            assert replay.status_code == 404
                            replay_done.set()
                        else:
                            await asyncio.wait_for(replay_done.wait(), timeout=5)
                            with h.pool.connection() as conn:
                                conn.execute(
                                    "UPDATE runtime_credentials SET revoked_at=now() WHERE token_hash=%s",
                                    (registry.digest(SECOND_GATEWAY),),
                                )
                                conn.commit()
                            replay = await client.post(
                                f"http://127.0.0.1:{port}/mcp",
                                headers={"Mcp-Session-Id": owned_session[0]},
                                json={},
                            )
                            assert replay.status_code == 401
                        return result

        results = await asyncio.gather(own(GATEWAY_TOKEN), own(SECOND_GATEWAY))
        assert a["id"] in results[0].content[0].text
        assert b["id"] in results[1].content[0].text
        with h.pool.connection() as conn:
            registry.stop(conn, second.runtime_id)
        async with httpx.AsyncClient() as client:
            res = await client.post(
                f"http://127.0.0.1:{port}/mcp",
                headers={"Authorization": f"Bearer {SECOND_GATEWAY}"},
                json={},
            )
            assert res.status_code == 401

    try:
        asyncio.run(go())
    finally:
        server.should_exit = True
        thread.join(5)
        sock.close()


def test_secondary_recovery_deadline_cancel_and_privacy_use_assigned_runtime(pair, monkeypatch):
    h, second, rt = pair
    a, b = enqueue(h), enqueue(h, "research-hermes")
    claim(h, h.worker, a)
    claim(h, second, b)
    seen = []

    def resolve(conn, rid, settings):
        seen.append(rid)
        return rt if rid == second.runtime_id else h.rt

    monkeypatch.setattr(registry, "adapter", resolve)
    res = h.c.post(f"/v1/agent-runs/{b['id']}/cancel", headers=AUTH)
    assert res.status_code == 200
    assert rt.stopped == ["same-hermes-id"] and h.rt.stopped == []
    assert seen == [second.runtime_id]
    # Recovery of a missing secondary runtime preserves its partial answer and never touches Admin's run.
    with h.pool.connection() as conn:
        lc.append_event(conn, UUID(b["id"]), "message.delta", {"text": "partial"}, "partial")
    assert second.recover() == 1
    assert h.run(b["id"])["state"] == "INTERRUPTED"
    assert h.run(b["id"])["output"] == "partial"
    assert h.run(a["id"])["state"] == "RUNNING"
    c = enqueue(h, "research-hermes")
    claim(h, second, c)
    message = h.c.get(f"/v1/agent-sessions/{c['session_id']}/messages", headers=AUTH).json()[
        "messages"
    ][0]
    res = h.c.post(
        f"/v1/agent-sessions/{c['session_id']}/messages/{message['id']}/forget", headers=AUTH
    )
    assert res.status_code == 200, res.text
    assert seen[-1] == second.runtime_id and len(rt.stopped) == 2 and h.rt.stopped == []


def test_registration_and_profile_binding_fail_closed(pair):
    h, second, _ = pair
    with h.pool.connection() as conn:
        with pytest.raises(registry.RegistryError):
            registry.register(conn, "research", "admin-hermes")
        with pytest.raises(registry.RegistryError):
            registry.register(conn, "human", "owner")
        with pytest.raises(registry.RegistryError):
            registry.credential(conn, h.worker.runtime_id, "gateway", SECOND_GATEWAY)
        with pytest.raises(registry.RegistryError):
            registry.credential(conn, second.runtime_id, "model", SECOND_GATEWAY)
        admin = identity.get_active(conn, "admin-hermes")
        with pytest.raises(psycopg.errors.ForeignKeyViolation), conn.transaction():
            conn.execute(
                "UPDATE agent_runtimes SET desired_profile_version_id=%s WHERE id=%s",
                (admin.version_id, second.runtime_id),
            )


def test_existing_principals_are_rechecked_and_endpoint_changes_require_drain(pair, gateway):
    from paw_server.modules.costs.proxy import ProxyDenied

    h, second, _ = pair
    app, service, provider = gateway
    b = enqueue(h, "research-hermes")
    claim(h, second, b)
    tool_identity = identity_for(h, SECOND_GATEWAY)
    model_identity = identity_for(h, SECOND_MODEL, "model")
    with h.pool.connection() as conn:
        with pytest.raises(registry.RegistryError):
            registry.register(
                conn, "research", "research-hermes", endpoint_url="http://127.0.0.1:12345"
            )
        registry.stop(conn, second.runtime_id)
    assert service.handle_call("docs_search", {"query": "no"}, runtime=tool_identity).is_error
    proxy = make_proxy(h, provider)
    with pytest.raises(ProxyDenied):
        proxy.admit(b["id"], json.dumps(body()).encode(), runtime=model_identity)
    asyncio.run(proxy.http.aclose())
    assert provider.requests == []
    with h.pool.connection() as conn:
        lc.finish(conn, UUID(b["id"]), "INTERRUPTED")
        assert (
            registry.register(
                conn, "research", "research-hermes", endpoint_url="http://127.0.0.1:12345"
            )
            == second.runtime_id
        )
        with pytest.raises(psycopg.errors.CheckViolation), conn.transaction():
            conn.execute(
                "UPDATE runtime_credentials SET revoked_at=NULL WHERE runtime_id=%s",
                (second.runtime_id,),
            )


def test_purge_removes_only_the_deleted_runtimes_reports_when_ids_collide(pair):
    from paw_server.modules.sessions import privacy

    h, second, _ = pair
    a, b = enqueue(h), enqueue(h, "research-hermes")
    claim(h, h.worker, a)
    claim(h, second, b)
    with h.pool.connection() as conn:
        for name, run in [("hermes", a), ("research", b)]:
            for reference in ("same-hermes-id", run["id"]):
                attestation.record_report(
                    conn, name, {"kind": "call", "hermes_run_id": reference, "tool_names": []}
                )
        lc.finish(conn, UUID(a["id"]), "SUCCEEDED", output="admin")
        lc.finish(conn, UUID(b["id"]), "SUCCEEDED", output="research")
    assert (
        h.c.post(
            f"/v1/agent-sessions/{a['session_id']}/delete",
            headers=AUTH,
            json={"include_delegated": False},
        ).status_code
        == 200
    )
    with h.pool.connection() as conn:
        conn.execute(
            "UPDATE agent_sessions SET purge_after=now()-interval '1 day' WHERE id=%s",
            (a["session_id"],),
        )
        conn.commit()
        assert privacy.purge_due(conn) == 1
        rows = conn.execute(
            "SELECT runtime_name,hermes_run_id FROM runtime_reports ORDER BY hermes_run_id"
        ).fetchall()
    assert {(r["runtime_name"], r["hermes_run_id"]) for r in rows} == {
        ("research", "same-hermes-id"),
        ("research", b["id"]),
    }


def test_secondary_api_admission_and_cancel_do_not_depend_on_admin_configuration(pair, monkeypatch):
    h, second, rt = pair
    client = h.env.client()  # no legacy Admin runtime or key configured
    try:
        response = client.post(
            "/v1/agent-sessions", json={"agent_actor_id": "research-hermes"}, headers=AUTH
        )
        assert response.status_code == 201, response.text
        sid = response.json()["id"]
        response = client.post(
            f"/v1/agent-sessions/{sid}/runs",
            json={"client_request_id": "secondary-only", "message": "research"},
            headers=AUTH,
        )
        assert response.status_code == 202, response.text
        b = response.json()
        claim(h, second, b)
        monkeypatch.setattr(registry, "adapter", lambda conn, rid, settings: rt)
        assert client.post(f"/v1/agent-runs/{b['id']}/cancel", headers=AUTH).status_code == 200
        assert rt.stopped == ["same-hermes-id"]
    finally:
        client.__exit__(None, None, None)


def test_stopped_and_disabled_actors_do_not_dispatch_or_authenticate(pair):
    h, second, _ = pair
    pending = enqueue(h, "research-hermes")
    with h.pool.connection() as conn, conn.transaction():
        registry.stop(conn, second.runtime_id)
        # An uncommitted stop holds the registry row: another worker must skip it,
        # rather than dispatch using a stale running-state snapshot.
        with h.pool.connection() as other:
            assert lc.claim_next(other, second.runtime_id) is None
    assert second.run_once() is False
    assert h.run(pending["id"])["state"] == "QUEUED"
    assert identity_for(h, SECOND_GATEWAY) is None
    with h.pool.connection() as conn:
        conn.execute(
            "UPDATE agent_runtimes SET desired_state='running' WHERE id=%s", (second.runtime_id,)
        )
        conn.execute("UPDATE actors SET status='disabled' WHERE id='research-hermes'")
        conn.commit()
    assert second.run_once() is False
    assert identity_for(h, SECOND_MODEL, "model") is None
    assert (
        h.c.post(
            "/v1/agent-sessions", json={"agent_actor_id": "research-hermes"}, headers=AUTH
        ).status_code
        == 404
    )


def test_observed_boot_and_desired_profile_and_deadline_are_per_runtime(pair, gateway):
    h, second, rt = pair
    app, _, _ = gateway
    with h.pool.connection() as conn:
        p = identity.get_active(conn, "research-hermes")
    report = {"kind": "boot", "config_hash": attestation.config_hash(p.compiled.config_fragment)}
    assert request(app, "/internal/report", SECOND_GATEWAY, report).status_code == 200
    with h.pool.connection() as conn:
        row = conn.execute(
            "SELECT status,last_health_at,desired_profile_version_id FROM agent_runtimes WHERE id=%s",
            (second.runtime_id,),
        ).fetchone()
    assert (
        row["status"] == "ready"
        and row["last_health_at"]
        and row["desired_profile_version_id"] == p.version_id
    )
    # A stricter profile is desired but has not been applied to the container yet.
    spec = DEFAULT_ADMIN_SPEC.model_copy(
        update={"tools": DEFAULT_ADMIN_SPEC.tools.model_copy(update={"grants": ["docs_search"]})}
    )
    with h.pool.connection() as conn:
        v = identity.create_version(conn, "research-hermes", spec, "owner")
        identity.activate(conn, "research-hermes", v["id"], "owner")
    strict = AgentWorker(
        h.pool, rt, h.env.settings(agent_runtime_name="research", gateway_attestation="required")
    )
    b = enqueue(h, "research-hermes")
    assert strict.run_once() is False
    with h.pool.connection() as conn:
        p = identity.get_active(conn, "research-hermes")
    report["config_hash"] = attestation.config_hash(p.compiled.config_fragment)
    assert request(app, "/internal/report", SECOND_GATEWAY, report).status_code == 200
    assert strict.run_once() is True
    a, b = enqueue(h), enqueue(h, "research-hermes")
    claim(h, h.worker, a)
    claim(h, second, b)
    with h.pool.connection() as conn:
        conn.execute(
            "UPDATE agent_runs SET deadline_at=now()-interval '1 second' WHERE id=ANY(%s::uuid[])",
            ([a["id"], b["id"]],),
        )
        conn.commit()
    assert second.expire_deadlines() == 1
    assert rt.stopped == ["same-hermes-id"] and h.rt.stopped == []
    assert h.run(a["id"])["state"] == "RUNNING"


def test_successful_secondary_recovery_is_idempotent_and_keeps_its_profile(pair):
    h, second, rt = pair
    b = enqueue(h, "research-hermes")
    with h.pool.connection() as conn:
        lc.claim_next(conn, second.runtime_id)
        p = identity.get_active(conn, "research-hermes")
        lc.bind_profile(
            conn,
            UUID(b["id"]),
            profile_version_id=p.version_id,
            version=p.version,
            compiled_hash=p.compiled.compiled_hash,
            max_run_seconds=600,
        )
        handle = rt.create_run(run_id=b["id"], input="research")
        lc.move(conn, UUID(b["id"]), "RUNNING", hermes_run_id=handle.hermes_run_id)
        lc.append_event(
            conn,
            UUID(b["id"]),
            "message.delta",
            {"text": "he", "raw": "he"},
            handle.hermes_run_id + ":0",
        )
    restarted = AgentWorker(h.pool, rt, h.env.settings(agent_runtime_name="research"))
    assert restarted.recover() == 1 and restarted.recover() == 0
    assert h.run(b["id"])["state"] == "SUCCEEDED"
    assert h.run(b["id"])["output"] == "hello"
    assert len(rt.created) == 1 and not h.rt.created
    with h.pool.connection() as conn:
        rows = conn.execute(
            "SELECT author_actor_id FROM agent_session_messages WHERE run_id=%s AND kind='assistant'",
            (b["id"],),
        ).fetchall()
    assert rows == [{"author_actor_id": "research-hermes"}]


def test_adapter_resolves_registered_endpoint_and_environment_secret(pair, monkeypatch):
    from paw_server.modules.runtime.hermes_adapter import HermesAdapter

    h, second, _ = pair
    requests = []

    def transport(request):
        requests.append(request)
        return httpx.Response(200, json={})

    def make(url, key):
        return HermesAdapter(
            url, key, http=httpx.Client(base_url=url, transport=httpx.MockTransport(transport))
        )

    monkeypatch.setattr(registry, "HermesAdapter", make)
    monkeypatch.setenv("PAW_RESEARCH_KEY", "private-research-key")
    with h.pool.connection() as conn:
        runtime = registry.adapter(conn, second.runtime_id, h.settings)
        runtime.stop_run("container-local-id")
    assert str(requests[0].url) == "http://127.0.0.1:9999/v1/runs/container-local-id/stop"
    assert requests[0].headers["Authorization"] == "Bearer private-research-key"
    monkeypatch.delenv("PAW_RESEARCH_KEY")
    with h.pool.connection() as conn, pytest.raises(registry.RegistryError):
        registry.adapter(conn, second.runtime_id, h.settings)


def test_migration_preserves_legacy_admin_and_bootstrap_is_idempotent(db_url):
    from alembic import command

    from paw_server.core.config import Settings
    from paw_server.db.migrations_info import alembic_config
    from paw_server.db.pool import create_pool

    settings = Settings(
        _env_file=None,
        database_url=db_url,
        owner_token="test-owner-token-0123456789",
        gateway_token=GATEWAY_TOKEN,
        model_proxy_token=PROXY_TOKEN,
        model_proxy_upstream_key="fake-provider-key-not-used",
        hermes_url="http://127.0.0.1:8642",
        hermes_key="private-admin-key",
    )
    cfg = alembic_config(db_url)
    command.upgrade(cfg, "0014")
    with create_pool(settings) as pool, pool.connection() as conn:
        # Populate using the old schema before upgrading. No live database is touched.
        v = identity.create_version(conn, "admin-hermes", DEFAULT_ADMIN_SPEC, "owner")
        conn.execute(
            "UPDATE actor_profile_versions SET status='active',activated_at=now() WHERE id=%s",
            (v["id"],),
        )
        rid = lc.ensure_runtime(conn)
        workspace = conn.execute("SELECT id FROM workspaces WHERE slug='default'").fetchone()["id"]
        conn.commit()
        session = lc.create_session(conn, owner="owner", workspace_id=workspace)
        run, _ = lc.admit(
            conn,
            session_id=session["id"],
            actor_id="owner",
            client_request_id="legacy",
            message="preserve me",
            queue_limit=5,
            model="gpt-4o-mini",
        )
    command.upgrade(cfg, "head")
    command.upgrade(cfg, "head")
    with create_pool(settings) as pool, pool.connection() as conn:
        assert registry.bootstrap(conn, settings) == rid
        assert registry.bootstrap(conn, settings) == rid
        assert registry.authenticate(conn, GATEWAY_TOKEN, "gateway").id == rid
        old = lc.get_run(conn, run["id"], "owner")
        assert old["user_message"] == "preserve me" and old["state"] == "QUEUED"
        assert conn.execute("SELECT count(*) AS n FROM runtime_credentials").fetchone()["n"] == 2
