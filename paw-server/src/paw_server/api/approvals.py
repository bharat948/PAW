"""Owner review and exact-payload approval endpoints; never callable by the runtime."""

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict, Field

from paw_server.api.deps import CurrentActor, StepUpActor
from paw_server.api.github import service as github
from paw_server.core.errors import ApiError
from paw_server.modules.approvals.service import ApprovalError, ApprovalService

router = APIRouter(prefix="/v1/approvals", tags=["approvals"])


class DecisionBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    args_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    approve: bool


def service(request: Request) -> ApprovalService:
    return ApprovalService(request.app.state.pool, github(request))


@router.get("")
def inbox(request: Request, actor: CurrentActor) -> dict[str, Any]:
    return {"approvals": service(request).inbox(actor.id)}


@router.post("/{approval_id}/decide")
def decide(
    approval_id: UUID, body: DecisionBody, request: Request, actor: StepUpActor
) -> dict[str, Any]:
    try:
        return service(request).decide(actor.id, approval_id, body.args_hash, body.approve)
    except ApprovalError as exc:
        raise ApiError(
            404 if str(exc) == "not_found" else 409,
            str(exc),
            "This approval cannot be used. Reload the approval inbox.",
        ) from None


@router.post("/{approval_id}/reconcile")
def reconcile(approval_id: UUID, request: Request, actor: StepUpActor) -> dict[str, Any]:
    try:
        return service(request).reconcile(actor.id, approval_id)
    except ApprovalError as exc:
        raise ApiError(409, str(exc), "This operation cannot be checked.") from None
