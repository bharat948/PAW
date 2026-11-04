"""HTTP adapter for the Hermès API server (pinned build c7da41dd). The only module that knows Hermès's wire format.

Rules it enforces:
- the PAW run id is the Idempotency-Key, so a retried create can never start a second run;
- events are deduplicated on `seq`; a gap in `seq` is a protocol fault, never papered over;
- a stream that ends without a terminal event is reconnected, then resolved from the run status;
  if even that fails the caller gets `stream_lost` (the run becomes INTERRUPTED, never SUCCEEDED).
"""

import time
from collections.abc import Callable, Iterator
from typing import Any

import httpx

from paw_server.modules.runtime.normaliser import STATUS_TERMINAL, normalise, usage_dict, usage_from
from paw_server.modules.runtime.protocol import RunHandle, RunSnapshot, RuntimeError_, RuntimeEvent
from paw_server.modules.runtime.sse import parse_frames

KNOWN_STATUS = {
    "queued", "started", "running", "waiting_for_approval", "stopping",
    "completed", "failed", "cancelled", "interrupted",
}  # fmt: skip
REQUIRED_FEATURES = ("run_submission", "run_status", "run_events_sse", "run_stop")


class HermesAdapter:
    def __init__(
        self,
        base_url: str,
        key: str,
        *,
        http: httpx.Client | None = None,
        timeout_s: float = 15.0,
        stream_read_timeout_s: float = 90.0,
        reconnects: int = 2,
        poll_interval_s: float = 0.5,
        poll_deadline_s: float = 30.0,
        create_retries: int = 2,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._http = http or httpx.Client(base_url=base_url.rstrip("/"), timeout=timeout_s)
        self._key = key
        self._timeout_s, self._read_s = timeout_s, stream_read_timeout_s
        self._reconnects, self._poll_s, self._poll_deadline = (
            reconnects,
            poll_interval_s,
            poll_deadline_s,
        )
        self._create_retries, self._sleep = create_retries, sleep

    # ---- plumbing ------------------------------------------------------------------------------------------
    def _headers(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._key}", **(extra or {})}

    @staticmethod
    def _check(r: httpx.Response) -> None:
        if r.status_code < 400:
            return
        if r.status_code == 404:
            raise RuntimeError_("not_found", "http 404")
        if r.status_code in (401, 403):
            raise RuntimeError_("auth", f"http {r.status_code}")
        if r.status_code == 429 or r.status_code >= 500:
            raise RuntimeError_("unavailable", f"http {r.status_code}")
        raise RuntimeError_(
            "rejected", f"http {r.status_code}"
        )  # upstream body deliberately not copied

    def _call(self, method: str, path: str, **kw: Any) -> httpx.Response:
        try:
            r = self._http.request(
                method, path, headers=self._headers(kw.pop("headers", None)), **kw
            )
        except httpx.TimeoutException as exc:
            raise RuntimeError_("timeout", path) from exc
        except httpx.HTTPError as exc:
            raise RuntimeError_("unavailable", type(exc).__name__) from exc
        self._check(r)
        return r

    @staticmethod
    def _json(r: httpx.Response) -> dict[str, Any]:
        try:
            body = r.json()
        except ValueError as exc:
            raise RuntimeError_("protocol", "response was not JSON") from exc
        if not isinstance(body, dict):
            raise RuntimeError_("protocol", "response was not an object")
        return body

    # ---- AgentRuntime --------------------------------------------------------------------------------------
    def create_run(
        self,
        *,
        run_id: str,
        input: str,
        instructions: str | None = None,
        history: list[dict[str, Any]] | None = None,
    ) -> RunHandle:
        body: dict[str, Any] = {"input": input}
        if instructions is not None:
            body["instructions"] = instructions
        if history is not None:
            body["conversation_history"] = history
        last: RuntimeError_ | None = None
        for attempt in range(self._create_retries + 1):
            try:
                r = self._call("POST", "/v1/runs", json=body, headers={"Idempotency-Key": run_id})
                data = self._json(r)
                hid = data.get("run_id")
                if not isinstance(hid, str) or not hid:
                    raise RuntimeError_("protocol", "no run_id in response")
                replayed = (
                    bool(data.get("replayed")) or r.headers.get("Idempotency-Replayed") == "true"
                )
                return RunHandle(hid, replayed)
            except RuntimeError_ as exc:
                if not exc.retryable:
                    raise
                last = exc  # safe to retry: same Idempotency-Key returns the same run if the first one landed
                if attempt < self._create_retries:
                    self._sleep(min(2.0, 0.25 * 2**attempt))
        assert last is not None
        raise last

    def get_run(self, hermes_run_id: str) -> RunSnapshot:
        data = self._json(self._call("GET", f"/v1/runs/{hermes_run_id}"))
        status = str(data.get("status", ""))
        controls = data.get("pending_controls")
        return RunSnapshot(
            status=status if status in KNOWN_STATUS else "unknown",
            output=str(data.get("output") or ""),
            usage=usage_from(data.get("usage")),
            pending_controls=controls if isinstance(controls, list) else [],
        )

    def stop_run(self, hermes_run_id: str) -> None:
        try:
            self._call("POST", f"/v1/runs/{hermes_run_id}/stop")
        except RuntimeError_ as exc:
            if exc.code == "rejected" and exc.detail.endswith("409"):
                return  # already finished
            raise

    def stream_events(self, hermes_run_id: str, after_seq: int = -1) -> Iterator[RuntimeEvent]:
        last = after_seq
        for attempt in range(self._reconnects + 1):
            try:
                timeout = httpx.Timeout(self._timeout_s, read=self._read_s)
                with self._http.stream(
                    "GET",
                    f"/v1/runs/{hermes_run_id}/events",
                    headers=self._headers(),
                    timeout=timeout,
                ) as r:
                    self._check(r)
                    for frame in parse_frames(r.iter_lines()):
                        ev = normalise(frame, hermes_run_id)
                        if ev is None:
                            continue
                        seq = int(ev.source_event_id.rsplit(":", 1)[1])
                        if seq <= last:
                            if ev.terminal:
                                return  # the caller already has this terminal event: never invent a second one
                            continue  # replayed after a reconnect
                        if seq != last + 1:
                            raise RuntimeError_("protocol", f"event gap {last} -> {seq}")
                        last = seq
                        yield ev
                        if ev.terminal:
                            return
            except RuntimeError_ as exc:
                if not exc.retryable:
                    raise
            except httpx.HTTPError:
                pass  # connection dropped or timed out: reconnect, then fall back to polling
            if attempt < self._reconnects:
                self._sleep(min(2.0, 0.25 * 2**attempt))
        yield from self._resolve_from_status(hermes_run_id, last)

    def _resolve_from_status(self, hermes_run_id: str, last: int) -> Iterator[RuntimeEvent]:
        """The stream is gone. Ask the run itself; if it is not finished by the deadline, say so."""
        waited = 0.0
        while True:
            try:
                snap = self.get_run(hermes_run_id)
            except RuntimeError_ as exc:
                if not exc.retryable:
                    raise
                snap = None
            if snap is not None and snap.terminal:
                kind = STATUS_TERMINAL[snap.status]
                payload: dict[str, Any] = {"synthesized": True}
                if kind in ("run.completed", "run.interrupted"):
                    payload.update(output=snap.output, usage=usage_dict(snap.usage))
                yield RuntimeEvent(kind, payload, f"{hermes_run_id}:{last + 1}")
                return
            if waited >= self._poll_deadline:
                raise RuntimeError_("stream_lost", "run state could not be established")
            self._sleep(self._poll_s)
            waited += self._poll_s

    def missing_capabilities(self) -> list[str]:
        try:
            caps = self._json(self._call("GET", "/v1/capabilities"))
        except RuntimeError_ as exc:
            return [f"unreachable:{exc.code}"]
        missing: list[str] = []
        auth = caps.get("auth")
        if not (isinstance(auth, dict) and auth.get("required") is True):
            missing.append("auth_required")
        features = caps.get("features")
        features = features if isinstance(features, dict) else {}
        missing += [f for f in REQUIRED_FEATURES if features.get(f) is not True]
        idem = features.get("runs_idempotency")
        if not (isinstance(idem, dict) and idem.get("supported") is True):
            missing.append("runs_idempotency")
        return missing

    def healthy(self) -> bool:
        try:
            return self._http.get("/health").status_code == 200
        except httpx.HTTPError:
            return False
