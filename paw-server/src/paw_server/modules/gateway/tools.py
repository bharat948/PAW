"""Tool handlers. A handler gets the call's `ToolContext` (derived by the gateway) and the validated arguments, and
returns plain data; the gateway bounds and wraps it. Handlers never see or accept identity from the model."""

from typing import Any

from paw_server.modules.gateway.shaping import ContinuationStore


class ToolError(Exception):
    """A failure whose message is safe to show the model (unlike an unexpected exception, which is never shown)."""


def make_continue_handler(store: ContinuationStore) -> Any:
    def paw_continue(ctx: Any, args: dict[str, Any]) -> dict[str, Any]:
        part = store.read(ctx.run_id, args["cursor"])
        if part is None:
            raise ToolError("That cursor is unknown or expired.")
        return part

    return paw_continue
