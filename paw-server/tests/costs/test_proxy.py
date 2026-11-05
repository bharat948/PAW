"""The model proxy against PostgreSQL with a fake provider: denials never reach it; every outcome is accounted.

No paid calls and no provider keys: the provider is an `httpx.MockTransport`.
"""

import asyncio
import json
import logging
from decimal import Decimal
from uuid import UUID, uuid4

import httpx
import pytest
from tests.gateway.test_service import start_run
from tests.test_agent_integration import Harness

from paw_server.modules.costs import service as costs
from paw_server.modules.costs.proxy import ModelProxy
from paw_server.modules.gateway.registry import ToolRegistry
from paw_server.modules.gateway.server import build_app
from paw_server.modules.gateway.service import GatewayService
from paw_server.modules.identity import service as identity
from paw_server.modules.identity.spec import DEFAULT_ADMIN_SPEC, ActorProfileSpec

pytestmark = pytest.mark.integration
UPSTREAM_KEY = "sk-upstream-" + "k" * 30
PROXY_TOKEN = "proxy-token-" + "p" * 20
GATEWAY_TOKEN = "gateway-token-" + "g" * 20
PROMPT = "a-distinctive-private-prompt-0xBEEF"
USAGE = {"prompt_tokens": 40, "completion_tokens": 5, "total_tokens": 45}


