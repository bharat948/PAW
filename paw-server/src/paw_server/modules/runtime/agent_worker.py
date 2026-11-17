"""Drives queued agent runs, one at a time, through an AgentRuntime. Each event is committed to PostgreSQL before
anyone can read it. A run ends SUCCEEDED only when the runtime's terminal completion event was received."""

import logging
import time
from typing import Any
from uuid import UUID

from psycopg_pool import ConnectionPool

from paw_server.core.config import Settings
from paw_server.modules.costs import service as costs
from paw_server.modules.identity import service as identity
from paw_server.modules.runtime import lifecycle as lc
from paw_server.modules.runtime.protocol import AgentRuntime, RuntimeError_, RuntimeEvent
from paw_server.modules.sessions import assembler, checkpoints, privacy
from paw_server.modules.sessions.journal import Journal

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
        self._filters: dict[UUID, checkpoints.CheckpointStream] = {}
        with pool.connection() as conn:
            privacy.replay(conn, Journal(settings.deletion_journal_dir))
            self.runtime_id = lc.ensure_runtime(conn)
            identity.ensure_default_profile(conn)

    def recover(self) -> int:
        with self.pool.connection() as conn:
            runs = conn.execute(
                "SELECT * FROM agent_runs WHERE runtime_id = %s AND state IN "
                "('DISPATCHING','RUNNING','CANCELLING') ORDER BY queued_at, id",
                (self.runtime_id,),
            ).fetchall()
            conn.commit()
        for run in runs:
            rid, hid = run["id"], run["hermes_run_id"]
            if not hid:
                self._end(rid, "INTERRUPTED", error_code="dispatch_unknown")
                continue
            stream = checkpoints.CheckpointStream()
            with self.pool.connection() as conn:
                rows = conn.execute(
                    "SELECT payload, source_event_id FROM agent_run_events WHERE run_id = %s ORDER BY seq",
                    (rid,),
                ).fetchall()
                conn.commit()
            after = -1
            for row in rows:
                source = row["source_event_id"] or ""
                if source.startswith(hid + ":") and source.rsplit(":", 1)[-1].isdigit():
                    after = max(after, int(source.rsplit(":", 1)[-1]))
                if "raw" in row["payload"]:
                    stream.feed(row["payload"]["raw"])
            self._filters[rid] = stream
            deadline = time.monotonic() + self.settings.agent_recovery_deadline_s
            while True:
                try:
                    snapshot = self.runtime.get_run(hid)
                    if snapshot.status == "unknown":
                        self._end(rid, "INTERRUPTED", error_code="runtime_unknown")
                        break
                    if run["state"] == "DISPATCHING":
                        with self.pool.connection() as conn:
                            lc.move(conn, rid, "RUNNING", hermes_run_id=hid)
                    if run["state"] == "CANCELLING" and not snapshot.terminal:
                        self.runtime.stop_run(hid)
                    ended = False
                    for ev in self.runtime.stream_events(hid, after_seq=after):
                        if self._store(rid, ev):
                            ended = True
                            break
                    if not ended:
                        self._end(rid, "INTERRUPTED", error_code="no_terminal_event")
                    break
                except RuntimeError_ as exc:
                    if exc.retryable and time.monotonic() < deadline:
                        time.sleep(min(0.5, max(0, deadline - time.monotonic())))
                        continue
                    self._end(
                        rid,
                        "INTERRUPTED",
                        error_code="runtime_missing" if exc.code == "not_found" else exc.code,
                    )
                    break
        return len(runs)

    def reap(self) -> int:
        """End runs whose stop was never confirmed. Safe to call from another thread while a run is processed."""
        with self.pool.connection() as conn:
            return lc.expire_cancelling(conn, self.settings.agent_stop_deadline_s)

    def reconcile_costs(self) -> dict[str, Any]:
        """Release holds a crash left unsent, label unfinished sends uncertain, settle what the runtime's usage proves."""
        with self.pool.connection() as conn:
            with conn.transaction():
                result = costs.reconcile(conn)
        if result["released"] or result["uncertain"] or result["runs"].get("settled"):
            log.warning(
                "cost reconciliation",
                extra={
                    "released": [str(i) for i in result["released"]],
                    "uncertain": [str(i) for i in result["uncertain"]],
                    "late_evidence": result["runs"].get("settled", 0),
                },
            )
        return result

    def expire_deadlines(self) -> int:
        """Ask runs past their deadline to stop. The stop itself is best effort: if Hermes never confirms it, the
        stop deadline ends the run as INTERRUPTED with its partial output and `deadline_exceeded` as the reason."""
        with self.pool.connection() as conn:
            due = lc.expired_running(conn, self.runtime_id)
            n = 0
            for rid in due:
                outcome, hid = lc.request_cancel(conn, rid, reason="deadline_exceeded")
                if outcome == "stopping" and hid:
                    n += 1
                    try:
                        self.runtime.stop_run(hid)
                    except Exception:
                        log.warning("deadline stop not delivered", extra={"run_id": str(rid)})
        return n

    def run_once(self) -> bool:
        """Claim and fully process one run. False when there was nothing to do."""
        with self.pool.connection() as conn:
            privacy.replay(conn, Journal(self.settings.deletion_journal_dir))
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
        self._filters.pop(run_id, None)

    def _process(self, run: dict[str, Any]) -> None:
        run_id: UUID = run["id"]
        with self.pool.connection() as conn:
            profile = identity.get_active(conn, run["executing_actor_id"])
            if profile is None:  # no active identity: refuse to run rather than run unconfigured
                self._end(
                    run_id,
                    "FAILED",
                    error_code="no_active_profile",
                    payload={"reason": "no_active_profile"},
                )
                return
            lc.bind_profile(
                conn, run_id, profile_version_id=profile.version_id, version=profile.version,
                compiled_hash=profile.compiled.compiled_hash,
                max_run_seconds=profile.compiled.policy["max_run_seconds"],
            )  # fmt: skip
            assembly = assembler.assemble(
                conn,
                run["session_id"],
                run_id,
                instructions=profile.compiled.run_instructions,
                profile_hash=profile.compiled.compiled_hash,
                history_budget=self.settings.agent_history_tokens,
                tool_budget=self.settings.agent_tool_history_tokens,
                summary_budget=self.settings.agent_summary_tokens,
            )
            conn.commit()
        try:
            # Serialize the final privacy check and dispatch with durable deletion requests.
            journal = Journal(self.settings.deletion_journal_dir)
            with journal.guard(), self.pool.connection() as conn:
                for receipt in journal.receipts():
                    privacy.apply(conn, receipt)
                conn.commit()
                valid = assembler.valid(conn, assembly.meta)
                conn.commit()
                if not valid:
                    self._end(run_id, "INTERRUPTED", error_code="context_changed")
                    return
                handle = self.runtime.create_run(
                    run_id=str(run_id),
                    input=assembly.input,
                    instructions=assembly.instructions,
                    history=assembly.history,
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
        with self.pool.connection() as conn:
            redacted = conn.execute(
                "SELECT 1 FROM agent_run_redactions WHERE run_id=%s", (run_id,)
            ).fetchone()
            conn.commit()
            if redacted:
                lc.request_cancel(conn, run_id)
                self.runtime.stop_run(handle.hermes_run_id)
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
                redacted = conn.execute(
                    "SELECT 1 FROM agent_run_redactions WHERE run_id=%s", (run_id,)
                ).fetchone()
                conn.commit()
                payload = dict(ev.payload)
                if ev.type == "message.delta":
                    raw = str(payload.get("text", ""))
                    payload = {
                        "text": self._filters.setdefault(
                            run_id, checkpoints.CheckpointStream()
                        ).feed(raw),
                        "raw": raw,
                    }
                if redacted:
                    payload = {"redacted": True}
                lc.append_event(conn, run_id, ev.type, payload, ev.source_event_id)
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
        self._filters.pop(run_id, None)
        return True
