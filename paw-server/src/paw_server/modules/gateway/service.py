"""One entry point for every tool call: authenticate, bind to the run, decide, execute, bound, audit.

Every call ends in exactly one `gateway_tool_invocations` row, including denials and crashes inside a handler, and, when a
run is known, a `tool.decision` event in that run's stream (so the chat shows the gateway's view, not the model's).
"""

import logging
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

from paw_server.core.config import Settings
from paw_server.modules.connectors import store as connector_store
from paw_server.modules.costs import service as costs
from paw_server.modules.gateway import attestation as att
from paw_server.modules.gateway.policy import Decision, PolicyInput, RunBinding, decide
from paw_server.modules.gateway.registry import ToolRegistry, enabled_tools, validate_arguments
from paw_server.modules.gateway.shaping import ContinuationStore, shape
from paw_server.modules.gateway.tools import ToolError
from paw_server.modules.runtime import lifecycle as lc
from paw_server.modules.sessions import service as ledger

log = logging.getLogger("paw.gateway")
OWNER = "owner"
CALLABLE_STATES = (
    "DISPATCHING",
    "RUNNING",
)  # Hermes may call a tool before the worker records RUNNING
ERROR_REASONS = ("HANDLER_ERROR", "TOOL_ERROR")
VISIBLE_DENIAL = "This tool call was not allowed ({reason})."


@dataclass(frozen=True)
class ToolContext:
    """Who a call is for. Derived by the gateway from the run's lease, never from tool arguments."""

    run_id: UUID
    session_id: UUID
    actor_id: str  # the agent executing the run
    requester: str  # the human who owns the session
    workspace_id: UUID
    invocation_id: UUID
    trace_id: str


@dataclass(frozen=True)
class ToolOutcome:
    is_error: bool
    text: str
    decision: str  # allowed | denied | error
    reason: str
    invocation_id: UUID


ConnectionStatus = Callable[[str, str], str | None]


def redact_args(args: Any) -> dict[str, Any]:
    """What the audit row keeps of the arguments: short values only; never anything identity-like or huge."""
    if not isinstance(args, dict):
        return {"_invalid": type(args).__name__}
    out: dict[str, Any] = {}
    for k, v in list(args.items())[:20]:
        key = str(k)[:64]
        if isinstance(v, str):
            out[key] = v[:200]
        elif isinstance(v, int | float | bool) or v is None:
            out[key] = v
        else:
            out[key] = f"[{type(v).__name__}]"
    return out


