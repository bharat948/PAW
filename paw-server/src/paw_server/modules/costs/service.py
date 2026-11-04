"""Atomic spending reservations. Caller commits authorization before any network I/O.

Lock order: workspace/day row, then the run's advisory lock (when a run is given). Only reserve
takes both, always in this order; settlement locks just the one result row. Accounting contains
no message text and survives conversation purge.
"""

from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

from paw_server.modules.costs.pricing import CostError, Rate
from paw_server.modules.ingestion.jobs import Conn

ZONE = ZoneInfo("Asia/Kolkata")
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
    now: datetime | None = None,
) -> dict[str, Any]:
    """Must run inside a transaction. Returned attempt is not authorized until commit."""
    now = now or datetime.now(UTC)
    day = day_at(now)
    rate = rate_for(conn, model)
    amount = rate.reserve(output)
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
            rate.context,
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


def run_cost(conn: Conn, rid: UUID) -> dict[str, Any]:
    rows = conn.execute(
        "SELECT a.rate_version,r.* FROM actor_cost_attempts a JOIN actor_cost_results r ON r.attempt_id=a.id WHERE a.run_id=%s ORDER BY a.authorized_at,a.id",
        (rid,),
    ).fetchall()
    if not rows:
        return {
            "confirmed_usd": None,
            "reserved_usd": None,
            "unconfirmed_usd": None,
            "status": "unknown",
            "served_model": None,
            "rate_versions": [],
        }
    return {
        "confirmed_usd": str(sum((r["cost_usd"] or ZERO for r in rows), ZERO)),
        "reserved_usd": str(
            sum(
                (
                    r["reserved_usd"]
                    for r in conn.execute(
                        "SELECT a.reserved_usd FROM actor_cost_attempts a JOIN actor_cost_results r ON r.attempt_id=a.id WHERE a.run_id=%s AND r.state IN ('reserved','forwarded','uncertain')",
                        (rid,),
                    ).fetchall()
                ),
                ZERO,
            )
        ),
        "unconfirmed_usd": str(
            sum(
                (
                    r["reserved_usd"]
                    for r in conn.execute(
                        "SELECT a.reserved_usd FROM actor_cost_attempts a JOIN actor_cost_results r ON r.attempt_id=a.id WHERE a.run_id=%s AND r.state='uncertain'",
                        (rid,),
                    ).fetchall()
                ),
                ZERO,
            )
        ),
        "status": "unconfirmed"
        if any(r["state"] == "uncertain" for r in rows)
        else (
            "reserved"
            if any(r["state"] in ("reserved", "forwarded") for r in rows)
            else "confirmed"
        ),
        "served_model": next(
            (r["served_model"] for r in reversed(rows) if r["served_model"]), None
        ),
        "rate_versions": sorted({r["rate_version"] for r in rows}),
    }
