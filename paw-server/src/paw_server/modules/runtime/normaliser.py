"""Hermès wire events -> PAW events. Pure functions; built from the S0 captures (docs/paw-002/captures).

Unknown events are never dropped silently: they become `unknown` events so the inspector shows them.
"""

import json
from typing import Any

from paw_server.modules.runtime.protocol import RuntimeEvent, Usage

MCP_PREFIX = "mcp__paw__"
RAW_LIMIT = 2000


def tool_name(wire: str) -> tuple[str, bool]:
    """(name, native). PAW gateway tools arrive as `mcp__paw__<tool>`; anything else is a Hermès-native tool,
    which the lockdown should make impossible, so it is flagged for the inspector."""
    if wire.startswith(MCP_PREFIX):
        return wire[len(MCP_PREFIX) :], False
    return wire, True


def usage_from(raw: Any) -> Usage | None:
    if not isinstance(raw, dict):
        return None
    try:
        return Usage(
            input_tokens=int(raw["input_tokens"]),
            output_tokens=int(raw["output_tokens"]),
            cache_read=_opt(raw.get("cache_read_tokens")),
            cache_write=_opt(raw.get("cache_write_tokens")),
        )
    except (KeyError, TypeError, ValueError):
        return None


def _opt(v: Any) -> int | None:
    return int(v) if isinstance(v, int | float) and not isinstance(v, bool) else None


def usage_dict(u: Usage | None) -> dict[str, Any] | None:
    return None if u is None else {
        "input_tokens": u.input_tokens, "output_tokens": u.output_tokens,
        "cache_read": u.cache_read, "cache_write": u.cache_write,
    }  # fmt: skip


def normalise(frame: dict[str, Any], run_id: str) -> RuntimeEvent | None:
    """None for frames with no seq (cannot be deduplicated, so they are not evidence of anything)."""
    seq = frame.get("seq")
    if not isinstance(seq, int) or isinstance(seq, bool):
        return None
    sid = f"{run_id}:{seq}"
    kind = str(frame.get("event", ""))

    def text(k: str) -> str:
        return str(frame.get(k) or "")

    if kind == "message.delta":
        return RuntimeEvent("message.delta", {"text": text("delta")}, sid)
    if kind in ("tool.started", "tool.completed"):
        name, native = tool_name(text("tool"))
        payload: dict[str, Any] = {"tool": name, "call_id": text("tool_call_id"), "native": native}
        if kind == "tool.started":
            payload["preview"] = text("preview")[:500]
        else:
            payload["duration_s"] = frame.get("duration") or 0
            payload["error"] = bool(frame.get("error"))
        return RuntimeEvent(kind, payload, sid)
    if kind == "approval.request":
        return RuntimeEvent(
            "approval.requested",
            {"prompt_id": text("prompt_id"), "description": text("description")[:500]},
            sid,
        )
    if kind == "run.completed":
        out = {"output": text("output"), "usage": usage_dict(usage_from(frame.get("usage")))}
        if (
            frame.get("completed") is True
            and not frame.get("partial")
            and not frame.get("interrupted")
        ):
            return RuntimeEvent("run.completed", out, sid)
        # Hermès says the turn did not finish cleanly: never report that as success
        return RuntimeEvent("run.interrupted", {**out, "reason": "incomplete_turn"}, sid)
    if kind == "run.failed":
        return RuntimeEvent("run.failed", {"error": text("error")[:500] or "runtime_error"}, sid)
    if kind == "run.cancelled":
        return RuntimeEvent("run.cancelled", {"reason": text("turn_exit_reason")}, sid)
    if kind == "run.interrupted":
        return RuntimeEvent(
            "run.interrupted", {"output": text("output"), "reason": "runtime_interrupted"}, sid
        )
    return RuntimeEvent(
        "unknown", {"source_type": kind, "raw": json.dumps(frame, default=str)[:RAW_LIMIT]}, sid
    )


STATUS_TERMINAL = {
    "completed": "run.completed",
    "failed": "run.failed",
    "cancelled": "run.cancelled",
    "interrupted": "run.interrupted",
}
