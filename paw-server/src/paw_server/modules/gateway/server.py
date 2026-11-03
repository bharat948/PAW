"""The MCP endpoint Hermes talks to (streamable HTTP at /mcp). It only translates MCP into `GatewayService.handle_call`;
all policy lives there. Requests without the runtime's bearer token get 401 before MCP is even parsed (002D-R01)."""

import hmac
import json
from typing import Any

import anyio
import mcp.types as t
from mcp.server import Server, ServerRequestContext

from paw_server.modules.gateway.attestation import ReportError
from paw_server.modules.gateway.registry import ToolRegistry
from paw_server.modules.gateway.service import GatewayService


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
            service.handle_call, params.name, params.arguments or {}
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


def build_app(service: GatewayService, registry: ToolRegistry, token: str) -> Any:
    """ASGI app: bearer check in front of the MCP streamable-HTTP app. `host="0.0.0.0"` only switches off the SDK's
    localhost Host-header allow-list (Hermes reaches us as host.docker.internal); the bearer token and the listener's
    network placement are the access control."""
    if len(token) < 24:
        raise ValueError("PAW_GATEWAY_TOKEN must be at least 24 characters")
    expected = f"Bearer {token}".encode()
    inner = build_mcp_server(service, registry).streamable_http_app(host="0.0.0.0")

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
            kind = await anyio.to_thread.run_sync(service.receive_report, json.loads(raw))
        except (ReportError, ValueError):
            return await _respond(send, 400, {"error": "bad report"})
        await _respond(send, 200, {"ok": True, "kind": kind})

    async def app(scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] == "http":
            if scope["path"] == "/_health":
                return await _respond(send, 200, {"ok": True})
            given = dict(scope["headers"]).get(b"authorization", b"")
            if not hmac.compare_digest(given, expected):
                return await _respond(send, 401, {"error": "unauthorized"})
            if scope["path"] == "/internal/report" and scope["method"] == "POST":
                return await report(scope, receive, send)
        await inner(scope, receive, send)

    return app
