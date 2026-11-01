"""Google sign-in, server-side sessions, logout. See modules/auth."""

import logging
from datetime import UTC, datetime
from typing import Annotated, Literal
from urllib.parse import urlencode

from fastapi import APIRouter, Query, Request, Response
from fastapi.responses import RedirectResponse

from paw_server.api.deps import SESSION_COOKIE, CurrentActor
from paw_server.core.errors import ApiError
from paw_server.modules.auth import service
from paw_server.modules.auth.oidc import IdentityError, new_pkce

log = logging.getLogger("paw.auth")
router = APIRouter(prefix="/v1/auth", tags=["auth"])


def _google_enabled(request: Request) -> bool:
    return bool(
        request.app.state.settings.auth_mode in ("google", "both") and request.app.state.idp
    )


def _redirect_uri(request: Request) -> str:
    return f"{request.app.state.settings.api_public_url.rstrip('/')}/v1/auth/google/callback"


def _back(request: Request, path: str = "/", error: str | None = None) -> RedirectResponse:
    web = request.app.state.settings.web_url.rstrip("/")
    suffix = f"?{urlencode({'auth_error': error})}" if error else ""
    return RedirectResponse(
        f"{web}{service.safe_return_to(path) if not error else '/'}{suffix}", 302
    )


@router.get("/config")
def auth_config(request: Request) -> dict[str, object]:
    """Public: tells the login screen which sign-in methods exist. No secrets."""
    s = request.app.state.settings
    return {"mode": s.auth_mode, "google": _google_enabled(request)}


@router.get("/google/start")
def google_start(
    request: Request,
    return_to: Annotated[str, Query(max_length=200)] = "/",
    purpose: Literal["login", "stepup"] = "login",
) -> RedirectResponse:
    if not _google_enabled(request):
        raise ApiError(404)
    st = request.app.state
    st.auth_limiter.check(f"start:{request.client.host if request.client else '-'}")
    session_id = None
    if purpose == "stepup":
        cookie = request.cookies.get(SESSION_COOKIE)
        with st.pool.connection() as conn:
            sess = service.get_session(conn, st.settings, cookie) if cookie else None
        if sess is None:
            raise ApiError(401)
        session_id = sess.id
    verifier, challenge = new_pkce()
    with st.pool.connection() as conn:
        state, nonce = service.start_attempt(
            conn, purpose=purpose, return_to=return_to, session_id=session_id, verifier=verifier
        )
    url = st.idp.authorize_url(
        redirect_uri=_redirect_uri(request),
        state=state,
        nonce=nonce,
        code_challenge=challenge,
        prompt="login" if purpose == "stepup" else "select_account",
    )
    return RedirectResponse(url, 302)


@router.get("/google/callback")
def google_callback(
    request: Request,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
) -> RedirectResponse:
    if not _google_enabled(request):
        raise ApiError(404)
    st = request.app.state
    st.auth_limiter.check(f"callback:{request.client.host if request.client else '-'}")
    if error or not code or not state:
        return _back(request, error="cancelled")
    with st.pool.connection() as conn:
        attempt = service.consume_attempt(conn, state)
    if attempt is None:
        return _back(request, error="expired")
    try:
        ident = st.idp.identity_from_code(
            code=code,
            redirect_uri=_redirect_uri(request),
            code_verifier=attempt.code_verifier,
            nonce=attempt.nonce,
        )
    except IdentityError as exc:
        log.warning("google sign-in rejected", extra={"reason": exc.code})
        return _back(request, error="failed")
    email = (
        ident.email.strip().lower()
    )  # normalise here too: never rely on the provider having done it
    if email not in st.settings.allowed_emails:
        log.warning(
            "google sign-in rejected", extra={"reason": "not_allowed"}
        )  # never log the address
        return _back(request, error="not_allowed")
    with st.pool.connection() as conn:
        if attempt.purpose == "stepup":
            sess_cookie = request.cookies.get(SESSION_COOKIE)
            current = service.get_session(conn, st.settings, sess_cookie) if sess_cookie else None
            ok = (
                current is not None
                and current.id == attempt.session_id
                and current.email == email
                and service.mark_stepup(conn, current.id)
            )
            return _back(request, attempt.return_to) if ok else _back(request, error="failed")
        token, _ = service.create_session(conn, st.settings, email)
    resp = _back(request, attempt.return_to)
    resp.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=st.settings.session_ttl_s,
        httponly=True,
        secure=st.settings.api_public_url.startswith("https://"),
        samesite="lax",
        path="/",
    )
    return resp


@router.get("/me")
def me(request: Request, actor: CurrentActor) -> dict[str, object]:
    sess = getattr(request.state, "session", None)
    if sess is None:
        return {"mode": "token"}
    return {
        "mode": "session",
        "email": sess.email,
        "csrf_token": sess.csrf_token,
        "expires_at": sess.expires_at.isoformat(),
        "authenticated_at": sess.authenticated_at.isoformat(),
        "stepup_valid": sess.stepup_valid(request.app.state.settings, datetime.now(UTC)),
    }


@router.post("/logout", status_code=204)
def logout(request: Request, actor: CurrentActor) -> Response:
    sess = getattr(request.state, "session", None)
    if sess is not None:
        with request.app.state.pool.connection() as conn:
            service.revoke(conn, sess.id)
    resp = Response(status_code=204)
    resp.delete_cookie(SESSION_COOKIE, path="/")
    return resp
