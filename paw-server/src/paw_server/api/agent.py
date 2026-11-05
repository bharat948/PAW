"""Admin Hermes: sessions, runs, and a durable event stream. The browser only ever reads what PostgreSQL holds."""

import json
import time
from collections.abc import Iterator
from decimal import Decimal
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Header, Query, Request, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from paw_server.api.deps import CurrentActor
from paw_server.core.errors import ApiError
from paw_server.modules.costs import service as costs
from paw_server.modules.gateway import attestation
from paw_server.modules.identity import service as identity
from paw_server.modules.runtime import lifecycle as lc
from paw_server.modules.runtime.protocol import TERMINAL_TYPES
from paw_server.modules.sessions import privacy
from paw_server.modules.sessions import service as ledger
from paw_server.modules.sessions.journal import Journal

router = APIRouter(tags=["agent"])
POLL_S = 0.15
KEEPALIVE_S = 15.0
MAX_STREAM_S = 3600.0


class SessionIn(BaseModel):
    title: str = Field(default="", max_length=200)


class RunIn(BaseModel):
    client_request_id: str = Field(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9._:-]+$")
    message: str = Field(min_length=1, max_length=8000)


def _need_runtime(request: Request) -> Any:
    rt = request.app.state.runtime
    if rt is None:
        raise ApiError(503, "agent_unavailable", "Admin Hermes is not configured.")
    return rt


def _iso(v: Any) -> str | None:
    return v.isoformat() if v else None


def _run_out(r: dict[str, Any], cost: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "id": str(r["id"]),
        "session_id": str(r["session_id"]),
        "client_request_id": r["client_request_id"],
        "message": r["user_message"],
        "state": r["state"],
        "output": r["output"],
        "error_code": r["error_code"],
        "usage": {"input_tokens": r["tokens_in"], "output_tokens": r["tokens_out"]}
        if r["tokens_in"] is not None
        else None,
        "queued_at": _iso(r["queued_at"]),
        "started_at": _iso(r["started_at"]),
        "finished_at": _iso(r["finished_at"]),
        "cost": cost if cost is not None else dict(costs.UNKNOWN_COST),
    }


