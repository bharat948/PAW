"""What the runtime says about itself, and whether it matches what the active profile allows.

The Hermes plugin reports (a) once at boot: a hash of the compiled config the container applied; (b) on every model
call: the exact tool names the call carried; (c) every native tool it blocked. Reports are evidence, not authority:
the gateway still authorises each call itself. A tool call whose run has no matching report is denied (fail closed),
because Hermes swallows errors in these observer hooks and a broken plugin must never look like a healthy one.
"""

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from paw_server.modules.ingestion.jobs import Conn

MCP_PREFIX = "mcp__paw__"
MAX_NAMES, MAX_LEN = 64, 100


class ReportError(ValueError):
    """The report is malformed. Nothing is stored."""


def config_hash(fragment: dict[str, Any]) -> str:
    """Hash of the compiled config fragment. The container is started with it and reports it back at boot."""
    return hashlib.sha256(
        json.dumps(fragment, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def expected_tool_names(grants: list[str]) -> list[str]:
    return sorted(f"{MCP_PREFIX}{g}" for g in grants)


def _text(v: Any, limit: int = MAX_LEN) -> str | None:
    if v is None:
        return None
    if not isinstance(v, str) or len(v) > limit:
        raise ReportError("bad text field")
    return v


def record_report(conn: Conn, runtime: str, body: Any) -> str:
    """Validate and store one report; returns its kind."""
    if not isinstance(body, dict):
        raise ReportError("report must be an object")
    kind = body.get("kind")
    if kind not in ("boot", "call", "blocked"):
        raise ReportError("unknown kind")
    names = body.get("tool_names")
    if names is not None:
        if (
            not isinstance(names, list)
            or len(names) > MAX_NAMES
            or not all(isinstance(n, str) and 0 < len(n) <= MAX_LEN for n in names)
        ):
            raise ReportError("bad tool_names")
    fields = (
        _text(body.get("hermes_run_id")), _text(body.get("config_hash"), 128), _text(body.get("model")),
        _text(body.get("provider")), sorted(set(names)) if names is not None else None, _text(body.get("tool_name")),
    )  # fmt: skip
    try:
        conn.execute(
            "INSERT INTO runtime_reports (runtime_name, kind, hermes_run_id, config_hash, model, provider, tool_names, "
            "tool_name) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
            (runtime, kind, *fields),
        )
    except Exception as exc:  # CHECK violation: a field the kind requires is missing
        conn.rollback()
        raise ReportError("missing field for this kind") from exc
    conn.execute("DELETE FROM runtime_reports WHERE observed_at < now() - interval '7 days'")
    conn.commit()
    return str(kind)


def call_attestation(
    conn: Conn, hermes_run_id: str | None, grants: list[str], paw_run_id: str | None = None
) -> str:
    """ok | missing | mismatch for a run, from its latest model-call report. The runtime reports under the
    session id it was given, which is the PAW run id; the Hermes run id is still accepted."""
    keys = [k for k in (hermes_run_id, paw_run_id) if k]
    if not keys:
        return "missing"
    row = conn.execute(
        "SELECT tool_names FROM runtime_reports WHERE kind = 'call' AND hermes_run_id = ANY(%s) "
        "ORDER BY observed_at DESC LIMIT 1",
        (keys,),
    ).fetchone()
    conn.commit()
    if row is None:
        return "missing"
    return "ok" if sorted(row["tool_names"]) == expected_tool_names(grants) else "mismatch"


@dataclass(frozen=True)
class RuntimeStatus:
    state: str  # matches | mismatch | unknown (no boot report yet)
    reported_hash: str | None
    expected_hash: str | None


def runtime_status(conn: Conn, runtime: str, active_config: dict[str, Any] | None) -> RuntimeStatus:
    """Does the container's applied config match the active profile? Drives "restart required" and /ready."""
    row = conn.execute(
        "SELECT config_hash FROM runtime_reports WHERE runtime_name = %s AND kind = 'boot' "
        "ORDER BY observed_at DESC LIMIT 1",
        (runtime,),
    ).fetchone()
    conn.commit()
    expected = config_hash(active_config) if active_config is not None else None
    if row is None or expected is None:
        return RuntimeStatus("unknown", None, expected)
    return RuntimeStatus(
        "matches" if row["config_hash"] == expected else "mismatch", row["config_hash"], expected
    )
