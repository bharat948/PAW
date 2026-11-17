"""Atomic spending reservations. Caller commits authorization before any network I/O.

Lock order: workspace/day row, then the run's advisory lock (when a run is given). Only reserve
takes both, always in this order; settlement locks just the one result row. Accounting contains
no message text and survives conversation purge.
"""

import logging
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

from paw_server.modules.costs.pricing import CostError, Rate
from paw_server.modules.ingestion.jobs import Conn

log = logging.getLogger("paw.costs")
ZONE = ZoneInfo("Asia/Kolkata")
RESERVED_GRACE = timedelta(seconds=60)  # a hold that was never sent after this long is abandoned
FORWARDED_GRACE = timedelta(
    minutes=5
)  # a send with no outcome after this long is labelled uncertain
ZERO = Decimal(0)


def day_at(now: datetime) -> date:
    return now.astimezone(ZONE).date()


def reset_at(now: datetime) -> datetime:
    return datetime.combine(day_at(now) + timedelta(days=1), time(), ZONE).astimezone(UTC)


def rate_for(conn: Conn, model: str, version: str | None = None) -> Rate:
    row = conn.execute(
        "SELECT * FROM ops_rate_cards WHERE model=%s AND (%s::text IS NULL OR version=%s) AND effective_from<=CURRENT_DATE ORDER BY effective_from DESC,version DESC LIMIT 1",
        (model, version, version),
    ).fetchone()
    if not row:
        raise CostError("pricing_unavailable")
    return Rate.from_row(row)


def totals(
    conn: Conn, workspace: UUID, day: date, *, actor: str | None = None, run: UUID | None = None
) -> dict[str, Decimal]:
    row = conn.execute(
        """SELECT coalesce(sum(r.cost_usd) FILTER(WHERE r.state='settled'),0) AS confirmed,
        coalesce(sum(a.reserved_usd) FILTER(WHERE r.state IN ('reserved','forwarded','uncertain')),0) AS reserved,
        coalesce(sum(a.reserved_usd) FILTER(WHERE r.state='uncertain'),0) AS unconfirmed
        FROM actor_cost_attempts a JOIN actor_cost_results r ON r.attempt_id=a.id
        WHERE a.workspace_id=%s AND (%s::uuid IS NOT NULL OR a.budget_day=%s)
        AND (%s::text IS NULL OR a.actor_id=%s) AND (%s::uuid IS NULL OR a.run_id=%s)""",
        (workspace, run, day, actor, actor, run, run),
    ).fetchone()
    assert row
    return {k: Decimal(row[k]) for k in ("confirmed", "reserved", "unconfirmed")}


def used(t: dict[str, Decimal]) -> Decimal:
    return t["confirmed"] + t["reserved"]


def reserve(
    conn: Conn,
    *,
    workspace: UUID,
    owner: str,
    actor: str,
    session: UUID,
    profile: UUID,
    model: str,
    output: int,
    daily_cap: Decimal,
    global_cap: Decimal,
    run: UUID | None = None,
    run_cap: Decimal | None = None,
    input_tokens: int | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Must run inside a transaction. Returned attempt is not authorized until commit.

    `input_tokens` is an upper bound on the request's input (see tokens.input_bound); omit it to reserve the
    model's whole context window."""
    now = now or datetime.now(UTC)
    day = day_at(now)
    rate = rate_for(conn, model)
    amount = rate.reserve(output, input_tokens)
    conn.execute(
        "INSERT INTO actor_budget_days(workspace_id,budget_day) VALUES (%s,%s) ON CONFLICT DO NOTHING",
        (workspace, day),
    )
    conn.execute(
        "SELECT 1 FROM actor_budget_days WHERE workspace_id=%s AND budget_day=%s FOR UPDATE",
        (workspace, day),
    )
    # Per-run concurrency can cross midnight: serialize the run independently of daily buckets.
    if run:
        conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,11))", (str(run),))
    checks = [
        (global_cap, totals(conn, workspace, day)),
        (daily_cap, totals(conn, workspace, day, actor=actor)),
    ]
    if run:
        if run_cap is None:
            raise CostError("missing_run_budget")
        checks.append((run_cap, totals(conn, workspace, day, run=run)))
    if any(not cap.is_finite() or cap <= 0 or used(t) + amount > cap for cap, t in checks):
        raise CostError("budget_exceeded")
    aid = uuid4()
    row = conn.execute(
        """INSERT INTO actor_cost_attempts
        (id,workspace_id,owner_id,actor_id,session_id,run_id,profile_version_id,kind,budget_day,
         requested_model,rate_version,input_bound,output_bound,reserved_usd,authorized_at)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *""",
        (
            aid,
            workspace,
            owner,
            actor,
            session,
            run,
            profile,
            "run" if run else "summary",
            day,
            model,
            rate.version,
            input_tokens or rate.context,
            output,
            amount,
            now,
        ),
    ).fetchone()
    conn.execute("INSERT INTO actor_cost_results(attempt_id) VALUES (%s)", (aid,))
    assert row
    return dict(row)


