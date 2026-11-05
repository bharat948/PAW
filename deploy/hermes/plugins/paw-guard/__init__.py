"""PAW guard and reporter for Hermes (spec D205, 002D).

1. `pre_tool_call` blocks any tool that is not a PAW gateway tool (`mcp__paw__*`), and tells the gateway, because Hermes
   itself emits no event for a blocked call. PAW_GUARD_MODE: block (default) | raise (S0 test: simulate a crashing hook)
   | off (S0 baseline: allow all). Hermes fails CLOSED when a hook raises or times out (S0 finding).
2. `pre_api_request` reports, on every model call, the exact tool names the call carries and the model. The gateway
   compares them with the active profile and denies tool calls from a run that does not match.
3. An `llm_request` middleware stamps `X-PAW-Run-ID` (the session id, which PAW sets to its own run id) on every model
   request, so the PAW model proxy can attribute and budget the call. Verified on the pinned Hermes (A1-01).
4. At load it reports the hash of the compiled config the container was started with (PAW_CONFIG_HASH).

Reports are fire-and-forget on a thread with a short timeout and never raise: Hermes swallows errors in observer hooks,
so a silent failure must look like "no report" to the gateway, which then fails closed. Stdlib only.
"""

import json
import logging
import os
import threading
import urllib.request

log = logging.getLogger("paw.guard")

ALLOWED_PREFIX = "mcp__paw__"
TIMEOUT_S = 3


def _report_url() -> str | None:
    base = os.environ.get("PAW_MCP_URL", "")
    if not base:
        return None
    return base.rstrip("/").removesuffix("/mcp") + "/internal/report"


def _send(body: dict) -> None:
    url, token = _report_url(), os.environ.get("PAW_MCP_TOKEN", "")
    if not url or not token:
        return
    try:
        req = urllib.request.Request(
            url,
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
            method="POST",
        )
        urllib.request.urlopen(req, timeout=TIMEOUT_S).read()  # noqa: S310 (fixed internal URL from the environment)
    except Exception as exc:  # never raise into Hermes
        log.warning("paw report failed: %s", type(exc).__name__)


def _report(body: dict) -> None:
    threading.Thread(target=_send, args=(body,), daemon=True).start()


def _guard(tool_name: str = "", args: dict | None = None, **kwargs):
    mode = os.environ.get("PAW_GUARD_MODE", "block")
    log.debug("paw-guard saw tool_name=%r mode=%s", tool_name, mode)
    if mode == "off":
        return None
    if mode == "raise":
        raise RuntimeError("paw-guard simulated failure")
    if not str(tool_name).startswith(ALLOWED_PREFIX):
        _report({"kind": "blocked", "hermes_run_id": str(kwargs.get("session_id") or kwargs.get("task_id") or ""),
                 "tool_name": str(tool_name)[:100]})  # fmt: skip
        return {"action": "block", "message": f"PAW policy: tool '{tool_name}' is not permitted"}
    return None


def _attest(**kw):
    """Observer hook: report the tool names this model call carries."""
    body = ((kw.get("request") or {}).get("body")) or {}
    names = []
    for t in body.get("tools") or []:
        if isinstance(t, dict):
            n = (t.get("function") or {}).get("name") or t.get("name")
            if n:
                names.append(str(n)[:100])
    run_id = str(kw.get("session_id") or kw.get("task_id") or "")
    if run_id:
        _report({"kind": "call", "hermes_run_id": run_id, "tool_names": names[:64],
                 "model": str(kw.get("model") or "")[:100], "provider": str(kw.get("provider") or "")[:100]})  # fmt: skip
    return None


def _stamp(request, session_id="", **kw):
    request = dict(request)
    request["extra_headers"] = {**(request.get("extra_headers") or {}), "X-PAW-Run-ID": str(session_id or "")}
    return {"request": request}


def register(ctx):
    ctx.register_middleware("llm_request", _stamp)
    ctx.register_hook("pre_tool_call", _guard)
    ctx.register_hook("pre_api_request", _attest)
    h = os.environ.get("PAW_CONFIG_HASH", "")
    if h:
        _report({"kind": "boot", "config_hash": h[:128]})
