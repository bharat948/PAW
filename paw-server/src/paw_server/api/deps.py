import hmac
from datetime import UTC, datetime
from typing import Annotated

from fastapi import Depends, Header, Request

from paw_server.core.config import Settings
from paw_server.core.errors import ApiError
from paw_server.modules.auth import service as auth_service
from paw_server.modules.ingestion.models import Actor

SESSION_COOKIE = "paw_session"
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


def _owner_actor(request: Request) -> Actor:
    """The single owner's default workspace (D006)."""
    with request.app.state.pool.connection() as conn:
        row = conn.execute(
            "SELECT id FROM workspaces WHERE slug = 'default' AND owner_actor_id = 'owner' "
            "AND status = 'active'"
        ).fetchone()
        conn.commit()
    if row is None:
        raise ApiError(503, "workspace_unavailable", "The workspace is not available.")
    return Actor(id="owner", workspace_id=row["id"])


def get_actor(request: Request, authorization: Annotated[str | None, Header()] = None) -> Actor:
    """Authenticate the owner by dev bearer token (mode token/both) or by Google session cookie (google/both).

    Cookie-authenticated writes also need the session's CSRF token in X-CSRF-Token.
    """
    settings: Settings = request.app.state.settings
    if authorization:
        scheme, _, token = authorization.partition(" ")
        if (
            settings.auth_mode in ("token", "both")
            and scheme.lower() == "bearer"
            and hmac.compare_digest(token.encode(), settings.owner_token.encode())
        ):
            return _owner_actor(request)
        raise ApiError(401, message="Authentication is required.")
    cookie = request.cookies.get(SESSION_COOKIE)
    if cookie and settings.auth_mode in ("google", "both"):
        with request.app.state.pool.connection() as conn:
            sess = auth_service.get_session(conn, settings, cookie)
        if sess is not None:
            if request.method not in SAFE_METHODS and not hmac.compare_digest(
                request.headers.get("x-csrf-token", "").encode(), sess.csrf_token.encode()
            ):
                raise ApiError(
                    403,
                    "csrf_failed",
                    "The request could not be verified. Reload the page and try again.",
                )
            request.state.session = sess
            return _owner_actor(request)
    raise ApiError(401, message="Authentication is required.")


CurrentActor = Annotated[Actor, Depends(get_actor)]


def require_stepup(request: Request, actor: CurrentActor) -> Actor:
    """For sensitive actions (approvals, 002F): the owner must have signed in with Google recently.

    The dev bearer token has no sign-in time, so it passes (it is rejected outright in the home environment).
    """
    settings: Settings = request.app.state.settings
    sess = getattr(request.state, "session", None)
    if sess is None:
        return actor
    if not sess.stepup_valid(settings, datetime.now(UTC)):
        raise ApiError(403, "stepup_required", "Please sign in again to confirm this action.")
    return actor


StepUpActor = Annotated[Actor, Depends(require_stepup)]