def forward(conn: Conn, aid: UUID) -> bool:
    """Durable send-intent fence. A released/crashed reservation can never be sent later."""
    return (
        conn.execute(
            "UPDATE actor_cost_results SET state='forwarded',updated_at=now() WHERE attempt_id=%s AND state='reserved' RETURNING attempt_id",
            (aid,),
        ).fetchone()
        is not None
    )


def uncertain(conn: Conn, aid: UUID) -> None:
    conn.execute(
        "UPDATE actor_cost_results SET state='uncertain',updated_at=now() WHERE attempt_id=%s AND state='forwarded'",
        (aid,),
    )


def release(conn: Conn, aid: UUID) -> None:
    # Only requests proven never forwarded may release without usage evidence.
    conn.execute(
        "UPDATE actor_cost_results SET state='released',updated_at=now() WHERE attempt_id=%s AND state='reserved'",
        (aid,),
    )


def settle(conn: Conn, aid: UUID, served_model: str, usage: Any) -> bool:
    row = conn.execute(
        "SELECT a.*,r.state FROM actor_cost_attempts a JOIN actor_cost_results r ON r.attempt_id=a.id WHERE a.id=%s FOR UPDATE OF r",
        (aid,),
    ).fetchone()
    if not row or row["state"] not in ("forwarded", "uncertain"):
        return False
    rate = rate_for(conn, row["requested_model"], row["rate_version"])
    try:
        if served_model not in rate.served_models or not isinstance(usage, dict):
            raise CostError("invalid_usage")
        # Untrusted provider values: Rate.cost rejects anything that is not a non-negative int.
        inputs: Any = usage.get("prompt_tokens")
        outputs: Any = usage.get("completion_tokens")
        details = usage.get("prompt_tokens_details") or {}
        cached = details.get("cached_tokens", 0)
        cost = rate.cost(inputs, outputs, cached)
        if (
            inputs > row["input_bound"]
            or outputs > row["output_bound"]
            or cost > row["reserved_usd"]
        ):
            raise CostError("usage_outside_bound")
    except (CostError, TypeError, AttributeError):
        uncertain(conn, aid)
        return False
    conn.execute(
        "UPDATE actor_cost_results SET state='settled',cost_usd=%s,served_model=%s,tokens_in=%s,tokens_out=%s,cached_tokens=%s,updated_at=now() WHERE attempt_id=%s",
        (cost, served_model, inputs, outputs, cached, aid),
    )
    return True


UNKNOWN_COST: dict[str, Any] = {
    "confirmed_usd": None,
    "reserved_usd": None,
    "unconfirmed_usd": None,
    "status": "unknown",
    "served_model": None,
    "rate_versions": [],
}
HELD = ("reserved", "forwarded", "uncertain")


