"""Drives queued agent runs, one at a time, through an AgentRuntime. Each event is committed to PostgreSQL before
anyone can read it. A run ends SUCCEEDED only when the runtime's terminal completion event was received."""

import logging
from typing import Any
from uuid import UUID

from psycopg_pool import ConnectionPool

from paw_server.core.config import Settings
from paw_server.modules.runtime import lifecycle as lc
from paw_server.modules.runtime.protocol import AgentRuntime, RuntimeError_, RuntimeEvent

log = logging.getLogger("paw.agent")


def _usage_fields(payload: dict[str, Any]) -> dict[str, Any]:
    u = payload.get("usage")
    if not isinstance(u, dict):
        return {}
    return {
        "tokens_in": u.get("input_tokens"),
        "tokens_out": u.get("output_tokens"),
        "cache_read": u.get("cache_read"),
        "cache_write": u.get("cache_write"),
    }


class AgentWorker:
    def __init__(
        self, pool: ConnectionPool[Any], runtime: AgentRuntime, settings: Settings
    ) -> None:
        self.pool, self.runtime, self.settings = pool, runtime, settings
        with pool.connection() as conn:
            self.runtime_id = lc.ensure_runtime(conn)

    def recover(self) -> int:
        with self.pool.connection() as conn:
            return lc.recover_interrupted(conn)

    def reap(self) -> int:
        """End runs whose stop was never confirmed. Safe to call from another thread while a run is processed."""
        with self.pool.connection() as conn:
            return lc.expire_cancelling(conn, self.settings.agent_stop_deadline_s)

    def run_once(self) -> bool:
        """Claim and fully process one run. False when there was nothing to do."""
        with self.pool.connection() as conn:
            run = lc.claim_next(conn, self.runtime_id)
        if run is None:
            return False
        try:
            self._process(run)
        except Exception:  # never leave a claimed run (and its lease) dangling
            log.exception("agent run crashed", extra={"run_id": str(run["id"])})
            self._end(
                run["id"],
                "INTERRUPTED",
                error_code="worker_error",
                payload={"reason": "worker_error"},
            )
        return True

    # ---- internals -------------------------------------------------------------------------------------------
    def _end(self, run_id: UUID, state: str, *, error_code: str | None = None, **kw: Any) -> None:
        with self.pool.connection() as conn:
            fields = dict(kw.pop("fields", {}))
            if error_code:
                fields["error_code"] = error_code
            if "output" not in fields and state in ("INTERRUPTED", "FAILED"):
                fields["output"] = lc.partial_output(conn, run_id)
            lc.finish(conn, run_id, state, **kw, **fields)

    def _process(self, run: dict[str, Any]) -> None:
        run_id: UUID = run["id"]
        with self.pool.connection() as conn:
            history = lc.history_for(conn, run["session_id"], run_id)
        try:
            handle = self.runtime.create_run(
                run_id=str(run_id),
                input=run["user_message"],
                instructions=lc.INSTRUCTIONS,
                history=history,
            )
        except RuntimeError_ as exc:
            # A timeout or outage may mean the request landed: we cannot know, so never call it a clean failure.
            unknown = exc.code in ("unavailable", "timeout")
            self._end(
                run_id,
                "INTERRUPTED" if unknown else "FAILED",
                error_code="dispatch_unknown" if unknown else f"dispatch_{exc.code}",
                payload={"reason": exc.code},
            )
            return
        with self.pool.connection() as conn:
            if not lc.move(conn, run_id, "RUNNING", hermes_run_id=handle.hermes_run_id):
                self._end(run_id, "INTERRUPTED", error_code="bad_state")
                return
        try:
            for ev in self.runtime.stream_events(handle.hermes_run_id):
                if self._store(run_id, ev):
                    return
        except RuntimeError_ as exc:
            self._end(run_id, "INTERRUPTED", error_code=exc.code, payload={"reason": exc.code})
            return
        # the generator contract is "terminal event or an error"; anything else is a runtime bug
        self._end(run_id, "INTERRUPTED", error_code="no_terminal_event")

    def _store(self, run_id: UUID, ev: RuntimeEvent) -> bool:
        """Persist one event. True when it was the terminal one (the run is then finished)."""
        if not ev.terminal:
            with self.pool.connection() as conn:
                # ended elsewhere (stop deadline): nothing may follow a terminal event
                if lc.is_terminal(conn, run_id):
                    return True
                lc.append_event(conn, run_id, ev.type, ev.payload, ev.source_event_id)
                lc.heartbeat(conn, run_id)
            return False
        to = lc.STATE_FOR_TERMINAL_EVENT[ev.type]
        fields: dict[str, Any] = {}
        if ev.type in ("run.completed", "run.interrupted"):
            fields["output"] = str(ev.payload.get("output") or "")
            fields.update({k: v for k, v in _usage_fields(ev.payload).items() if v is not None})
        if ev.type == "run.failed":
            fields["error_code"] = "runtime_error"
        if ev.type == "run.interrupted":
            fields["error_code"] = str(ev.payload.get("reason") or "interrupted")
        with self.pool.connection() as conn:
            lc.finish(
                conn, run_id, to, event_type=ev.type, payload=ev.payload,
                source_event_id=ev.source_event_id, **fields,
            )  # fmt: skip
        return True
