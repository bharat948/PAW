from fastapi import APIRouter, Request
from pydantic import BaseModel

from paw_server.api.deps import CurrentActor
from paw_server.core.config import Environment
from paw_server.core.errors import ApiError

router = APIRouter(prefix="/v1/dev", tags=["dev"])

_DEV_ENVS = (Environment.LOCAL, Environment.CI)


class ResetResponse(BaseModel):
    documents_deleted: int


@router.delete("/data", response_model=ResetResponse)
def reset_data(request: Request, actor: CurrentActor) -> ResetResponse:
    """Dev-only: wipe this workspace's documents and derived rows. Disabled outside local/ci."""
    if request.app.state.settings.env not in _DEV_ENVS:
        raise ApiError(403, message="Clearing data is only available in dev mode.")
    ws = actor.workspace_id
    with request.app.state.pool.connection() as conn:
        ver = "(SELECT id FROM document_versions WHERE workspace_id = %s)"
        conn.execute(
            "UPDATE documents SET current_version_id = NULL WHERE workspace_id = %s", (ws,)
        )
        conn.execute(f"DELETE FROM document_chunks WHERE version_id IN {ver}", (ws,))
        conn.execute(f"DELETE FROM ingestion_events WHERE version_id IN {ver}", (ws,))
        conn.execute("DELETE FROM ingestion_jobs WHERE workspace_id = %s", (ws,))
        conn.execute("DELETE FROM document_versions WHERE workspace_id = %s", (ws,))
        n = conn.execute("DELETE FROM documents WHERE workspace_id = %s", (ws,)).rowcount
        conn.execute("DELETE FROM retrieval_events WHERE workspace_id = %s", (ws,))
        conn.commit()
    return ResetResponse(documents_deleted=n)