def _runs_out(request: Request, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Runs with their spend, read in one query. The caller has already scoped `rows` to the owner."""
    with request.app.state.pool.connection() as conn:
        spend = costs.run_costs(conn, [r["id"] for r in rows])
        conn.commit()
    return [_run_out(r, spend[r["id"]]) for r in rows]


def _own_session(request: Request, actor: Any, session_id: UUID) -> dict[str, Any]:
    with request.app.state.pool.connection() as conn:
        privacy.replay(conn, Journal(request.app.state.settings.deletion_journal_dir))
        s = lc.get_session(conn, session_id, actor.id)
    if s is None or s["workspace_id"] != actor.workspace_id:
        raise ApiError(404)
    return s


@router.get("/v1/runtime/health")
def runtime_health(request: Request, actor: CurrentActor) -> dict[str, Any]:
    st = request.app.state
    rt = st.runtime
    if rt is None:
        return {
            "configured": False,
            "ready": False,
            "missing": ["not_configured"],
            "config": "unknown",
        }
    missing = rt.missing_capabilities()
    with st.pool.connection() as conn:
        identity.ensure_default_profile(conn)
        active = identity.get_active(conn, identity.ADMIN)
        status = attestation.runtime_status(
            conn, st.settings.gateway_runtime, active.compiled.config_fragment if active else None
        )
    # "config" is whether the container runs the tool list and model of the active profile; a mismatch means restart
    mismatch = st.settings.gateway_attestation == "required" and status.state != "matches"
    return {
        "configured": True,
        "ready": not missing and rt.healthy() and not mismatch,
        "missing": missing + (["config_" + status.state] if mismatch else []),
        "config": status.state,
    }


@router.post("/v1/agent-sessions", status_code=201)
def create_session(request: Request, actor: CurrentActor, body: SessionIn) -> dict[str, Any]:
    _need_runtime(request)
    with request.app.state.pool.connection() as conn:
        s = lc.create_session(
            conn, owner=actor.id, workspace_id=actor.workspace_id, title=body.title
        )
    return _session_out(s)


@router.get("/v1/agent-sessions")
def list_sessions(
    request: Request,
    actor: CurrentActor,
    cursor: UUID | None = None,
    limit: int = Query(default=30, ge=1, le=100),
) -> dict[str, Any]:
    with request.app.state.pool.connection() as conn:
        privacy.replay(conn, Journal(request.app.state.settings.deletion_journal_dir))
        rows = conn.execute(
            "SELECT s.*,a.display_name AS actor_name FROM agent_sessions s JOIN actors a ON a.id=s.agent_actor_id "
            "WHERE s.owner=%s AND s.workspace_id=%s AND s.deleted_at IS NULL AND s.kind='chat' AND s.status='active' "
            "AND (%s::uuid IS NULL OR (s.last_activity_at,s.id) < "
            "(SELECT last_activity_at,id FROM agent_sessions WHERE id=%s AND owner=%s AND workspace_id=%s)) "
            "ORDER BY s.last_activity_at DESC,s.id DESC LIMIT %s",
            (actor.id, actor.workspace_id, cursor, cursor, actor.id, actor.workspace_id, limit + 1),
        ).fetchall()
        conn.commit()
    return {
        "sessions": [_session_out(s) for s in rows[:limit]],
        "next_cursor": rows[limit - 1]["id"] if len(rows) > limit else None,
    }


@router.get("/v1/agent-sessions/{session_id}/runs")
def list_runs(request: Request, actor: CurrentActor, session_id: UUID) -> dict[str, Any]:
    _own_session(request, actor, session_id)
    with request.app.state.pool.connection() as conn:
        rows = lc.list_runs(conn, session_id)
    return {"runs": _runs_out(request, rows)}


@router.post("/v1/agent-sessions/{session_id}/runs")
def submit_run(
    request: Request, actor: CurrentActor, session_id: UUID, body: RunIn, response: Response
) -> dict[str, Any]:
    """202 with the new run, or 200 with the existing one when the same client_request_id is sent again."""
    _need_runtime(request)
    _own_session(request, actor, session_id)
    st = request.app.state
    try:
        with st.pool.connection() as conn:
            run, created = lc.admit(
                conn,
                session_id=session_id,
                actor_id=actor.id,
                client_request_id=body.client_request_id,
                message=body.message,
                queue_limit=st.settings.agent_queue_limit,
                model=st.settings.agent_model,
            )
    except lc.AdmissionError as exc:
        if exc.kind == "missing":
            raise ApiError(404) from exc
        if exc.kind == "full":
            raise ApiError(429, "queue_full", "Admin Hermes is busy. Try again shortly.") from exc
        if exc.kind == "busy":
            raise ApiError(
                409, "session_busy", "This conversation already has a run in progress."
            ) from exc
        raise ApiError(
            409, "idempotency_conflict", "That request id was used with a different message."
        ) from exc
    response.status_code = 202 if created else 200
    return _runs_out(request, [run])[0]


@router.get("/v1/agent-spend")
def agent_spend(request: Request, actor: CurrentActor) -> dict[str, Any]:
    """Today's model spend for the signed-in owner's workspace: confirmed, held, and unconfirmed (it may or may not
    have been billed), beside the limits and the next reset. Amounts only: no conversation content."""
    st = request.app.state
    with st.pool.connection() as conn:
        active = identity.get_active(conn, identity.ADMIN)
        if active is None:
            raise ApiError(503, "profile_unavailable", "Admin Hermes has no active profile.")
        out = costs.spend_today(
            conn,
            actor.workspace_id,
            identity.ADMIN,
            actor_cap=Decimal(str(active.compiled.policy["budgets"]["daily_usd"])),
            shared_cap=st.settings.cost_shared_daily_usd,
        )
        conn.commit()
    return out


@router.get("/v1/agent-runs/{run_id}")
def get_run(request: Request, actor: CurrentActor, run_id: UUID) -> dict[str, Any]:
    with request.app.state.pool.connection() as conn:
        privacy.replay(conn, Journal(request.app.state.settings.deletion_journal_dir))
        run = lc.get_run(conn, run_id, actor.id, actor.workspace_id)
    if run is None:
        raise ApiError(404)
    return _runs_out(request, [run])[0]


@router.post("/v1/agent-runs/{run_id}/cancel")
def cancel_run(request: Request, actor: CurrentActor, run_id: UUID) -> dict[str, Any]:
    rt = _need_runtime(request)
    st = request.app.state
    with st.pool.connection() as conn:
        privacy.replay(conn, Journal(st.settings.deletion_journal_dir))
        if lc.get_run(conn, run_id, actor.id, actor.workspace_id) is None:
            raise ApiError(404)
        outcome, hermes_id = lc.request_cancel(conn, run_id)
    if outcome == "not_yet":
        raise ApiError(409, "not_cancellable_yet", "The run is starting. Try again in a moment.")
    if outcome == "stopping" and hermes_id:
        try:
            rt.stop_run(hermes_id)
        except (
            Exception
        ):  # the worker's stream still ends the run; a failed stop is visible as a late finish
            pass
    with st.pool.connection() as conn:
        run = lc.get_run(conn, run_id, actor.id, actor.workspace_id)
    assert run is not None
    return _runs_out(request, [run])[0]


def _sse(seq: int, type_: str, payload: dict[str, Any]) -> str:
    body = json.dumps(
        {"seq": seq, "type": type_, "payload": {k: v for k, v in payload.items() if k != "raw"}}
    )
    return f"id: {seq}\nevent: {type_}\ndata: {body}\n\n"


@router.get("/v1/agent-runs/{run_id}/events")
def run_events(
    request: Request,
    actor: CurrentActor,
    run_id: UUID,
    after: Annotated[int, Query(ge=0)] = 0,
    last_event_id: Annotated[str | None, Header()] = None,
) -> StreamingResponse:
    """Server-sent events replayed from PostgreSQL. Reconnect with `?after=<seq>` or `Last-Event-ID`; ids are the
    stored sequence numbers, so a client never sees a gap or a duplicate. The stream ends after the terminal event."""
    st = request.app.state
    with st.pool.connection() as conn:
        privacy.replay(conn, Journal(st.settings.deletion_journal_dir))
        if lc.get_run(conn, run_id, actor.id, actor.workspace_id) is None:
            raise ApiError(404)
    start = after
    if last_event_id and last_event_id.isdigit():
        start = max(start, int(last_event_id))

    def gen() -> Iterator[str]:
        last, began, quiet_since = start, time.monotonic(), time.monotonic()
        while time.monotonic() - began < MAX_STREAM_S:
            with st.pool.connection() as conn:
                rows = lc.events_after(conn, run_id, last)
                done = False
                if not rows:
                    run = conn.execute(
                        "SELECT state FROM agent_runs WHERE id = %s", (run_id,)
                    ).fetchone()
                    conn.commit()
                    done = run is None or run["state"] in lc.TERMINAL
            for r in rows:
                yield _sse(r["seq"], r["type"], r["payload"])
                last = r["seq"]
                quiet_since = time.monotonic()
                if r["type"] in TERMINAL_TYPES:
                    return
            if done:
                return
            if time.monotonic() - quiet_since > KEEPALIVE_S:
                yield ": keepalive\n\n"
                quiet_since = time.monotonic()
            time.sleep(POLL_S)

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )


# Conversation reads come from the ledger, rather than being reconstructed from successful runs.
def _session_out(s: dict[str, Any]) -> dict[str, Any]:
    keys = (
        "id",
        "title",
        "created_at",
        "last_activity_at",
        "requester_actor_id",
        "agent_actor_id",
        "actor_name",
        "kind",
        "forked_from_session_id",
        "fork_seq",
        "delegated_from_session_id",
        "root_session_id",
    )
    return {k: s.get(k) for k in keys}


def _message_out(m: dict[str, Any]) -> dict[str, Any]:
    return {
        k: m.get(k)
        for k in (
            "id",
            "session_id",
            "seq",
            "kind",
            "author_actor_id",
            "run_id",
            "tool_invocation_id",
            "status",
            "created_at",
            "body",
            "data",
            "forgotten",
            "source_message_id",
        )
    }


class RenameIn(BaseModel):
    title: str = Field(min_length=1, max_length=200)


class ForkIn(RunIn):
    message_id: UUID


class DeleteIn(BaseModel):
    include_delegated: bool


@router.get("/v1/agent-sessions/{session_id}/messages")
def list_messages(
    request: Request,
    actor: CurrentActor,
    session_id: UUID,
    after: int = Query(default=0, ge=0),
    limit: int = Query(default=100, ge=1, le=200),
) -> dict[str, Any]:
    _own_session(request, actor, session_id)
    with request.app.state.pool.connection() as conn:
        rows = [m for m in ledger.messages(conn, session_id) if m["seq"] > after]
        summaries = conn.execute(
            "SELECT id,from_seq,to_seq,message_id,invalidated_at,model FROM agent_summaries WHERE session_id=%s",
            (session_id,),
        ).fetchall()
        children = conn.execute(
            "SELECT s.*,a.display_name AS actor_name FROM agent_sessions s JOIN actors a ON a.id=s.agent_actor_id "
            "WHERE s.delegated_from_session_id=%s AND s.owner=%s AND s.workspace_id=%s AND s.deleted_at IS NULL ORDER BY s.last_activity_at DESC",
            (session_id, actor.id, actor.workspace_id),
        ).fetchall()
        job = conn.execute(
            "SELECT state,error_code FROM agent_session_jobs WHERE session_id=%s", (session_id,)
        ).fetchone()
        conn.commit()
    page = rows[:limit]
    return {
        "messages": [_message_out(m) for m in page],
        "next_after": page[-1]["seq"] if len(rows) > limit else None,
        "summaries": summaries,
        "children": [_session_out(s) for s in children],
        "summary_job": job,
    }


@router.post("/v1/agent-sessions/{session_id}/rename")
def rename_session(
    request: Request, actor: CurrentActor, session_id: UUID, body: RenameIn
) -> dict[str, Any]:
    _own_session(request, actor, session_id)
    with request.app.state.pool.connection() as conn, conn.transaction():
        s = ledger.lock(conn, session_id)
        if s["deleted_at"]:
            raise ApiError(404)
        title = body.title.strip()
        if not title:
            raise ApiError(422, "invalid_title", "Enter a conversation title.")
        s = conn.execute(
            "UPDATE agent_sessions SET title=%s,title_source='user' WHERE id=%s RETURNING *",
            (title, session_id),
        ).fetchone()
        assert s is not None
    return _session_out(s)


@router.post("/v1/agent-sessions/{session_id}/fork", status_code=201)
def fork_session(
    request: Request, actor: CurrentActor, session_id: UUID, body: ForkIn
) -> dict[str, Any]:
    _need_runtime(request)
    _own_session(request, actor, session_id)
    with request.app.state.pool.connection() as conn, conn.transaction():
        conn.execute("SELECT pg_advisory_xact_lock(hashtextextended('agent:admission', 0))")
        try:
            existing = conn.execute(
                "SELECT * FROM agent_sessions WHERE owner=%s AND forked_from_session_id=%s AND fork_request_id=%s AND deleted_at IS NULL",
                (actor.id, session_id, body.client_request_id),
            ).fetchone()
            if existing:
                prior = lc._existing(conn, existing["id"], body.client_request_id)
                if (
                    prior is None
                    or prior["user_message"] != body.message
                    or existing["fork_message_id"] != body.message_id
                ):
                    raise ApiError(
                        409, "idempotency_conflict", "That edit request was already used."
                    )
                return {"session": _session_out(existing), "run": _run_out(prior)}
            s = privacy.fork(conn, session_id, body.message_id, actor.id, actor.workspace_id)
            conn.execute(
                "UPDATE agent_sessions SET fork_request_id=%s,fork_message_id=%s WHERE id=%s",
                (body.client_request_id, body.message_id, s["id"]),
            )
            run, _ = lc.admit(
                conn,
                session_id=s["id"],
                actor_id=actor.id,
                client_request_id=body.client_request_id,
                message=body.message,
                queue_limit=request.app.state.settings.agent_queue_limit,
                model=request.app.state.settings.agent_model,
            )
        except ValueError as exc:
            raise ApiError(404) from exc
        except lc.AdmissionError as exc:
            raise ApiError(429, "queue_full", "Admin Hermes is busy. Try again shortly.") from exc
    return {"session": _session_out(s), "run": _run_out(run)}


def _stop_private_runs(request: Request, runtime_ids: list[str]) -> None:
    if request.app.state.runtime:
        for hid in runtime_ids:
            try:
                request.app.state.runtime.stop_run(hid)
            except Exception:
                pass  # the cancel deadline and redaction fence remain active


@router.post("/v1/agent-sessions/{session_id}/messages/{message_id}/forget")
def forget_message(
    request: Request, actor: CurrentActor, session_id: UUID, message_id: UUID
) -> dict[str, Any]:
    _own_session(request, actor, session_id)
    with request.app.state.pool.connection() as conn:
        try:
            receipt, runtime_ids = privacy.request(
                conn,
                Journal(request.app.state.settings.deletion_journal_dir),
                session_id=session_id,
                owner=actor.id,
                workspace_id=actor.workspace_id,
                message_id=message_id,
            )
        except ValueError as exc:
            raise ApiError(404) from exc
    _stop_private_runs(request, runtime_ids)
    return {"id": receipt.id, "forgotten": True}


@router.post("/v1/agent-sessions/{session_id}/delete")
def delete_session(
    request: Request, actor: CurrentActor, session_id: UUID, body: DeleteIn
) -> dict[str, Any]:
    _own_session(request, actor, session_id)
    with request.app.state.pool.connection() as conn:
        receipt, runtime_ids = privacy.request(
            conn,
            Journal(request.app.state.settings.deletion_journal_dir),
            session_id=session_id,
            owner=actor.id,
            workspace_id=actor.workspace_id,
            include_delegated=body.include_delegated,
        )
    _stop_private_runs(request, runtime_ids)
    return {"id": receipt.id, "deleted": True, "purge_days": 30, "backup_expiry_days": 35}


@router.get("/v1/agent-activity")
def all_activity(
    request: Request,
    actor: CurrentActor,
    cursor: UUID | None = None,
    limit: int = Query(default=100, ge=1, le=200),
) -> dict[str, Any]:
    with request.app.state.pool.connection() as conn:
        privacy.replay(conn, Journal(request.app.state.settings.deletion_journal_dir))
        rows = conn.execute(
            "SELECT m.*,c.body,c.data,(d.message_id IS NOT NULL) AS forgotten FROM agent_session_messages m "
            "JOIN agent_sessions s ON s.id=m.session_id LEFT JOIN agent_message_contents c ON c.message_id=m.id "
            "LEFT JOIN agent_message_redactions d ON d.message_id=m.id WHERE s.owner=%s AND s.workspace_id=%s AND s.deleted_at IS NULL "
            "AND (%s::uuid IS NULL OR (m.created_at,m.id) < (SELECT m2.created_at,m2.id FROM agent_session_messages m2 "
            "JOIN agent_sessions s2 ON s2.id=m2.session_id WHERE m2.id=%s AND s2.owner=%s AND s2.workspace_id=%s)) "
            "ORDER BY m.created_at DESC,m.id DESC LIMIT %s",
            (actor.id, actor.workspace_id, cursor, cursor, actor.id, actor.workspace_id, limit + 1),
        ).fetchall()
        conn.commit()
    return {
        "messages": [_message_out(m) for m in rows[:limit]],
        "next_cursor": rows[limit - 1]["id"] if len(rows) > limit else None,
    }