def run_costs(conn: Conn, run_ids: list[UUID]) -> dict[UUID, dict[str, Any]]:
    """Spend per run in one query. `confirmed` is settled cost; `reserved` is money still held (not yet settled or
    released), of which `unconfirmed` is what an unfinished or unprovable call may have cost. A run with no
    attempts is `unknown`, never zero. Amounts are exact decimal strings; no message content is involved."""
    rows = conn.execute(
        "SELECT a.run_id, a.rate_version, a.reserved_usd, r.state, r.cost_usd, r.served_model "
        "FROM actor_cost_attempts a JOIN actor_cost_results r ON r.attempt_id=a.id "
        "WHERE a.run_id = ANY(%s) ORDER BY a.authorized_at, a.id",
        (run_ids,),
    ).fetchall()
    grouped: dict[UUID, list[dict[str, Any]]] = {}
    for r in rows:
        grouped.setdefault(r["run_id"], []).append(r)
    out: dict[UUID, dict[str, Any]] = {}
    for rid in run_ids:
        rs = grouped.get(rid)
        if not rs:
            out[rid] = dict(UNKNOWN_COST)
            continue
        out[rid] = {
            "confirmed_usd": str(sum((r["cost_usd"] or ZERO for r in rs), ZERO)),
            "reserved_usd": str(sum((r["reserved_usd"] for r in rs if r["state"] in HELD), ZERO)),
            "unconfirmed_usd": str(
                sum((r["reserved_usd"] for r in rs if r["state"] == "uncertain"), ZERO)
            ),
            "status": "unconfirmed"
            if any(r["state"] == "uncertain" for r in rs)
            else (
                "reserved"
                if any(r["state"] in ("reserved", "forwarded") for r in rs)
                else "confirmed"
            ),
            "served_model": next(
                (r["served_model"] for r in reversed(rs) if r["served_model"]), None
            ),
            "rate_versions": sorted({r["rate_version"] for r in rs}),
        }
    return out


def run_cost(conn: Conn, rid: UUID) -> dict[str, Any]:
    return run_costs(conn, [rid])[rid]


