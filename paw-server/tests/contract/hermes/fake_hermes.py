"""Fake Hermes API server for deterministic tests.

It implements ONLY what the Hermes docs state (checked 2026-11-02): /v1/capabilities, POST /v1/runs (+ Idempotency-Key),
GET /v1/runs/{id}, GET /v1/runs/{id}/events (SSE with ': keepalive'), POST /v1/runs/{id}/stop.
Passing tests against this fake proves the HARNESS and PAW's future adapter logic, NOT Hermes behaviour.
Magic inputs: 'SLOW' keeps the run alive until stopped (ignores stop if 'STUBBORN' is also present).
"""

import json
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

KEY = "fake-hermes-key-0123456789abcdef0123456789"


class _Run:
    def __init__(self, run_id: str, body: dict[str, Any]) -> None:
        self.id, self.body = run_id, body
        self.status = "running"
        self.events: list[tuple[str, dict[str, Any]]] = []
        self.cond = threading.Condition()
        self.stop_requested = False

    def emit(self, name: str, payload: dict[str, Any]) -> None:
        with self.cond:
            self.events.append((name, payload))
            self.cond.notify_all()


class FakeHermes:
    def __init__(self, features: dict[str, bool] | None = None) -> None:
        self.runs: dict[str, _Run] = {}
        self.idem: dict[str, str] = {}
        self.features: dict[str, Any] = features or {
            "run_submission": True,
            "run_status": True,
            "run_events_sse": True,
            "run_stop": True,
            "runs_idempotency": {"supported": True, "durable": True, "retention_seconds": 86400},
        }
        # fault injection for adapter tests
        self.cut_stream_after: int | None = None  # next event stream ends after this many frames
        self.fail_create: int = 0  # next N create calls answer 503 without starting a run
        self.lose_create_response: int = (
            0  # next N create calls start the run, then drop the connection
        )
        self.creates: int = 0
        fake = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a: Any) -> None:
                pass

            def _auth(self) -> bool:
                if self.headers.get("Authorization") != f"Bearer {KEY}":
                    self._json(401, {"error": {"message": "invalid api key"}})
                    return False
                return True

            def _json(
                self, code: int, body: dict[str, Any], headers: dict[str, str] | None = None
            ) -> None:
                data = json.dumps(body).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self) -> None:  # noqa: N802
                if self.path == "/health":  # unauthenticated, like the real one
                    return self._json(200, {"status": "ok"})
                if not self._auth():
                    return
                p = self.path
                if p == "/v1/capabilities":
                    return self._json(
                        200,
                        {
                            "object": "hermes.api_server.capabilities",
                            "platform": "hermes-agent",
                            "model": "hermes-agent",
                            "auth": {"type": "bearer", "required": True},
                            "features": fake.features,
                        },
                    )
                parts = p.strip("/").split("/")
                if len(parts) >= 3 and parts[:2] == ["v1", "runs"] and parts[2] in fake.runs:
                    run = fake.runs[parts[2]]
                    if len(parts) == 3:
                        return self._json(
                            200,
                            {
                                "object": "hermes.run",
                                "run_id": run.id,
                                "status": run.status,
                                "output": "".join(
                                    e[1].get("delta", "")
                                    for e in run.events
                                    if e[0] == "message.delta"
                                ),
                                "usage": {
                                    "input_tokens": 10,
                                    "output_tokens": 5,
                                    "total_tokens": 15,
                                    "cache_read_tokens": 0,
                                    "cache_write_tokens": 0,
                                },
                            },
                        )
                    if parts[3] == "events":
                        return self._sse(run)
                self._json(404, {"error": {"message": "not found"}})

            def _sse(self, run: _Run) -> None:
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                self.wfile.write(b": open\n\n")
                i = 0
                while True:
                    with run.cond:
                        while i >= len(run.events):
                            run.cond.wait(timeout=0.2)
                            if run.status in (
                                "completed",
                                "failed",
                                "cancelled",
                                "interrupted",
                            ) and i >= len(run.events):
                                return
                        name, payload = run.events[i]
                    i += 1
                    if fake.cut_stream_after is not None and i - 1 >= fake.cut_stream_after:
                        fake.cut_stream_after = None
                        return  # simulates a dropped connection mid-run
                    frame = {"event": name, "run_id": run.id, "seq": i - 1, **payload}
                    self.wfile.write(
                        f"data: {json.dumps(frame)}\n\n".encode()
                    )  # unnamed frame, like real Hermes
                    self.wfile.flush()
                    if name.startswith("run.") and name != "run.steered":
                        return

            def do_POST(self) -> None:  # noqa: N802
                if not self._auth():
                    return
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n) or b"{}")
                if self.path == "/v1/runs":
                    fake.creates += 1
                    if fake.fail_create > 0:
                        fake.fail_create -= 1
                        return self._json(503, {"error": {"message": "overloaded"}})
                    if not isinstance(body.get("input"), str):
                        return self._json(400, {"error": {"message": "input required"}})
                    key = self.headers.get("Idempotency-Key")
                    if key and key in fake.idem:
                        return self._json(
                            202,
                            {"run_id": fake.idem[key], "status": "started"},
                            {"Idempotency-Replayed": "true"},
                        )
                    rid = "run_" + uuid.uuid4().hex[:12]
                    run = _Run(rid, body)
                    fake.runs[rid] = run
                    if key:
                        fake.idem[key] = rid
                    threading.Thread(target=fake._execute, args=(run,), daemon=True).start()
                    if fake.lose_create_response > 0:
                        fake.lose_create_response -= 1
                        self.connection.close()  # run started, answer never arrives
                        return
                    return self._json(202, {"run_id": rid, "status": "started", "replayed": False})
                parts = self.path.strip("/").split("/")
                if len(parts) == 4 and parts[3] == "stop" and parts[2] in fake.runs:
                    run = fake.runs[parts[2]]
                    run.stop_requested = True
                    run.status = "stopping" if run.status == "running" else run.status
                    return self._json(200, {"status": "stopping"})
                self._json(404, {"error": {"message": "not found"}})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def _execute(self, run: _Run) -> None:
        text = str(run.body["input"])
        if "SLOW" in text:
            while "STUBBORN" in text or not run.stop_requested:  # STUBBORN never honours stop
                time.sleep(0.05)
            run.emit(
                "run.cancelled",
                {"completed": False, "interrupted": True, "turn_exit_reason": "stopped"},
            )
            run.status = "cancelled"
            return
        for word in ("echo:", " ", text[:40]):
            run.emit("message.delta", {"delta": word})
        run.emit(
            "run.completed", {"completed": True, "usage": {"input_tokens": 10, "output_tokens": 5}}
        )
        run.status = "completed"

    def close(self) -> None:
        self.server.shutdown()
