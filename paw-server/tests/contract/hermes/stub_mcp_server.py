"""Stub PAW MCP server for spike S0 (Q6, Q8, Q9). NOT the real gateway (that is 002D).

Runs inside the Hermes image so server and client share the same MCP SDK (2.x). Streamable HTTP at /mcp, static
bearer token (STUB_TOKEN). Records every call at GET /_calls so tests can prove what was (not) executed.
Tools: paw_echo (readOnlyHint), paw_write_note (write-capable), paw_slow (readOnlyHint, sleeps).
"""

import asyncio
import json
import os
import time

import uvicorn
from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

TOKEN = os.environ["STUB_TOKEN"]
CALLS: list[dict] = []
mcp = MCPServer("paw-stub")


def _log(tool: str, **kw: object) -> None:
    CALLS.append({"tool": tool, "at": time.time(), **kw})


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
def paw_echo(text: str) -> str:
    """Echo the text back. Read-only."""
    _log("paw_echo", text=text)
    return f"echo:{text}"


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False))
def paw_write_note(text: str) -> str:
    """Store a note. Write-capable (not read-only)."""
    _log("paw_write_note", text=text)
    return "noted"


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
async def paw_slow(seconds: int) -> str:
    """Wait the given number of seconds, then return."""
    _log("paw_slow", seconds=seconds, phase="start")
    await asyncio.sleep(seconds)
    _log("paw_slow", seconds=seconds, phase="end")
    return "slow done"


inner = mcp.streamable_http_app(host="0.0.0.0")


async def _respond(send, status: int, body: object) -> None:
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


async def app(scope, receive, send):
    if scope["type"] == "http":
        if scope["path"] == "/_calls":
            return await _respond(send, 200, CALLS)
        if scope["path"] == "/_health":
            return await _respond(send, 200, {"ok": True})
        auth = dict(scope["headers"]).get(b"authorization", b"")
        if auth != f"Bearer {TOKEN}".encode():
            _log("_rejected", reason="bad or missing bearer")
            return await _respond(send, 401, {"error": "unauthorized"})
    await inner(scope, receive, send)


if __name__ == "__main__":
    uvicorn.run(
        app, host="0.0.0.0", port=int(os.environ.get("STUB_PORT", "8001")), log_level="warning"
    )