def spend_today(
    conn: Conn,
    workspace: UUID,
    actor: str,
    *,
    actor_cap: Decimal,
    shared_cap: Decimal,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Today's (Asia/Kolkata) spend for one actor and for everyone, beside the caps and the reset time."""
    now = now or datetime.now(UTC)
    day = day_at(now)

    def view(t: dict[str, Decimal], cap: Decimal) -> dict[str, str]:
        return {
            "confirmed_usd": str(t["confirmed"]),
            "reserved_usd": str(t["reserved"]),
            "unconfirmed_usd": str(t["unconfirmed"]),
            "limit_usd": str(cap),
        }

    return {
        "day": str(day),
        "resets_at": reset_at(now).isoformat(),
        "actor": view(totals(conn, workspace, day, actor=actor), actor_cap),
        "shared": view(totals(conn, workspace, day), shared_cap),
    }


# ---- crash accounting and late evidence (A1-09) -------------------------------------------------------------------


def sweep(conn: Conn, now: datetime | None = None) -> dict[str, list[UUID]]:
    """Resolve what a crash leaves behind. Caller commits.

    reserved  -> released : `forward` is the durable send fence and both it and `release` only act on state
                'reserved', so a hold still in that state was provably never sent. If this wins a race, the late
                sender's `forward` fails and the call is denied; if the sender wins, this does nothing.
    forwarded -> uncertain: the send may have been billed. Both states hold the reservation, so this is a label;
                a late `settle` still works from either.
    Nothing here ever frees money that might have been spent."""
    now = now or datetime.now(UTC)
    released = conn.execute(
        "UPDATE actor_cost_results r SET state='released', updated_at=now() FROM actor_cost_attempts a "
        "WHERE a.id=r.attempt_id AND r.state='reserved' AND a.authorized_at < %s RETURNING r.attempt_id",
        (now - RESERVED_GRACE,),
    ).fetchall()
    marked = conn.execute(
        "UPDATE actor_cost_results SET state='uncertain', updated_at=now() "
        "WHERE state='forwarded' AND updated_at < %s RETURNING attempt_id",
        (now - FORWARDED_GRACE,),
    ).fetchall()
    return {
        "released": [r["attempt_id"] for r in released],
        "uncertain": [r["attempt_id"] for r in marked],
    }


TERMINAL_RUN = ("SUCCEEDED", "FAILED", "CANCELLED", "INTERRUPTED")


def reconcile_run(conn: Conn, run: UUID) -> str:
    """Settle a finished run's one uncertain attempt from the runtime's run-level usage, when that is unambiguous.

    The runtime reports usage for the whole run. Subtract what the ledger already settled; if exactly one attempt of
    the run is uncertain and nothing else is still in flight, the remainder is the usage of that one call. Anything
    less clear leaves the reservation held: missing, zero or contradictory evidence is never taken as 'free'.
    Settlement still enforces the attempt's own bounds, so the worst case is a wrong split inside an amount that
    was already reserved. Caller commits."""
    conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,11))", (str(run),))
    ev = conn.execute(
        "SELECT state, tokens_in, tokens_out, cache_read, cache_write FROM agent_runs WHERE id=%s",
        (run,),
    ).fetchone()
    if (
        not ev
        or ev["state"] not in TERMINAL_RUN
        or ev["tokens_in"] is None
        or ev["tokens_out"] is None
    ):
        return "no_evidence"
    if ev["cache_read"] or ev["cache_write"]:
        return "ambiguous_cache"  # cannot tell whether input_tokens includes the cached part
    rows = conn.execute(
        "SELECT a.id, a.requested_model, r.state, r.tokens_in, r.tokens_out FROM actor_cost_attempts a "
        "JOIN actor_cost_results r ON r.attempt_id=a.id WHERE a.run_id=%s FOR UPDATE OF r",
        (run,),
    ).fetchall()
    if any(r["state"] in ("reserved", "forwarded") for r in rows):
        return "pending"  # a call may still report for itself
    uncertain = [r for r in rows if r["state"] == "uncertain"]
    if len(uncertain) != 1:
        return "no_candidate" if not uncertain else "not_attributable"
    settled = [r for r in rows if r["state"] == "settled"]
    inputs = ev["tokens_in"] - sum(r["tokens_in"] or 0 for r in settled)
    outputs = ev["tokens_out"] - sum(r["tokens_out"] or 0 for r in settled)
    if inputs < 0 or outputs < 0:
        return "inconsistent"
    if inputs == 0 and outputs == 0:
        return "no_usage"
    target = uncertain[0]
    ok = settle(
        conn,
        target["id"],
        target["requested_model"],
        {"prompt_tokens": inputs, "completion_tokens": outputs},
    )
    if ok:
        log.info(
            "late_evidence settled an uncertain attempt",
            extra={"run_id": str(run), "attempt": str(target["id"])},
        )
    return "settled" if ok else "out_of_bounds"


def reconcile(conn: Conn, now: datetime | None = None) -> dict[str, Any]:
    """One pass: sweep, then try the late-evidence settlement for every recent run that still has an uncertain
    attempt. Safe to repeat and to run from several workers. Caller commits."""
    swept = sweep(conn, now)
    today = day_at(now or datetime.now(UTC))
    runs = conn.execute(
        "SELECT DISTINCT a.run_id FROM actor_cost_attempts a JOIN actor_cost_results r ON r.attempt_id=a.id "
        "WHERE r.state='uncertain' AND a.run_id IS NOT NULL AND a.budget_day >= %s LIMIT 50",
        (today - timedelta(days=2),),
    ).fetchall()
    outcomes: dict[str, int] = {}
    for r in runs:
        with conn.transaction():
            o = reconcile_run(conn, r["run_id"])
        outcomes[o] = outcomes.get(o, 0) + 1
    return {**swept, "runs": outcomes}
