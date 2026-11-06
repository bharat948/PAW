"""The MCP endpoint Hermes talks to (streamable HTTP at /mcp). It only translates MCP into `GatewayService.handle_call`;
all policy lives there. Requests without the runtime's bearer token get 401 before MCP is even parsed (002D-R01)."""

import json
from collections.abc import Callable
from functools import partial
from typing import Any

import anyio
import mcp.types as t
from mcp.server import Server, ServerRequestContext
from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser
from mcp.server.auth.provider import AccessToken

from paw_server.modules.costs.proxy import ModelProxy
from paw_server.modules.gateway.attestation import ReportError
from paw_server.modules.gateway.registry import ToolRegistry
from paw_server.modules.gateway.service import GatewayService
from paw_server.modules.runtime import registry as runtimes


def build_mcp_server(service: GatewayService, registry: ToolRegistry) -> Server[Any]:
    async def list_tools(
        ctx: ServerRequestContext[Any], params: t.PaginatedRequestParams | None
    ) -> t.ListToolsResult:
        # Hermes's own `tools.include` narrows this further; the policy engine is what actually authorises each call.
        tools = []
        for name in registry.names():
            s = registry.get(name)
            assert s is not None
            tools.append(
                t.Tool(
                    name=s.name,
                    description=s.description,
                    inputSchema=s.input_schema,
                    annotations=t.ToolAnnotations(
                        readOnlyHint=s.side_effect_class == "read",
                        destructiveHint=False,
                        openWorldHint=False,
                    ),
                )
            )
        return t.ListToolsResult(tools=tools)

    async def call_tool(
        ctx: ServerRequestContext[Any], params: t.CallToolRequestParams
    ) -> t.CallToolResult:
        outcome = await anyio.to_thread.run_sync(
            partial(
                service.handle_call,
                params.name,
                params.arguments or {},
                runtime=ctx.request.scope.get("paw_runtime") if ctx.request else None,
                authenticated=ctx.request is None or "paw_runtime" in ctx.request.scope,
            )
        )
        return t.CallToolResult(
            content=[t.TextContent(type="text", text=outcome.text)], isError=outcome.is_error
        )

    return Server("paw-gateway", version="1", on_list_tools=list_tools, on_call_tool=call_tool)


async def _respond(send: Any, status: int, body: object) -> None:
    data = json.dumps(body).encode()
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(data)).encode()),
            ],
        }
    )
    await send({"type": "http.response.body", "body": data})


def build_app(
    service: GatewayService,
    registry: ToolRegistry,
    token: str,
    proxy: ModelProxy | None = None,
    proxy_token: str = "",
    enforcement: Callable[[], list[str]] | None = None,
) -> Any:
    """ASGI app: bearer check in front of the MCP streamable-HTTP app. `host="0.0.0.0"` only switches off the SDK's
    localhost Host-header allow-list (Hermes reaches us as host.docker.internal); the bearer token and the listener's
    network placement are the access control."""
    if len(token) < 24:
        raise ValueError("PAW_GATEWAY_TOKEN must be at least 24 characters")
    if proxy is not None and (len(proxy_token) < 24 or proxy_token == token):
        raise ValueError(
            "the model proxy token must be at least 24 characters and differ from the gateway token"
        )
    with service.pool.connection() as conn:
        # Import the configured Admin tokens once; a revoked hash stays revoked on restart.
        rid = runtimes.register(conn, service.settings.gateway_runtime, "admin-hermes")
        runtimes.credential(conn, rid, "gateway", token)
        if proxy is not None:
            runtimes.credential(conn, rid, "model", proxy_token)
    inner = build_mcp_server(service, registry).streamable_http_app(host="0.0.0.0")

    def authenticate(given: bytes, purpose: str) -> runtimes.RuntimeIdentity | None:
        if not given.startswith(b"Bearer "):
            return None
        try:
            value = given[7:].decode("ascii")
        except UnicodeDecodeError:
            return None
        with service.pool.connection() as conn:
            return runtimes.authenticate(conn, value, purpose)

    async def read_body(receive: Any, limit: int = 16 * 1024) -> bytes | None:
        body: bytes = b""
        while True:
            msg = await receive()
            body += msg.get("body", b"")
            if len(body) > limit:
                return None
            if not msg.get("more_body"):
                return bytes(body)

    async def report(scope: Any, receive: Any, send: Any) -> None:
        raw = await read_body(receive)
        try:
            if raw is None:
                raise ReportError("too large")
            kind = await anyio.to_thread.run_sync(
                partial(service.receive_report, json.loads(raw), runtime=scope["paw_runtime"])
            )
        except (ReportError, ValueError):
            return await _respond(send, 400, {"error": "bad report"})
        await _respond(send, 200, {"ok": True, "kind": kind})

    async def app(scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] == "http":
            if scope["path"] == "/_health":
                return await _respond(send, 200, {"ok": True})
            if scope["path"] == "/_ready":
                # Unauthenticated like /_health, so it names failing checks only. No enforcement hook = not enforcing.
                problems = (
                    ["model_proxy_not_configured"]
                    if enforcement is None
                    else await anyio.to_thread.run_sync(enforcement)
                )
                return await _respond(
                    send, 200 if not problems else 503, {"ok": not problems, "problems": problems}
                )
            given = dict(scope["headers"]).get(b"authorization", b"")
            if scope["path"].startswith("/v1/"):
                # The model route has its own credential: the gateway token cannot spend, and this one cannot call tools.
                if proxy is None or scope["path"] != "/v1/chat/completions":
                    return await _respond(send, 404, {"error": "not found"})
                principal = await anyio.to_thread.run_sync(authenticate, given, "model")
                if principal is None:
                    return await _respond(send, 401, {"error": "unauthorized"})
                if scope["method"] != "POST":
                    return await _respond(send, 405, {"error": "method not allowed"})
                return await proxy.handle({**scope, "paw_runtime": principal}, receive, send)
            principal = await anyio.to_thread.run_sync(authenticate, given, "gateway")
            if principal is None:
                return await _respond(send, 401, {"error": "unauthorized"})
            scope = {
                **scope,
                "paw_runtime": principal,
                # Let the MCP transport enforce session ownership for POST, SSE GET and DELETE alike.
                # It needs only stable identity, never the plaintext bearer value.
                "user": AuthenticatedUser(
                    AccessToken(
                        token="",
                        client_id=str(principal.credential_id),
                        subject=str(principal.id),
                        scopes=[],
                    )
                ),
            }
            if scope["path"] == "/internal/report" and scope["method"] == "POST":
                return await report(scope, receive, send)
        await inner(scope, receive, send)

    return app
