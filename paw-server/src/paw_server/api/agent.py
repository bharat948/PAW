"""Admin Hermes: sessions, runs, and a durable event stream. The browser only ever reads what PostgreSQL holds."""

import json
import time
from collections.abc import Iterator
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Header, Query, Request, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from paw_server.api.deps import CurrentActor
from paw_server.core.errors import ApiError
from paw_server.modules.runtime import lifecycle as lc
from paw_server.modules.runtime.protocol import TERMINAL_TYPES

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


def _run_out(r: dict[str, Any]) -> dict[str, Any]:
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
    }


def _own_session(request: Request, actor: Any, session_id: UUID) -> dict[str, Any]:
    with request.app.state.pool.connection() as conn:
        s = lc.get_session(conn, session_id, actor.id)
    if s is None:
        raise ApiError(404)
    return s


@router.get("/v1/runtime/health")
def runtime_health(request: Request, actor: CurrentActor) -> dict[str, Any]:
    rt = request.app.state.runtime
    if rt is None:
        return {"configured": False, "ready": False, "missing": ["not_configured"]}
    missing = rt.missing_capabilities()
    return {"configured": True, "ready": not missing and rt.healthy(), "missing": missing}


@router.post("/v1/agent-sessions", status_code=201)
def create_session(request: Request, actor: CurrentActor, body: SessionIn) -> dict[str, Any]:
    _need_runtime(request)
    with request.app.state.pool.connection() as conn:
        s = lc.create_session(
            conn, owner=actor.id, workspace_id=actor.workspace_id, title=body.title
        )
    return {"id": str(s["id"]), "title": s["title"], "created_at": s["created_at"].isoformat()}


@router.get("/v1/agent-sessions")
def list_sessions(request: Request, actor: CurrentActor) -> dict[str, Any]:
    with request.app.state.pool.connection() as conn:
        rows = lc.list_sessions(conn, actor.id)
    return {
        "sessions": [
            {"id": str(s["id"]), "title": s["title"], "created_at": s["created_at"].isoformat()}
            for s in rows
        ]
    }


@router.get("/v1/agent-sessions/{session_id}/runs")
def list_runs(request: Request, actor: CurrentActor, session_id: UUID) -> dict[str, Any]:
    _own_session(request, actor, session_id)
    with request.app.state.pool.connection() as conn:
        return {"runs": [_run_out(r) for r in lc.list_runs(conn, session_id)]}


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
    return _run_out(run)


@router.get("/v1/agent-runs/{run_id}")
def get_run(request: Request, actor: CurrentActor, run_id: UUID) -> dict[str, Any]:
    with request.app.state.pool.connection() as conn:
        run = lc.get_run(conn, run_id, actor.id)
    if run is None:
        raise ApiError(404)
    return _run_out(run)


@router.post("/v1/agent-runs/{run_id}/cancel")
def cancel_run(request: Request, actor: CurrentActor, run_id: UUID) -> dict[str, Any]:
    rt = _need_runtime(request)
    st = request.app.state
    with st.pool.connection() as conn:
        if lc.get_run(conn, run_id, actor.id) is None:
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
        run = lc.get_run(conn, run_id, actor.id)
    assert run is not None
    return _run_out(run)


def _sse(seq: int, type_: str, payload: dict[str, Any]) -> str:
    body = json.dumps({"seq": seq, "type": type_, "payload": payload})
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
        if lc.get_run(conn, run_id, actor.id) is None:
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
