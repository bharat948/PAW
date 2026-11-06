"""Single-use issue approvals. Unknown outcomes are reconciled, never blindly re-sent.

The runtime can request an approval; only an authenticated owner with recent sign-in
can consume it. The provider receives stored canonical arguments and a stable marker.
"""

import hashlib
import json
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

from paw_server.modules.connectors.github import GitHubError, repository_name
from paw_server.modules.connectors.github_service import GitHubService
from paw_server.modules.gateway.github_tools import github_specs
from paw_server.modules.gateway.registry import validate_arguments
from paw_server.modules.sessions import service as ledger


class ApprovalError(Exception):
    pass


def canonical(args: dict[str, Any]) -> tuple[dict[str, Any], str]:
    args = {**args, "repository": repository_name(args["repository"])}
    encoded = json.dumps(args, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return json.loads(encoded), hashlib.sha256(encoded.encode()).hexdigest()


def op_key(approval_id: UUID) -> str:
    return hashlib.sha256(str(approval_id).encode()).hexdigest()


class ApprovalService:
    def __init__(self, pool: ConnectionPool[Any], github: GitHubService) -> None:
        self.pool, self.github = pool, github

    def request(
        self, run: dict[str, Any], invocation_id: UUID, args: dict[str, Any]
    ) -> dict[str, Any]:
        spec = next(s for s in github_specs() if s.name == "github_create_issue")
        if validate_arguments(spec, args):
            raise ApprovalError("invalid_arguments")
        args, digest = canonical(args)
        live, _, _ = self.github._current(run["owner"])
        _, repo = self.github.connection_for_write(run["owner"], live.id, args["repository"])
        aid = uuid4()
        with self.pool.connection() as conn, conn.transaction():
            session = ledger.lock(conn, run["session_id"])
            attempt = conn.execute(
                "SELECT context_revision FROM gateway_tool_attempts WHERE id=%s", (invocation_id,)
            ).fetchone()
            current = conn.execute(
                "SELECT state FROM agent_runs WHERE id=%s FOR UPDATE", (run["id"],)
            ).fetchone()
            if (
                session["deleted_at"]
                or not attempt
                or attempt["context_revision"] != session["context_revision"]
                or not current
                or current["state"] not in ("DISPATCHING", "RUNNING")
            ):
                raise ApprovalError("context_changed")
            conn.execute(
                "INSERT INTO approval_requests(id,invocation_id,owner_actor,actor_id,session_id,run_id,context_revision,profile_version_id,tool,tool_version,args,args_hash,repository,repository_id,connection_id) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (
                    aid,
                    invocation_id,
                    run["owner"],
                    run["executing_actor_id"],
                    run["session_id"],
                    run["id"],
                    session["context_revision"],
                    run["profile_version_id"],
                    spec.name,
                    spec.version,
                    Jsonb(args),
                    digest,
                    args["repository"],
                    repo["id"],
                    live.id,
                ),
            )
            ledger.append(
                conn,
                run["session_id"],
                "platform_event",
                "owner",
                "Approval requested for GitHub issue creation.",
                run_id=run["id"],
                data={"approval_id": str(aid), "tool": spec.name, "state": "PENDING"},
            )
        return {
            "approval_id": str(aid),
            "state": "PENDING",
            "note": "No issue has been created. The owner must review and approve the exact payload in Integrations.",
        }

    def inbox(self, owner: str) -> list[dict[str, Any]]:
        with self.pool.connection() as conn:
            rows = conn.execute(
                "SELECT r.*,o.state AS operation_state,o.result,o.error_code FROM approval_requests r LEFT JOIN approval_operations o ON o.approval_id=r.id JOIN agent_sessions s ON s.id=r.session_id WHERE r.owner_actor=%s AND s.deleted_at IS NULL AND r.args <> '{}'::jsonb ORDER BY r.created_at DESC LIMIT 100",
                (owner,),
            ).fetchall()
            conn.commit()
        return [
            {
                **r,
                "state": "EXPIRED"
                if r["state"] == "PENDING" and r["expires_at"] <= datetime.now(UTC)
                else r["state"],
            }
            for r in rows
        ]

    def _binding(self, conn: Any, row: dict[str, Any]) -> bool:
        session = ledger.lock(conn, row["session_id"])
        policy = conn.execute(
            "SELECT c.policy FROM actors a JOIN actor_profile_versions v ON v.actor_id=a.id AND v.status='active' JOIN actor_compiled_profiles c ON c.profile_version_id=v.id WHERE a.id=%s AND a.status='active'",
            (row["actor_id"],),
        ).fetchone()
        tool = conn.execute(
            "SELECT enabled,version FROM gateway_tools WHERE name=%s", (row["tool"],)
        ).fetchone()
        scopes = policy["policy"].get("resource_scopes", {}).get(row["tool"]) if policy else None
        return bool(
            not session["deleted_at"]
            and session["context_revision"] == row["context_revision"]
            and policy
            and row["tool"] in policy["policy"].get("grants", [])
            and (scopes is None or row["repository"] in scopes)
            and tool
            and tool["enabled"]
            and tool["version"] == row["tool_version"]
            and canonical(row["args"])[1] == row["args_hash"]
        )

    def decide(self, owner: str, aid: UUID, digest: str, approve: bool) -> dict[str, Any]:
        # Serialize consumption before doing any provider write; a process crash after
        # this commit leaves STARTED, which only the read-only reconciler may resolve.
        with self.pool.connection() as conn, conn.transaction():
            initial = conn.execute(
                "SELECT session_id FROM approval_requests WHERE id=%s AND owner_actor=%s",
                (aid, owner),
            ).fetchone()
            if not initial:
                raise ApprovalError("not_found")
            ledger.lock(conn, initial["session_id"])
            row = conn.execute(
                "SELECT *,expires_at <= now() AS expired FROM approval_requests WHERE id=%s FOR UPDATE",
                (aid,),
            ).fetchone()
            if row["state"] != "PENDING":
                raise ApprovalError("already_used")
            if row["args_hash"] != digest:
                raise ApprovalError("payload_changed")
            if row["expired"]:
                state = "EXPIRED"
            elif not approve:
                state = "REJECTED"
            elif not self._binding(conn, row):
                state = "INVALIDATED"
            else:
                state = "APPROVED"
            conn.execute(
                "UPDATE approval_requests SET state=%s,decided_at=now() WHERE id=%s", (state, aid)
            )
            if state != "APPROVED":
                return {"id": str(aid), "state": state}
            conn.execute(
                "INSERT INTO approval_operations(approval_id,op_key,state) VALUES (%s,%s,'STARTED')",
                (aid, op_key(aid)),
            )
        try:
            installation, repo = self.github.connection_for_write(
                owner, row["connection_id"], row["repository"]
            )
            if repo["id"] != row["repository_id"]:
                raise GitHubError("out_of_scope")
        except GitHubError as exc:
            return self._outcome(row, "FAILED", {}, exc.code)
        try:
            result = self.github.provider.create_issue(installation, repo, row["args"], op_key(aid))
        except GitHubError:
            # Even a transport failure may occur after GitHub accepted the write.
            # There is never a second POST for this approval.
            return self._outcome(row, "UNKNOWN", {}, "provider_outcome_unknown")
        except Exception:
            return self._outcome(row, "UNKNOWN", {}, "provider_outcome_unknown")
        return self._outcome(row, "SUCCEEDED", result, None)

    def _outcome(
        self, row: dict[str, Any], state: str, result: dict[str, Any], error: str | None
    ) -> dict[str, Any]:
        with self.pool.connection() as conn, conn.transaction():
            session = ledger.lock(conn, row["session_id"])
            if session["deleted_at"] or session["context_revision"] != row["context_revision"]:
                result = {}
            current = conn.execute(
                "SELECT state,result FROM approval_operations WHERE approval_id=%s FOR UPDATE",
                (row["id"],),
            ).fetchone()
            if current is None:
                raise ApprovalError("not_found")
            if current["state"] in ("SUCCEEDED", "FAILED"):
                return {
                    "id": str(row["id"]),
                    "state": current["state"],
                    "result": current["result"],
                }
            conn.execute(
                "UPDATE approval_operations SET state=%s,result=%s,error_code=%s,updated_at=now() WHERE approval_id=%s",
                (state, Jsonb(result), error, row["id"]),
            )
            if not session["deleted_at"]:
                ledger.append(
                    conn,
                    row["session_id"],
                    "platform_event",
                    "owner",
                    f"GitHub issue operation: {state}.",
                    run_id=row["run_id"],
                    data={"approval_id": str(row["id"]), "state": state, "result": result},
                )
        return {"id": str(row["id"]), "state": state, "result": result}

    def reconcile(self, owner: str, aid: UUID) -> dict[str, Any]:
        with self.pool.connection() as conn:
            row = conn.execute(
                "SELECT r.*,o.state AS operation_state FROM approval_requests r JOIN approval_operations o ON o.approval_id=r.id WHERE r.id=%s AND r.owner_actor=%s",
                (aid, owner),
            ).fetchone()
            conn.commit()
        if not row:
            raise ApprovalError("not_found")
        if row["operation_state"] not in ("STARTED", "UNKNOWN"):
            raise ApprovalError("already_resolved")
        try:
            installation, repo = self.github.connection_for_write(
                owner, row["connection_id"], row["repository"]
            )
            if repo["id"] != row["repository_id"]:
                raise GitHubError("out_of_scope")
            result = self.github.provider.find_issue(installation, repo, op_key(aid))
        except GitHubError:
            result = None
        return self._outcome(
            row,
            "SUCCEEDED" if result else "UNKNOWN",
            result or {},
            None if result else "provider_outcome_unknown",
        )