class GatewayService:
    def __init__(
        self,
        pool: ConnectionPool[Any],
        registry: ToolRegistry,
        settings: Settings,
        *,
        connection_status: ConnectionStatus | None = None,
        attestation: Callable[[UUID], str] | None = None,
        continuations: ContinuationStore | None = None,
    ) -> None:
        self.pool, self.registry, self.settings = pool, registry, settings
        self.continuations = continuations or ContinuationStore()
        self._connection_status = connection_status or self._db_connection_status
        self._attestation = (
            attestation  # injected in tests; the default checks the runtime's own reports
        )

    # ---- facts -----------------------------------------------------------------------------------------------
    def _db_connection_status(self, owner: str, provider: str) -> str | None:
        with self.pool.connection() as conn:
            live = connector_store.get_live(conn, owner, provider)
        return live.status if live else None

    def _active_runs(self) -> list[dict[str, Any]]:
        with self.pool.connection() as conn:
            rows = conn.execute(
                "SELECT r.id, r.session_id, r.executing_actor_id, r.profile_version, r.hermes_run_id, s.owner, s.workspace_id, "
                "c.policy, (SELECT count(*) FROM gateway_tool_invocations i WHERE i.run_id = r.id) AS calls "
                "FROM agent_runs r JOIN agent_run_leases l ON l.run_id = r.id "
                "JOIN agent_runtimes rt ON rt.id = l.runtime_id AND rt.name = %s "
                "JOIN agent_sessions s ON s.id = r.session_id "
                "JOIN actor_compiled_profiles c ON c.profile_version_id = r.profile_version_id "
                "WHERE r.state = ANY(%s) AND s.deleted_at IS NULL "
                "AND (r.deadline_at IS NULL OR r.deadline_at > now())",
                (self.settings.gateway_runtime, list(CALLABLE_STATES)),
            ).fetchall()
            conn.commit()
        return [self._with_costs(r) for r in rows]

    def _with_costs(self, run: dict[str, Any]) -> dict[str, Any]:
        """Spend so far from the cost ledger: this run, this actor today and everyone today. Unreadable = unavailable."""
        try:
            with self.pool.connection() as conn:
                today = costs.day_at(datetime.now(UTC))
                ws, actor = run["workspace_id"], run["executing_actor_id"]
                spend = {
                    "cost_run": costs.used(costs.totals(conn, ws, today, run=run["id"])),
                    "cost_today": costs.used(costs.totals(conn, ws, today, actor=actor)),
                    "cost_shared": costs.used(costs.totals(conn, ws, today)),
                }
                conn.commit()
            return {**run, **spend, "cost_unavailable": False}
        except Exception:
            log.exception("gateway could not read spend", extra={"run_id": str(run["id"])})
            return {**run, "cost_unavailable": True}

    # ---- the call ---------------------------------------------------------------------------------------------
    def handle_call(self, tool_name: str, args: Any, *, authenticated: bool = True) -> ToolOutcome:
        started = time.monotonic()
        trace_id, invocation_id = uuid.uuid4().hex, uuid.uuid4()
        spec = self.registry.get(tool_name)
        runs = self._active_runs() if authenticated else []
        bindings = tuple(
            RunBinding(
                run_id=str(r["id"]), actor_id=r["executing_actor_id"], profile_version=r["profile_version"] or 0,
                policy=r["policy"], calls_so_far=r["calls"],
                cost_run_usd=r.get("cost_run", 0), cost_today_usd=r.get("cost_today", 0),
                cost_shared_usd=r.get("cost_shared", 0), cost_unavailable=r.get("cost_unavailable", False),
            )
            for r in runs
        )  # fmt: skip
        run = runs[0] if len(runs) == 1 else None
        problems = tuple(validate_arguments(spec, args)) if spec else ()
        connection = None
        if spec and spec.provider and run is not None:
            connection = self._connection_status(run["owner"], spec.provider)
        with self.pool.connection() as conn:
            enabled = tool_name in enabled_tools(conn)
        if run is None or self.settings.gateway_attestation == "off":
            attestation = "ok"
        elif self._attestation is not None:
            attestation = self._attestation(run["id"])
        else:
            with self.pool.connection() as conn:
                attestation = att.call_attestation(
                    conn, run["hermes_run_id"], run["policy"].get("grants", []), str(run["id"])
                )
        decision = decide(
            PolicyInput(
                authenticated=authenticated, active_runs=bindings, attestation=attestation, tool=spec,
                tool_enabled=enabled, args_problems=problems, connection=connection,
                max_calls_per_run=self.settings.gateway_max_calls_per_run,
                shared_daily_usd=self.settings.cost_shared_daily_usd,
            )
        )  # fmt: skip
        ctx = (
            ToolContext(
                run_id=run["id"], session_id=run["session_id"], actor_id=run["executing_actor_id"],
                requester=run["owner"], workspace_id=run["workspace_id"], invocation_id=invocation_id,
                trace_id=trace_id,
            )
            if run is not None
            else None
        )  # fmt: skip

        if run is not None:
            try:
                self._begin(invocation_id, run, tool_name, args)
            except ToolError:
                # Admission raced with completion or deletion; never execute the stale binding.
                self._audit(
                    invocation_id=invocation_id, run=None, tool=tool_name, spec=spec, args={},
                    decision="denied", reason="RUN_INACTIVE", latency_ms=0, result_bytes=0,
                    truncated=False, error=None, trace_id=trace_id,
                )  # fmt: skip
                return ToolOutcome(
                    True,
                    "The conversation is no longer active.",
                    "denied",
                    "RUN_INACTIVE",
                    invocation_id,
                )
        result_text, truncated, error = "", False, None
        full_result: dict[str, Any] = {}
        final = decision
        if decision.allowed:
            assert spec is not None and ctx is not None
            try:
                if spec.handler is None:
                    raise NotImplementedError("tool has no handler")
                full_result = ledger.safe_data(spec.handler(ctx, args))
                shaped = shape(tool_name, full_result, run_id=ctx.run_id, store=self.continuations)
                result_text, truncated = shaped.text, shaped.truncated
            except ToolError as exc:  # a failure the tool chose to explain to the model
                final = Decision(False, "TOOL_ERROR")
                error = str(ledger.safe_data(str(exc)))[:200]
            except (
                Exception
            ) as exc:  # a broken tool must never crash the gateway or leak internals to the model
                log.exception(
                    "tool handler failed", extra={"tool": tool_name, "trace_id": trace_id}
                )
                final = Decision(False, "HANDLER_ERROR")
                error = type(exc).__name__
        latency_ms = int((time.monotonic() - started) * 1000)
        outcome_kind = (
            "allowed" if final.allowed else ("error" if final.reason in ERROR_REASONS else "denied")
        )
        delivered = self._audit(
            invocation_id=invocation_id, run=run, tool=tool_name, spec=spec, args=args, decision=outcome_kind,
            reason=final.reason, latency_ms=latency_ms, result_bytes=len(result_text.encode()), truncated=truncated,
            error=error, trace_id=trace_id, result=full_result,
            result_text=result_text if final.allowed else (f"The tool reported an error: {error}" if final.reason == "TOOL_ERROR" else VISIBLE_DENIAL.format(reason=final.reason)),
        )  # fmt: skip
        if not delivered:
            return ToolOutcome(
                True,
                "Result discarded because the conversation changed.",
                "denied",
                "CONTEXT_CHANGED",
                invocation_id,
            )
        if final.allowed:
            return ToolOutcome(False, result_text, "allowed", "OK", invocation_id)
        text = (
            f"The tool reported an error: {error}"
            if final.reason == "TOOL_ERROR"
            else VISIBLE_DENIAL.format(reason=final.reason)
        )
        return ToolOutcome(True, text, outcome_kind, final.reason, invocation_id)

    def receive_report(self, body: Any) -> str:
        """A report from the runtime's plugin. A blocked native tool call also becomes an audit row and a
        `tool.decision` event, so the chat shows it (Hermes itself emits no event for a blocked call)."""
        with self.pool.connection() as conn:
            kind = att.record_report(conn, self.settings.gateway_runtime, body)
            run = None
            if kind == "blocked":
                run = conn.execute(
                    "SELECT id, session_id, executing_actor_id FROM agent_runs WHERE hermes_run_id = %s OR id::text = %s",
                    (body.get("hermes_run_id"), body.get("hermes_run_id")),
                ).fetchone()
                conn.commit()
        if kind == "blocked":
            invocation_id = uuid.uuid4()
            if run is not None:
                try:
                    self._begin(invocation_id, run, str(body.get("tool_name")), {})
                except ToolError:
                    run = None
            self._audit(
                invocation_id=invocation_id, run=run, tool=str(body.get("tool_name")), spec=None, args={},
                decision="denied", reason="NATIVE_TOOL_BLOCKED", latency_ms=0, result_bytes=0, truncated=False,
                error=None, trace_id=uuid.uuid4().hex,
            )  # fmt: skip
        return kind

    def _begin(self, invocation_id: UUID, run: dict[str, Any], tool: str, args: Any) -> None:
        with self.pool.connection() as conn, conn.transaction():
            session = ledger.lock(conn, run["session_id"])
            current = conn.execute(
                "SELECT state FROM agent_runs WHERE id = %s FOR UPDATE", (run["id"],)
            ).fetchone()
            if (
                session["deleted_at"] is not None
                or current is None
                or current["state"] in lc.TERMINAL
            ):
                raise ToolError("The conversation is no longer active.")
            conn.execute(
                "INSERT INTO gateway_tool_attempts (id, run_id, context_revision) VALUES (%s, %s, %s)",
                (invocation_id, run["id"], session["context_revision"]),
            )
            ledger.append(
                conn,
                run["session_id"],
                "tool_call",
                run["executing_actor_id"],
                tool,
                run_id=run["id"],
                invocation_id=invocation_id,
                data={"tool": tool, "arguments": ledger.safe_data(redact_args(args))},
            )

    def _audit(
        self, *, invocation_id: UUID, run: dict[str, Any] | None, tool: str, spec: Any, args: Any, decision: str,
        reason: str, latency_ms: int, result_bytes: int, truncated: bool, error: str | None, trace_id: str,
        result: dict[str, Any] | None = None, result_text: str = "",
    ) -> bool:  # fmt: skip
        run_id = run["id"] if run else None
        with self.pool.connection() as conn, conn.transaction():
            redacted = False
            if run is not None:
                session = ledger.lock(conn, run["session_id"])
                attempt = conn.execute(
                    "SELECT context_revision FROM gateway_tool_attempts WHERE id = %s",
                    (invocation_id,),
                ).fetchone()
                redacted = bool(
                    session["deleted_at"] is not None
                    or attempt is None
                    or attempt["context_revision"] != session["context_revision"]
                )
                if redacted:
                    args, result, result_text, error = (
                        {},
                        {},
                        "[result discarded after conversation changed]",
                        None,
                    )
            conn.execute(
                "INSERT INTO gateway_tool_invocations (id, run_id, actor_id, tool, tool_version, args_redacted, decision, "
                "reason_code, latency_ms, result_bytes, truncated, error, trace_id) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (
                    invocation_id, run_id, run["executing_actor_id"] if run else None, tool[:100],
                    spec.version if spec else None, Jsonb(ledger.safe_data(redact_args(args))), decision, reason, latency_ms,
                    result_bytes, truncated, error, trace_id,
                ),
            )  # fmt: skip
            if run_id is not None and run is not None:
                ledger.append(
                    conn,
                    run["session_id"],
                    "tool_result",
                    run["executing_actor_id"],
                    result_text or VISIBLE_DENIAL.format(reason=reason),
                    data={
                        "tool": tool,
                        "decision": decision,
                        "reason": reason,
                        "result": result or {},
                        "redacted": redacted,
                    },
                    run_id=run_id,
                    invocation_id=invocation_id,
                )
                current = conn.execute(
                    "SELECT state FROM agent_runs WHERE id = %s FOR UPDATE", (run_id,)
                ).fetchone()
                if current is not None and current["state"] not in lc.TERMINAL:
                    lc._insert_event(
                    conn, run_id, "tool.decision",
                    {"tool": tool[:100], "decision": decision, "reason": reason, "invocation_id": str(invocation_id),
                     "latency_ms": latency_ms, "truncated": truncated},
                    None,
                )  # fmt: skip

        return not redacted
