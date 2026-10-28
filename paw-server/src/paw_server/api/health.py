from fastapi import APIRouter, Request

from paw_server.core.errors import ApiError
from paw_server.db.readiness import check_ready

router = APIRouter(tags=["ops"])


@router.get("/health")
def health() -> dict[str, str]:
    """Liveness: process-oriented, never touches external services."""
    return {"status": "alive"}


@router.get("/ready")
def ready(request: Request) -> dict[str, str]:
    """Readiness: fails with 503 if PostgreSQL is down or migrations are incompatible."""
    result = check_ready(request.app.state.pool)
    if not result.ready:
        raise ApiError(
            503,
            code="not_ready",
            message="The service is not ready to accept requests.",
            details=[{"check": result.failed_check, "status": "failing"}],
        )
    return {"status": "ready"}
