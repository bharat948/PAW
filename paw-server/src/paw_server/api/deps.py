import hmac
from typing import Annotated

from fastapi import Depends, Header, Request

from paw_server.core.config import Settings
from paw_server.core.errors import ApiError
from paw_server.modules.ingestion.models import Actor


def get_actor(request: Request, authorization: Annotated[str | None, Header()] = None) -> Actor:
    """Single-owner bearer auth (D006). Resolves the owner's default workspace."""
    settings: Settings = request.app.state.settings
    scheme, _, token = (authorization or "").partition(" ")
    if scheme.lower() != "bearer" or not hmac.compare_digest(
        token.encode(), settings.owner_token.encode()
    ):
        raise ApiError(401, message="Authentication is required.")
    with request.app.state.pool.connection() as conn:
        row = conn.execute(
            "SELECT id FROM workspaces WHERE slug = 'default' AND owner_actor_id = 'owner' "
            "AND status = 'active'"
        ).fetchone()
        conn.commit()
    if row is None:
        raise ApiError(503, "workspace_unavailable", "The workspace is not available.")
    return Actor(id="owner", workspace_id=row["id"])


CurrentActor = Annotated[Actor, Depends(get_actor)]
