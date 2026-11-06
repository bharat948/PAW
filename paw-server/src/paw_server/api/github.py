"""Owner-only GitHub App installation flow and signed lifecycle notifications."""

import hashlib
import hmac
import json
from datetime import UTC, datetime
from typing import Annotated, Any
from urllib.parse import urlencode
from uuid import UUID

from fastapi import APIRouter, Query, Request, Response
from fastapi.responses import RedirectResponse

from paw_server.api.deps import CurrentActor, StepUpActor
from paw_server.api.integrations import _back, _session
from paw_server.core.errors import ApiError
from paw_server.modules.auth import service as auth
from paw_server.modules.connectors.github import GitHubError
from paw_server.modules.connectors.github_service import GitHubService

router = APIRouter(prefix="/v1/integrations/github", tags=["integrations"])
PURPOSE = "connect_github"


def service(request: Request) -> GitHubService:
    got: GitHubService | None = request.app.state.github
    if got is None:
        raise ApiError(404)
    return got


def failure(exc: GitHubError) -> ApiError:
    status = 503 if exc.code in ("unavailable", "rate_limited", "key_unavailable") else 409
    return ApiError(
        status,
        exc.code,
        {
            "not_installed": "Install PAW Space on the selected repositories first.",
            "no_repositories": "Select at least one repository in the GitHub App installation.",
            "rate_limited": "GitHub's rate limit was reached. Try again later.",
            "not_connected": "GitHub is not connected.",
            "revoked": "GitHub access was revoked. Reconnect it.",
        }.get(exc.code, "GitHub could not complete this request."),
    )


@router.get("/connect")
def start(
    request: Request, return_to: Annotated[str, Query(max_length=200)] = "/#/integrations"
) -> RedirectResponse:
    service(request)
    st = request.app.state
    st.auth_limiter.check(f"github-connect:{request.client.host if request.client else '-'}")
    sess = _session(request)
    if sess is None:
        raise ApiError(401)
    if not sess.stepup_valid(st.settings, datetime.now(UTC)):
        raise ApiError(403, "stepup_required", "Please sign in again to connect GitHub.")
    with st.pool.connection() as conn:
        state, _ = auth.start_attempt(
            conn, purpose=PURPOSE, return_to=return_to, session_id=sess.id, verifier=""
        )
    return RedirectResponse(
        f"https://github.com/apps/{st.settings.github_app_slug}/installations/new?{urlencode({'state': state})}",
        302,
    )


@router.get("/installed")
def installed(
    request: Request,
    installation_id: Annotated[int | None, Query(gt=0)] = None,
    state: str | None = None,
) -> RedirectResponse:
    svc = service(request)
    # A direct GitHub setup redirect never attaches an owner connection by itself.
    if not state or installation_id is None:
        return _back(request, error="github_finish")
    with request.app.state.pool.connection() as conn:
        attempt = auth.consume_attempt(conn, state)
    sess = _session(request)
    if attempt is None or attempt.purpose != PURPOSE:
        return _back(request, error="expired")
    if (
        sess is None
        or sess.id != attempt.session_id
        or not sess.stepup_valid(request.app.state.settings, datetime.now(UTC))
    ):
        return _back(request, error="github_failed")
    try:
        svc.connect("owner", installation_id)
    except GitHubError:
        return _back(request, error="github_failed")
    return _back(request, attempt.return_to)


@router.post("/connect")
def finish(request: Request, actor: StepUpActor) -> dict[str, Any]:
    """Finish a manually installed App, including local bearer-token development.

    No callback identity is accepted: resolve the operator-bound account's installation
    from GitHub using the App's own credential.
    """
    try:
        return service(request).connect(actor.id)
    except GitHubError as exc:
        raise failure(exc) from None


@router.get("/repositories")
def repositories(request: Request, actor: CurrentActor) -> dict[str, Any]:
    try:
        return service(request).read(actor.id, "github_list_repos", {})
    except GitHubError as exc:
        raise failure(exc) from None


@router.delete("", status_code=204)
def disconnect(request: Request, actor: CurrentActor) -> Response:
    service(request).disconnect(actor.id)
    return Response(status_code=204)


@router.post("/webhook", status_code=204)
async def webhook(request: Request) -> Response:
    st = request.app.state
    if not st.settings.github_webhook_secret:
        raise ApiError(404)
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > 1024 * 1024:
            raise ApiError(413)
    signature = (
        "sha256="
        + hmac.new(st.settings.github_webhook_secret.encode(), body, hashlib.sha256).hexdigest()
    )
    if not hmac.compare_digest(signature, request.headers.get("x-hub-signature-256", "")):
        raise ApiError(401)
    try:
        delivery = str(UUID(request.headers.get("x-github-delivery", "")))
        payload = json.loads(body)
        if not isinstance(payload, dict):
            raise ValueError
    except (ValueError, TypeError):
        raise ApiError(400) from None
    event = request.headers.get("x-github-event")
    if event not in ("installation", "installation_repositories"):
        return Response(status_code=204)
    installation = payload.get("installation")
    if not isinstance(installation, dict) or type(installation.get("id")) is not int:
        raise ApiError(400)
    # Only deletion/suspension revokes the local connection. Repository removals are
    # checked live by every call, and additions never widen the saved snapshot.
    revoke = event == "installation" and payload.get("action") in ("deleted", "suspend")
    with st.pool.connection() as conn, conn.transaction():
        inserted = conn.execute(
            "INSERT INTO github_webhook_receipts(delivery_id) VALUES (%s) ON CONFLICT DO NOTHING RETURNING delivery_id",
            (delivery,),
        ).fetchone()
        if inserted and revoke:
            rows = conn.execute(
                "SELECT c.id, s.ciphertext FROM connector_connections c JOIN connector_secrets s ON s.connection_id=c.id WHERE c.provider='github' AND c.status <> 'revoked' FOR UPDATE OF c"
            ).fetchall()
            from paw_server.modules.connectors.store import unseal

            for row in rows:
                data = unseal(st.settings.connector_key, row["ciphertext"])
                if data and data.get("installation_id") == installation["id"]:
                    conn.execute(
                        "UPDATE connector_connections SET status='revoked',revoked_at=now(),updated_at=now() WHERE id=%s",
                        (row["id"],),
                    )
                    conn.execute(
                        "DELETE FROM connector_secrets WHERE connection_id=%s", (row["id"],)
                    )
    if st.github:
        st.github.provider.clear_tokens()
    return Response(status_code=204)
