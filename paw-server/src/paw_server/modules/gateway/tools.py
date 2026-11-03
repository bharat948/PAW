"""Tool handlers. A handler gets the call's `ToolContext` (derived by the gateway) and the validated arguments, and
returns plain data; the gateway bounds and wraps it. Handlers never see or accept identity from the model."""

from dataclasses import dataclass, replace
from typing import Any
from uuid import UUID

from psycopg_pool import ConnectionPool

from paw_server.core.config import Settings
from paw_server.db.readiness import check_ready
from paw_server.modules.connectors.calendar_service import CalendarError, CalendarService
from paw_server.modules.gateway.registry import ToolRegistry, ToolSpec
from paw_server.modules.gateway.shaping import ContinuationStore
from paw_server.modules.ingestion.embedding import Embedder
from paw_server.modules.ingestion.models import Actor
from paw_server.modules.retrieval import service as retrieval
from paw_server.modules.retrieval.excerpt import make_excerpt, query_terms


class ToolError(Exception):
    """A failure whose message is safe to show the model (unlike an unexpected exception, which is never shown)."""


@dataclass
class ToolDeps:
    pool: ConnectionPool[Any]
    settings: Settings
    embedder: Embedder | None
    calendar: CalendarService | None
    continuations: ContinuationStore


def make_continue_handler(store: ContinuationStore) -> Any:
    def paw_continue(ctx: Any, args: dict[str, Any]) -> dict[str, Any]:
        part = store.read(ctx.run_id, args["cursor"])
        if part is None:
            raise ToolError("That cursor is unknown or expired.")
        return part

    return paw_continue


def _continue(deps: ToolDeps) -> Any:
    return make_continue_handler(deps.continuations)


def _system_status(deps: ToolDeps) -> Any:
    def paw_system_status(ctx: Any, args: dict[str, Any]) -> dict[str, Any]:
        ready = check_ready(deps.pool)
        with deps.pool.connection() as conn:
            runs = conn.execute(
                "SELECT state, count(*) AS n FROM agent_runs WHERE queued_at > now() - interval '24 hours' "
                "GROUP BY state"
            ).fetchall()
            integrations = conn.execute(
                "SELECT provider, status FROM connector_connections WHERE owner_actor = %s AND status <> 'revoked'",
                (ctx.requester,),
            ).fetchall()
            conn.commit()
        return {
            "database": "ok" if ready.ready else f"failing ({ready.failed_check})",
            "agent_runs_last_24h": {r["state"]: r["n"] for r in runs},
            "integrations": {i["provider"]: i["status"] for i in integrations},
        }

    return paw_system_status


def _run_status(deps: ToolDeps) -> Any:
    def paw_run_status(ctx: Any, args: dict[str, Any]) -> dict[str, Any]:
        which = args.get("which", "previous")
        with deps.pool.connection() as conn:
            if which == "current":
                row = conn.execute(
                    "SELECT * FROM agent_runs WHERE id = %s AND session_id = %s",
                    (ctx.run_id, ctx.session_id),
                ).fetchone()
            else:  # the most recent earlier run in this conversation
                row = conn.execute(
                    "SELECT * FROM agent_runs WHERE session_id = %s AND id <> %s AND queued_at < "
                    "(SELECT queued_at FROM agent_runs WHERE id = %s) ORDER BY queued_at DESC LIMIT 1",
                    (ctx.session_id, ctx.run_id, ctx.run_id),
                ).fetchone()
            conn.commit()
        if row is None:
            return {"found": False, "note": "There is no earlier run in this conversation."}
        return {
            "found": True,
            "which": which,
            "state": row["state"],
            "error_code": row["error_code"],
            "message_preview": row["user_message"][:120],
            "queued_at": row["queued_at"].isoformat(),
            "finished_at": row["finished_at"].isoformat() if row["finished_at"] else None,
            "tokens_in": row["tokens_in"],
            "tokens_out": row["tokens_out"],
        }

    return paw_run_status


def _docs_search(deps: ToolDeps) -> Any:
    def docs_search(ctx: Any, args: dict[str, Any]) -> dict[str, Any]:
        if deps.embedder is None:
            raise ToolError("Document search is not available.")
        query = " ".join(str(args["query"]).split())
        out = retrieval.search(
            deps.pool, deps.embedder, deps.settings, Actor(id=ctx.requester, workspace_id=ctx.workspace_id),
            query=query, top_k=int(args.get("top_k", 5)), document_ids=None, fusion_version=retrieval.DEFAULT_FUSION,
        )  # fmt: skip
        terms = query_terms(query)
        results = []
        for h in out.hits:
            r = h.row
            excerpt, start = make_excerpt(r["text"], terms, deps.settings.excerpt_chars)
            results.append(
                {
                    "chunk_id": str(h.chunk_id),
                    "document": r["display_name"],
                    "page": r["page_number"],
                    "section": r["section"],
                    "excerpt": excerpt,
                    "excerpt_complete": start == 0 and len(excerpt) == len(r["text"]),
                }
            )
        return {"query": query, "results": results, "degraded": out.degraded}

    return docs_search


def _docs_get_source(deps: ToolDeps) -> Any:
    def docs_get_source(ctx: Any, args: dict[str, Any]) -> dict[str, Any]:
        try:
            chunk_id = UUID(str(args["chunk_id"]))
        except ValueError as exc:
            raise ToolError("chunk_id is not a valid id.") from exc
        with deps.pool.connection() as conn:
            r = retrieval.fetch_chunk(
                conn, ctx.workspace_id, chunk_id
            )  # scoped to the run's workspace, in SQL
        if r is None:
            raise ToolError("No such passage.")  # the same answer for missing and foreign chunks
        return {
            "chunk_id": str(r["id"]),
            "document": r["display_name"],
            "media_type": r["media_type"],
            "page": r["page_number"],
            "section": r["section"],
            "is_current_version": r["is_current"],
            "text": r["text"],
        }

    return docs_get_source


def _calendar_events(deps: ToolDeps) -> Any:
    def calendar_list_events(ctx: Any, args: dict[str, Any]) -> dict[str, Any]:
        if deps.calendar is None:
            raise ToolError("Google Calendar is not available on this server.")
        try:
            got = deps.calendar.list_events(
                ctx.requester, days=int(args.get("days", 7)), limit=int(args.get("limit", 20))
            )
        except CalendarError as exc:
            raise ToolError(
                {
                    "not_connected": "Google Calendar is not connected.",
                    "needs_reauth": "Google Calendar needs to be reconnected by the owner.",
                }.get(exc.code, "Google Calendar is temporarily unavailable.")
            ) from exc
        return {"calendar_owner": got.account_email, "events": got.events, "count": len(got.events)}

    return calendar_list_events


def attach_handlers(registry: ToolRegistry, deps: ToolDeps) -> ToolRegistry:
    """The registry with every built-in tool bound to its handler."""
    factories = {
        "paw_continue": _continue, "paw_system_status": _system_status, "paw_run_status": _run_status,
        "docs_search": _docs_search, "docs_get_source": _docs_get_source, "calendar_list_events": _calendar_events,
    }  # fmt: skip
    specs: list[ToolSpec] = []
    for name in registry.names():
        spec = registry.get(name)
        assert spec is not None
        specs.append(replace(spec, handler=factories[name](deps)) if name in factories else spec)
    return ToolRegistry(specs)
