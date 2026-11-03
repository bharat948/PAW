"""Admin Hermes's identity: view versions, draft a new one, activate it. Owner only, never reachable by the agent."""

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Request
from pydantic import BaseModel

from paw_server.api.deps import CurrentActor, StepUpActor
from paw_server.core.errors import ApiError
from paw_server.modules.gateway import attestation
from paw_server.modules.identity import service as svc
from paw_server.modules.identity.spec import ActorProfileSpec
from paw_server.modules.identity.tools import KNOWN_TOOLS

router = APIRouter(prefix="/v1/agent/profile", tags=["agent-profile"])


class DraftIn(BaseModel):
    spec: ActorProfileSpec


class ActivateIn(BaseModel):
    confirm: bool = False


def _iso(v: Any) -> str | None:
    return v.isoformat() if v else None


def _version_out(v: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(v["id"]),
        "version": v["version"],
        "status": v["status"],
        "spec": v["spec"],
        "compiled_hash": v.get("compiled_hash"),
        "created_by": v["created_by"],
        "created_at": _iso(v["created_at"]),
        "activated_at": _iso(v["activated_at"]),
    }


@router.get("")
def get_profile(request: Request, actor: CurrentActor) -> dict[str, Any]:
    with request.app.state.pool.connection() as conn:
        svc.ensure_default_profile(
            conn
        )  # a fresh install shows version 1 without waiting for the worker
        versions = svc.list_versions(conn, svc.ADMIN)
    out = [_version_out(v) for v in versions]
    with request.app.state.pool.connection() as conn:
        active = svc.get_active(conn, svc.ADMIN)
        status = attestation.runtime_status(
            conn,
            request.app.state.settings.gateway_runtime,
            active.compiled.config_fragment if active else None,
        )
    return {
        "runtime_config": status.state,  # matches | mismatch | unknown
        "restart_required": status.state == "mismatch",
        "actor_id": svc.ADMIN,
        "active": next((v for v in out if v["status"] == "active"), None),
        "versions": out,
        "known_tools": sorted(KNOWN_TOOLS),
    }


@router.post("/versions", status_code=201)
def create_draft(request: Request, actor: CurrentActor, body: DraftIn) -> dict[str, Any]:
    unknown = sorted(set(body.spec.tools.grants) - KNOWN_TOOLS)
    if unknown:
        raise ApiError(422, "unknown_tool", f"Unknown tools: {', '.join(unknown)}.")
    with request.app.state.pool.connection() as conn:
        svc.ensure_default_profile(conn)
        row = svc.create_version(conn, svc.ADMIN, body.spec, created_by=actor.id)
        active = svc.get_active(conn, svc.ADMIN)
    changes = (
        svc.authority_changes(active.spec, body.spec) if active else ["tools", "budgets", "runtime"]
    )
    return {
        "id": str(row["id"]),
        "version": row["version"],
        "status": row["status"],
        "changes": changes,
        "requires_confirmation": bool(changes),
    }


@router.post("/versions/{version_id}/activate")
def activate(
    request: Request, actor: StepUpActor, version_id: UUID, body: ActivateIn
) -> dict[str, Any]:
    """Changing tools, budgets or runtime limits needs `confirm: true` and a recent Google sign-in."""
    with request.app.state.pool.connection() as conn:
        versions = {v["id"]: v for v in svc.list_versions(conn, svc.ADMIN)}
        target = versions.get(version_id)
        if target is None:
            raise ApiError(404)
        active = svc.get_active(conn, svc.ADMIN)
        new_spec = ActorProfileSpec.model_validate(target["spec"])
        changes = svc.authority_changes(active.spec, new_spec) if active else []
        if changes and not body.confirm:
            raise ApiError(
                409,
                "confirmation_required",
                f"This changes {', '.join(changes)}. Confirm to activate it.",
            )
        try:
            now_active = svc.activate(conn, svc.ADMIN, version_id, by=actor.id)
        except svc.ProfileError as exc:
            raise ApiError(
                409, "not_activatable", "Only a draft version can be activated."
            ) from exc
    before = active.compiled.config_fragment if active else None
    restart = before is not None and before != now_active.compiled.config_fragment
    return {
        "version": now_active.version,
        "compiled_hash": now_active.compiled.compiled_hash,
        "changes": changes,
        "restart_required": restart,  # the model or tool list changed: restart Hermes to apply it
    }
