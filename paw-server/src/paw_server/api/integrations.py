"""Integrations hub: what PAW can connect to, and the Google Calendar connector (read-only)."""

import logging
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any
from urllib.parse import urlencode

from fastapi import APIRouter, Query, Request, Response
from fastapi.responses import RedirectResponse

from paw_server.api.deps import SESSION_COOKIE, CurrentActor
from paw_server.core.errors import ApiError
from paw_server.modules.auth import service as auth
from paw_server.modules.auth.oidc import new_pkce
from paw_server.modules.connectors import store
from paw_server.modules.connectors.google_calendar import ConnectorError

log = logging.getLogger("paw.integrations")
router = APIRouter(prefix="/v1/integrations", tags=["integrations"])
PROVIDER = "google_calendar"
PURPOSE = "connect_gcal"


def _cal(request: Request) -> Any:
    cal = request.app.state.calendar
    if cal is None:
        raise ApiError(404)
    return cal


def _redirect_uri(request: Request) -> str:
    base = request.app.state.settings.api_public_url.rstrip("/")
    return f"{base}/v1/integrations/google-calendar/callback"


def _back(
    request: Request, path: str = "/#/integrations", error: str | None = None
) -> RedirectResponse:
    """Back to the web app. The error goes before any #fragment so the page can read it from the query."""
    web = request.app.state.settings.web_url.rstrip("/")
    target = auth.safe_return_to(path)
    base, sep, frag = target.partition("#")
    q = f"?{urlencode({'integration_error': error})}" if error else ""
    return RedirectResponse(f"{web}{base}{q}{sep}{frag}", 302)


def _session(request: Request) -> Any:
    cookie = request.cookies.get(SESSION_COOKIE)
    if not cookie:
        return None
    with request.app.state.pool.connection() as conn:
        return auth.get_session(conn, request.app.state.settings, cookie)


@router.get("")
def catalog(request: Request, actor: CurrentActor) -> dict[str, object]:
    st = request.app.state
    with st.pool.connection() as conn:
        live = store.get_live(conn, actor.id, PROVIDER)
    if st.calendar is None:
        cal: dict[str, object] = {
            "state": "unavailable",
            "reason": "Needs Google sign-in and PAW_CONNECTOR_KEY to be configured.",
        }
    elif live is None:
        cal = {"state": "available"}
    else:
        cal = {
            "state": "connected" if live.status == "active" else live.status,
            "account_email": live.account_email,
            "connected_at": live.created_at.isoformat(),
        }
    return {
        "integrations": [
            {
                "id": "documents",
                "name": "PAW Documents",
                "description": "Search your uploaded documents with sources.",
                "kind": "built_in",
                "state": "connected",
                "tools": [{"name": "docs_search", "access": "read"}],
            },
            {
                "id": PROVIDER,
                "name": "Google Calendar",
                "description": "Let PAW read your upcoming events. Read-only.",
                "kind": "connector",
                "tools": [{"name": "calendar_list_events", "access": "read"}],
                **cal,
            },
        ]
    }


@router.get("/google-calendar/connect")
def connect(
    request: Request, return_to: Annotated[str, Query(max_length=200)] = "/"
) -> RedirectResponse:
    cal = _cal(request)
    st = request.app.state
    st.auth_limiter.check(f"connect:{request.client.host if request.client else '-'}")
    sess = _session(request)
    if sess is None:
        raise ApiError(401)
    verifier, challenge = new_pkce()
    with st.pool.connection() as conn:
        state, nonce = auth.start_attempt(
            conn, purpose=PURPOSE, return_to=return_to, session_id=sess.id, verifier=verifier
        )
    url = cal.authorize_url(
        redirect_uri=_redirect_uri(request), state=state, nonce=nonce, code_challenge=challenge
    )
    return RedirectResponse(url, 302)