class Provider:
    """A scriptable fake provider that records every call that reaches it."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.respond = self.ok

    def ok(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if body.get("stream"):
            frames = [
                {
                    "model": "gpt-4o-mini-2024-07-18",
                    "choices": [{"index": 0, "delta": {"content": "hi"}}],
                },
                {"model": "gpt-4o-mini-2024-07-18", "choices": [], "usage": USAGE},
            ]
            sse = "".join(f"data: {json.dumps(f)}\n\n" for f in frames) + "data: [DONE]\n\n"
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                stream=httpx.ByteStream(sse.encode()),
            )
        return httpx.Response(
            200,
            json={
                "model": "gpt-4o-mini-2024-07-18",
                "choices": [{"message": {"content": "hi"}}],
                "usage": USAGE,
            },
        )

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self.respond(request)


@pytest.fixture
def h(env):
    x = Harness(env)
    yield x
    x.pool.close()


@pytest.fixture
def provider():
    return Provider()


def make_proxy(h, provider, **kw) -> ModelProxy:
    settings = h.env.settings(
        model_proxy_token=PROXY_TOKEN,
        model_proxy_upstream_key=UPSTREAM_KEY,
        gateway_token=GATEWAY_TOKEN,
        **kw,
    )
    http = httpx.AsyncClient(transport=httpx.MockTransport(provider))
    return ModelProxy(h.pool, settings, http)


def body(**over):
    b = {"model": "gpt-4o-mini", "messages": [{"role": "user", "content": PROMPT}]}
    b.update(over)
    return b


def call(proxy, run, payload, *, header=True, raw=None):
    """Drive the ASGI handler directly: returns (status, content_type, body bytes)."""
    data = raw if raw is not None else json.dumps(payload).encode()
    headers = [(b"content-length", str(len(data)).encode())]
    if header:
        headers.append((b"x-paw-run-id", str(run).encode()))
    sent: list[dict] = []

    async def receive():
        return {"type": "http.request", "body": data, "more_body": False}

    async def send(msg):
        sent.append(msg)

    asyncio.run(proxy.handle({"type": "http", "headers": headers}, receive, send))
    start = next(m for m in sent if m["type"] == "http.response.start")
    out = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    ctype = dict(start["headers"]).get(b"content-type", b"").decode()
    return start["status"], ctype, out


def spend(h, run_id):
    with h.pool.connection() as c:
        s = costs.run_cost(c, UUID(str(run_id)))
        c.commit()
    return s


def attempts(h, run_id):
    with h.pool.connection() as c:
        rows = c.execute(
            "SELECT a.input_bound, a.output_bound, a.reserved_usd, r.state FROM actor_cost_attempts a "
            "JOIN actor_cost_results r ON r.attempt_id = a.id WHERE a.run_id = %s",
            (str(run_id),),
        ).fetchall()
        c.commit()
    return rows


# ---- success ------------------------------------------------------------------------------------------------


def test_a_call_is_priced_before_it_is_sent_and_settles_from_provider_usage(h, provider):
    run = start_run(h)["id"]
    status, _, out = call(make_proxy(h, provider), run, body())
    assert status == 200 and json.loads(out)["usage"] == USAGE
    (req,) = provider.requests
    sent = json.loads(req.content)
    assert req.headers["authorization"] == f"Bearer {UPSTREAM_KEY}"
    assert "x-paw-run-id" not in req.headers  # identity stays inside PAW
    assert (
        sent["max_completion_tokens"] == 4096
    )  # a missing output bound is filled in, never left open
    (a,) = attempts(h, run)
    assert (
        a["state"] == "settled" and 0 < a["input_bound"] < 128_000
    )  # tokenised, not the whole window
    s = spend(h, run)
    assert s["status"] == "confirmed" and s["reserved_usd"] == "0"
    assert Decimal(s["confirmed_usd"]) > 0 and s["served_model"] == "gpt-4o-mini-2024-07-18"


def test_a_stream_settles_from_the_final_usage_frame(h, provider):
    run = start_run(h)["id"]
    status, ctype, out = call(make_proxy(h, provider), run, body(stream=True))
    assert status == 200 and ctype == "text/event-stream" and b"[DONE]" in out
    assert json.loads(provider.requests[0].content)["stream_options"] == {"include_usage": True}
    assert spend(h, run)["status"] == "confirmed"


def test_a_stream_that_loses_its_usage_keeps_the_reservation_as_unconfirmed(h, provider):
    run = start_run(h)["id"]

    def no_usage(request):
        sse = 'data: {"model":"gpt-4o-mini","choices":[{"index":0,"delta":{"content":"x"}}]}\n\ndata: [DONE]\n\n'
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            stream=httpx.ByteStream(sse.encode()),
        )

    provider.respond = no_usage
    assert call(make_proxy(h, provider), run, body(stream=True))[0] == 200
    s = spend(h, run)
    assert (
        s["status"] == "unconfirmed"
        and Decimal(s["unconfirmed_usd"]) > 0
        and s["confirmed_usd"] == "0"
    )


def test_a_stream_that_breaks_midway_keeps_the_reservation(h, provider):
    run = start_run(h)["id"]

    class Breaks(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'data: {"model":"gpt-4o-mini","choices":[]}\n\n'
            raise httpx.ReadError("connection reset")

    provider.respond = lambda r: httpx.Response(
        200, headers={"content-type": "text/event-stream"}, stream=Breaks()
    )
    with pytest.raises(httpx.ReadError):
        call(make_proxy(h, provider), run, body(stream=True))
    assert spend(h, run)["status"] == "unconfirmed"


def test_an_upstream_error_is_passed_through_and_cannot_be_proven_free(h, provider):
    run = start_run(h)["id"]
    provider.respond = lambda r: httpx.Response(500, json={"error": {"message": "boom"}})
    status, _, out = call(make_proxy(h, provider), run, body())
    assert status == 500 and b"boom" in out
    assert spend(h, run)["status"] == "unconfirmed"


def test_a_provider_that_cannot_be_reached_keeps_the_reservation_unconfirmed(h, provider):
    run = start_run(h)["id"]

    def refuse(request):
        raise httpx.ConnectError("no route", request=request)

    provider.respond = refuse
    status, _, out = call(make_proxy(h, provider), run, body())
    assert status == 502 and json.loads(out)["error"]["code"] == "upstream_unreachable"
    (a,) = attempts(h, run)
    assert a["state"] == "uncertain" and spend(h, run)["status"] == "unconfirmed"


# ---- denials: zero provider calls ---------------------------------------------------------------------------


def denied(h, provider, run, payload, code, status, *, header=True, raw=None, **kw):
    s, _, out = call(make_proxy(h, provider, **kw), run, payload, header=header, raw=raw)
    assert (s, json.loads(out)["error"]["code"]) == (status, code)
    assert provider.requests == []  # the provider was never contacted


def test_missing_malformed_and_stale_identity_is_denied(h, provider):
    run = start_run(h)["id"]
    denied(h, provider, run, body(), "identity_missing", 403, header=False)
    queued = h.send(h.session())["id"]  # never claimed: no lease, so it cannot spend
    denied(h, provider, queued, body(), "run_not_active", 403)
    denied(h, provider, uuid4(), body(), "run_not_active", 403)


def test_only_the_runs_own_model_is_allowed(h, provider):
    run = start_run(h)["id"]
    denied(h, provider, run, body(model="gpt-4o"), "model_not_allowed", 400)


@pytest.mark.parametrize(
    "extra, code",
    [
        ({"n": 2}, "unsupported_payload"),
        ({"modalities": ["audio"]}, "unsupported_payload"),
        ({"web_search_options": {}}, "unsupported_payload"),
        ({"stream": "yes"}, "unsupported_payload"),
        ({"max_completion_tokens": 0}, "unsupported_output_bound"),
        ({"max_completion_tokens": 10**7}, "unsupported_output_bound"),
        ({"max_tokens": "10"}, "unsupported_output_bound"),
    ],
)
def test_unsupported_payloads_and_output_bounds_are_refused(h, provider, extra, code):
    run = start_run(h)["id"]
    denied(h, provider, run, body(**extra), code, 400)


def test_images_and_non_json_bodies_are_refused(h, provider):
    run = start_run(h)["id"]
    image = [{"type": "image_url", "image_url": {"url": "http://x/y.png"}}]
    denied(
        h,
        provider,
        run,
        body(messages=[{"role": "user", "content": image}]),
        "unsupported_payload",
        400,
    )
    denied(h, provider, run, None, "unsupported_payload", 400, raw=b"not json")
    denied(h, provider, run, None, "unsupported_payload", 400, raw=b"[1, 2]")


def test_an_oversized_request_never_reaches_the_provider(h, provider):
    run = start_run(h)["id"]
    big = "word " * 200_000  # well over the context window in tokens, under the byte limit
    denied(h, provider, run, body(messages=[{"role": "user", "content": big}]), "input_exceeds_context", 400,
           model_proxy_max_body_bytes=8 * 1024 * 1024)  # fmt: skip
    denied(
        h,
        provider,
        run,
        body(),
        "request_too_large",
        413,
        model_proxy_max_body_bytes=1024,
        raw=b"x" * 2000,
    )
    assert attempts(h, run) == []  # refused before any reservation


def activate_budget(h, per_run, daily):
    d = DEFAULT_ADMIN_SPEC.model_dump(mode="json")
    d["budgets"] = {"per_run_usd": per_run, "daily_usd": daily}
    with h.pool.connection() as conn:
        v = identity.create_version(
            conn, "admin-hermes", ActorProfileSpec.model_validate(d), "owner"
        )
        identity.activate(conn, "admin-hermes", v["id"], "owner")


def test_the_shared_cap_denies_before_any_reservation_exists(h, provider):
    run = start_run(h)["id"]
    denied(
        h, provider, run, body(), "budget_exceeded", 403, cost_shared_daily_usd=Decimal("0.000001")
    )
    assert attempts(h, run) == []


@pytest.mark.parametrize("per_run, daily", [(0.005, 3.0), (0.005, 0.005)])
def test_the_profiles_run_and_daily_limits_deny_a_worst_case_that_does_not_fit(
    h, provider, per_run, daily
):
    activate_budget(h, per_run=per_run, daily=daily)
    run = start_run(h)["id"]
    # a 16k-token answer reserves about $0.0098 on its own; with these limits the worst case must not be admitted
    denied(h, provider, run, body(max_completion_tokens=16384), "budget_exceeded", 403)
    assert attempts(h, run) == []
    # the same limits admit a small request: the reservation follows the bound the caller asked for
    assert call(make_proxy(h, provider), run, body(max_completion_tokens=50))[0] == 200


def test_a_held_reservation_blocks_the_next_call_until_the_money_is_accounted_for(h, provider):
    run = start_run(h)["id"]
    small = body(max_completion_tokens=100)
    first = make_proxy(h, provider)
    provider.respond = lambda r: httpx.Response(500, json={"error": {"message": "boom"}})
    assert call(first, run, small)[0] == 500  # sent, outcome unknown: the reservation stays held
    held = attempts(h, run)[0]["reserved_usd"]
    tight = make_proxy(h, provider, cost_shared_daily_usd=held * Decimal("1.5"))
    before = len(provider.requests)
    s, _, out = call(tight, run, small)
    assert (s, json.loads(out)["error"]["code"]) == (403, "budget_exceeded")
    assert len(provider.requests) == before  # denied without a provider call
    provider.respond = provider.ok
    roomy = make_proxy(h, provider)
    assert call(roomy, run, small)[0] == 200  # and a normal cap still admits it


# ---- the listener: credentials are separate -----------------------------------------------------------------


def test_the_model_route_and_the_tool_route_do_not_share_a_credential(h, provider):
    proxy = make_proxy(h, provider)
    service = GatewayService(h.pool, ToolRegistry([]), proxy.settings)
    app = build_app(service, ToolRegistry([]), GATEWAY_TOKEN, proxy, PROXY_TOKEN)

    async def go():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://t"
        ) as c:
            r = {}
            r["no_auth"] = (await c.post("/v1/chat/completions", json=body())).status_code
            r["gateway_token"] = (
                await c.post(
                    "/v1/chat/completions",
                    json=body(),
                    headers={"Authorization": f"Bearer {GATEWAY_TOKEN}"},
                )
            ).status_code
            r["proxy_on_mcp"] = (
                await c.post("/mcp", json={}, headers={"Authorization": f"Bearer {PROXY_TOKEN}"})
            ).status_code
            r["other_v1"] = (
                await c.get("/v1/models", headers={"Authorization": f"Bearer {PROXY_TOKEN}"})
            ).status_code
            r["get"] = (
                await c.get(
                    "/v1/chat/completions", headers={"Authorization": f"Bearer {PROXY_TOKEN}"}
                )
            ).status_code
            r["no_run"] = (
                await c.post(
                    "/v1/chat/completions",
                    json=body(),
                    headers={"Authorization": f"Bearer {PROXY_TOKEN}"},
                )
            ).status_code
            return r

    r = asyncio.run(go())
    assert r == {
        "no_auth": 401,
        "gateway_token": 401,
        "proxy_on_mcp": 401,
        "other_v1": 404,
        "get": 405,
        "no_run": 403,
    }
    assert provider.requests == []


def test_the_app_refuses_a_proxy_token_that_equals_the_gateway_token(h, provider):
    proxy = make_proxy(h, provider)
    service = GatewayService(h.pool, ToolRegistry([]), proxy.settings)
    with pytest.raises(ValueError):
        build_app(service, ToolRegistry([]), GATEWAY_TOKEN, proxy, GATEWAY_TOKEN)


# ---- hygiene ------------------------------------------------------------------------------------------------


def test_no_prompt_or_credential_reaches_the_logs(h, provider, caplog):
    run = start_run(h)["id"]
    with caplog.at_level(logging.DEBUG):
        call(make_proxy(h, provider), run, body())
        call(make_proxy(h, provider), run, body(n=2))
    text = "\n".join(f"{r.getMessage()} {r.__dict__}" for r in caplog.records)
    assert PROMPT not in text and UPSTREAM_KEY not in text and PROXY_TOKEN not in text


def test_a_run_past_its_deadline_cannot_spend_even_before_the_reaper_notices(h, provider):
    run = start_run(h)["id"]
    with h.pool.connection() as conn:
        conn.execute(
            "UPDATE agent_runs SET deadline_at = now() - interval '1 second' WHERE id = %s", (run,)
        )
        conn.commit()
    denied(h, provider, run, body(), "run_not_active", 403)
