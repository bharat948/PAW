"""The gateway's decision, as one pure function (002D-R03). Default deny: the first failing step wins and its reason
code is recorded. Nothing here reads the database or the network; the caller gathers the facts, so every step can
be tested with a table and the order cannot silently change."""

from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any

from paw_server.modules.gateway.registry import ToolSpec

# every reason code the gateway can record, in evaluation order
REASONS = (
    "UNAUTHENTICATED", "NO_ACTIVE_RUN", "ATTESTATION_MISSING", "ATTESTATION_MISMATCH", "UNKNOWN_TOOL",
    "TOOL_DISABLED", "NOT_GRANTED", "INVALID_ARGUMENTS", "NOT_CONNECTED", "CONNECTION_REVOKED", "OUT_OF_SCOPE",
    "RATE_LIMITED", "BUDGET_EXCEEDED", "APPROVAL_REQUIRED", "OK",
)  # fmt: skip


@dataclass(frozen=True)
class RunBinding:
    """The one RUNNING run a call belongs to, with the policy of the profile version it was bound to at claim time."""

    run_id: str
    actor_id: str
    profile_version: int
    policy: dict[str, Any]  # compiled policy: grants, resource_scopes, budgets
    calls_so_far: int = 0
    # Confirmed plus reserved (including unconfirmed) spend from the cost ledger; never read from the runtime.
    cost_run_usd: Decimal | float = Decimal(0)
    cost_today_usd: Decimal | float = Decimal(0)  # this actor, Asia/Kolkata day
    cost_shared_usd: Decimal | float = Decimal(0)  # every actor in the workspace, same day
    cost_unavailable: bool = False  # the ledger could not be read: fail closed


@dataclass(frozen=True)
class PolicyInput:
    authenticated: bool
    active_runs: tuple[RunBinding, ...]  # RUNNING runs on this runtime: exactly one is required
    attestation: str  # ok | missing | mismatch
    tool: ToolSpec | None  # None = no such tool
    tool_enabled: bool = True
    args_problems: tuple[str, ...] = ()
    connection: str | None = (
        None  # status of the provider connection: active | needs_reauth | revoked | None
    )
    resource: str | None = None  # the resource a call targets, for scoped tools (e.g. a repository)
    max_calls_per_run: int = 40
    shared_daily_usd: Decimal | float = Decimal(3)
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Decision:
    allowed: bool
    reason: str
    detail: str = ""

    @property
    def decision(self) -> str:
        return "allowed" if self.allowed else "denied"


def _deny(reason: str, detail: str = "") -> Decision:
    return Decision(False, reason, detail)


def _dec(v: Any) -> Decimal:
    return Decimal(str(v))


def _over_budget(run: RunBinding, shared_cap: Decimal | float) -> str | None:
    """Which limit is spent, or None. Default deny: a missing or malformed limit, or an unreadable ledger, counts as spent."""
    if run.cost_unavailable:
        return "accounting unavailable"
    budgets = run.policy.get("budgets")
    if not isinstance(budgets, dict):
        return "no budget in the policy"
    try:
        checks = (
            ("run", run.cost_run_usd, budgets["per_run_usd"]),
            ("actor daily", run.cost_today_usd, budgets["daily_usd"]),
            ("shared daily", run.cost_shared_usd, shared_cap),
        )
        for name, spent, cap in checks:
            if _dec(spent) >= _dec(cap):
                return f"{name} budget spent"
    except (KeyError, InvalidOperation):
        return "malformed budget"
    return None


def decide(p: PolicyInput) -> Decision:
    if not p.authenticated:
        return _deny("UNAUTHENTICATED")
    if len(p.active_runs) != 1:
        return _deny("NO_ACTIVE_RUN", f"{len(p.active_runs)} runs are running")
    run = p.active_runs[0]
    if p.attestation == "missing":
        return _deny("ATTESTATION_MISSING")
    if p.attestation != "ok":
        return _deny("ATTESTATION_MISMATCH")
    if p.tool is None:
        return _deny("UNKNOWN_TOOL")
    if not p.tool_enabled:
        return _deny("TOOL_DISABLED")
    if p.tool.name not in run.policy.get("grants", []):
        return _deny("NOT_GRANTED", f"profile version {run.profile_version}")
    if p.args_problems:
        return _deny("INVALID_ARGUMENTS", "; ".join(p.args_problems))
    if p.tool.provider:
        if p.connection is None:
            return _deny("NOT_CONNECTED", p.tool.provider)
        if p.connection != "active":
            return _deny("CONNECTION_REVOKED", p.tool.provider)
    scopes = run.policy.get("resource_scopes", {}).get(p.tool.name)
    if scopes is not None and p.resource not in scopes:
        return _deny("OUT_OF_SCOPE")
    if run.calls_so_far >= p.max_calls_per_run:
        return _deny("RATE_LIMITED", f"{run.calls_so_far} calls in this run")
    if (over := _over_budget(run, p.shared_daily_usd)) is not None:
        return _deny("BUDGET_EXCEEDED", over)
    if p.tool.approval_required or p.tool.side_effect_class == "external_write":
        return _deny("APPROVAL_REQUIRED")
    return Decision(True, "OK")