@router.get("/google-calendar/callback")
def callback(
    request: Request, code: str | None = None, state: str | None = None, error: str | None = None
) -> RedirectResponse:
    cal = _cal(request)
    st = request.app.state
    st.auth_limiter.check(f"connect-cb:{request.client.host if request.client else '-'}")
    if error or not code or not state:
        return _back(request, error="cancelled")
    with st.pool.connection() as conn:
        attempt = auth.consume_attempt(conn, state)
    if attempt is None or attempt.purpose != PURPOSE:
        return _back(request, error="expired")
    sess = _session(request)
    if sess is None or sess.id != attempt.session_id:  # the browser that started it must finish it
        return _back(request, error="failed")
    try:
        grant = cal.exchange_code(
            code=code,
            redirect_uri=_redirect_uri(request),
            code_verifier=attempt.code_verifier,
            nonce=attempt.nonce,
        )
    except ConnectorError as exc:
        log.warning("calendar connect rejected", extra={"reason": exc.code})
        return _back(request, error="missing_scope" if exc.code == "missing_scope" else "failed")
    with st.pool.connection() as conn:
        store.save_connection(
            conn,
            st.settings.connector_key,
            owner="owner",
            provider=PROVIDER,
            email=grant.email,
            scopes=grant.scopes,
            secrets={
                "refresh_token": grant.refresh_token,
                "access_token": grant.access_token,
                "expires_at": grant.expires_at.isoformat(),
            },
        )
    return _back(request, attempt.return_to)


def _access_token(request: Request, live: store.Connection) -> str:
    cal, st = request.app.state.calendar, request.app.state
    key = st.settings.connector_key
    with st.pool.connection() as conn:
        sec = store.load_secrets(conn, key, live.id)
    if sec is None:  # key rotated or row damaged: the user must reconnect
        with st.pool.connection() as conn:
            store.set_status(conn, live.id, "needs_reauth", "secret_unreadable")
        raise ApiError(409, "needs_reauth", "Reconnect Google Calendar.")
    if datetime.fromisoformat(sec["expires_at"]) > datetime.now(UTC) + timedelta(seconds=60):
        return str(sec["access_token"])
    try:
        token, expires = cal.refresh(sec["refresh_token"])
    except ConnectorError as exc:
        if exc.code == "revoked":
            with st.pool.connection() as conn:
                store.set_status(conn, live.id, "needs_reauth", "revoked")
            raise ApiError(409, "needs_reauth", "Reconnect Google Calendar.") from exc
        raise ApiError(503, "calendar_unavailable", "Google Calendar is unavailable.") from exc
    with st.pool.connection() as conn:
        store.update_secrets(
            conn,
            key,
            live.id,
            {**sec, "access_token": token, "expires_at": expires.isoformat()},
        )
    return str(token)


@router.get("/google-calendar/events")
def events(
    request: Request,
    actor: CurrentActor,
    days: Annotated[int, Query(ge=1, le=31)] = 7,
    limit: Annotated[int, Query(ge=1, le=50)] = 20,
) -> dict[str, object]:
    cal = _cal(request)
    with request.app.state.pool.connection() as conn:
        live = store.get_live(conn, actor.id, PROVIDER)
    if live is None or live.status != "active":
        raise ApiError(
            409, "needs_reauth" if live else "not_connected", "Google Calendar is not connected."
        )
    token = _access_token(request, live)
    now = datetime.now(UTC)
    try:
        items = cal.list_events(
            token, time_min=now, time_max=now + timedelta(days=days), limit=limit
        )
    except ConnectorError as exc:
        if exc.code == "revoked":
            with request.app.state.pool.connection() as conn:
                store.set_status(conn, live.id, "needs_reauth", "revoked")
            raise ApiError(409, "needs_reauth", "Reconnect Google Calendar.") from exc
        raise ApiError(503, "calendar_unavailable", "Google Calendar is unavailable.") from exc
    return {"account_email": live.account_email, "events": items}


@router.delete("/google-calendar", status_code=204)
def disconnect(request: Request, actor: CurrentActor) -> Response:
    cal = _cal(request)
    st = request.app.state
    with st.pool.connection() as conn:
        live = store.get_live(conn, actor.id, PROVIDER)
        sec = store.load_secrets(conn, st.settings.connector_key, live.id) if live else None
    if live is None:
        raise ApiError(404)
    if sec:
        cal.revoke(sec["refresh_token"])  # best effort at Google; we delete our copy regardless
    with st.pool.connection() as conn:
        store.set_status(conn, live.id, "revoked")
    return Response(status_code=204)
