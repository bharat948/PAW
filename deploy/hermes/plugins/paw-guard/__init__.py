"""PAW guard: pre_tool_call hook that blocks anything outside the PAW MCP server (spec D205, defense in depth).

Real tool names are `mcp__paw__<tool>` (double underscores; the spec's `mcp_paw_` is wrong for this Hermes build).
PAW_GUARD_MODE: block (default) | raise (S0 test: simulate a crashing hook) | off (S0 baseline: allow all).
S0 findings (Hermes c7da41d): a raising or timed-out hook FAILS CLOSED (tool blocked, logged), and a blocked call emits
no tool.* event, so 002D must make this hook report blocked attempts to the PAW gateway for the audit trail.
"""

import logging
import os

log = logging.getLogger("paw.guard")

ALLOWED_PREFIX = "mcp__paw__"


def _guard(tool_name: str = "", args: dict | None = None, **kwargs):
    mode = os.environ.get("PAW_GUARD_MODE", "block")
    log.debug("paw-guard saw tool_name=%r mode=%s", tool_name, mode)
    if mode == "off":
        return None
    if mode == "raise":
        raise RuntimeError("paw-guard simulated failure")
    if not str(tool_name).startswith(ALLOWED_PREFIX):
        return {"action": "block", "message": f"PAW policy: tool '{tool_name}' is not permitted"}
    return None


def register(ctx):
    ctx.register_hook("pre_tool_call", _guard)
