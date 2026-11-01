"""Minimal HTTP client for the Hermes API server, used only by the S0 contract tests.

Everything here follows the DOCUMENTED surface (docs checked 2026-11-02). Fields the docs do not specify are read
defensively and recorded by the capture helpers, because spike S0 exists to find out what is really sent.
"""

import json
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

import httpx

TERMINAL_EVENTS = {"run.completed", "run.failed", "run.cancelled", "run.interrupted"}
TERMINAL_STATUSES = {"completed", "failed", "cancelled", "interrupted"}


@dataclass
class SseFrame:
    event: str
    data: Any
    raw: str
    comments: list[str] = field(default_factory=list)


def parse_sse(lines: Iterator[str]) -> Iterator[SseFrame]:
    """Parse text/event-stream lines. Comment lines (': keepalive') are attached to the next frame."""
    event, data, raw, comments = "message", [], [], []
    for line in lines:
        if line == "":
            if data:  # per the SSE spec a block without data: lines dispatches nothing
                text = "\n".join(data)
                try:
                    payload: Any = json.loads(text)
                except ValueError:
                    payload = text
                name = event
                if event == "message" and isinstance(payload, dict):
                    # real Hermes (c7da41d) sends unnamed frames: {"event": "run.completed", "seq": 2, ...}
                    name = str(payload.get("event") or event)
                yield SseFrame(name, payload, "\n".join(raw), comments)
                comments = []
            event, data, raw = "message", [], []  # comments carry over to the next real frame
            continue
        raw.append(line)
        if line.startswith(":"):
            comments.append(line[1:].strip())
        elif line.startswith("event:"):
            event = line[6:].strip()
        elif line.startswith("data:"):
            data.append(line[5:].lstrip())


class HermesClient:
    def __init__(self, base_url: str, key: str | None, timeout: float = 60.0) -> None:
        self.base = base_url.rstrip("/")
        self.key = key
        self.http = httpx.Client(base_url=self.base, timeout=timeout)

    def _h(self, extra: dict[str, str] | None = None, auth: bool = True) -> dict[str, str]:
        h = {"Authorization": f"Bearer {self.key}"} if (auth and self.key) else {}
        return {**h, **(extra or {})}

    def capabilities(self, auth: bool = True) -> httpx.Response:
        return self.http.get("/v1/capabilities", headers=self._h(auth=auth))

    def start_run(
        self,
        input: str,
        *,
        instructions: str | None = None,
        conversation_history: list[dict[str, Any]] | None = None,
        idempotency_key: str | None = None,
        auth: bool = True,
        **extra: Any,
    ) -> httpx.Response:
        body: dict[str, Any] = {"input": input, **extra}
        if instructions is not None:
            body["instructions"] = instructions
        if conversation_history is not None:
            body["conversation_history"] = conversation_history
        hdr = {"Idempotency-Key": idempotency_key} if idempotency_key else {}
        return self.http.post("/v1/runs", json=body, headers=self._h(hdr, auth))

    def get_run(self, run_id: str) -> httpx.Response:
        return self.http.get(f"/v1/runs/{run_id}", headers=self._h())

    def stop_run(self, run_id: str) -> httpx.Response:
        return self.http.post(f"/v1/runs/{run_id}/stop", headers=self._h())

    def events(self, run_id: str, deadline_s: float = 120.0) -> list[SseFrame]:
        """Collect frames until a terminal event or the deadline."""
        frames: list[SseFrame] = []
        end = time.monotonic() + deadline_s
        with self.http.stream(
            "GET", f"/v1/runs/{run_id}/events", headers=self._h(), timeout=deadline_s
        ) as r:
            r.raise_for_status()
            for f in parse_sse(r.iter_lines()):
                frames.append(f)
                if f.event in TERMINAL_EVENTS or time.monotonic() > end:
                    break
        return frames

    def wait_terminal(self, run_id: str, deadline_s: float = 120.0) -> dict[str, Any]:
        end = time.monotonic() + deadline_s
        while time.monotonic() < end:
            body = self.get_run(run_id).json()
            if body.get("status") in TERMINAL_STATUSES:
                return body  # type: ignore[no-any-return]
            time.sleep(0.5)
        raise TimeoutError(f"run {run_id} not terminal after {deadline_s}s")

    def run_to_end(self, input: str, **kw: Any) -> dict[str, Any]:
        r = self.start_run(input, **kw)
        r.raise_for_status()
        return self.wait_terminal(r.json()["run_id"])
