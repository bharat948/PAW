"""Pinned-runtime gate: fake provider only, no owner state or paid credentials.
Run with PAW_PROXY_CONTRACT=1. Requests must carry the explicit PAW session identity.
"""
import json
import os
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from uuid import uuid4

import httpx
import pytest

pytestmark = pytest.mark.skipif(os.getenv("PAW_PROXY_CONTRACT") != "1", reason="isolated Docker contract")


def test_pinned_runtime_proxy_contract(tmp_path):
    key = "test-only-proxy-key-" + uuid4().hex
    calls = []
    forwarded = []
    allowed = set()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            identity = self.headers.get("X-PAW-Run-ID")
            calls.append((identity, body))
            if self.headers.get("Authorization") != f"Bearer {key}" or identity not in allowed:
                self.send_response(403)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"error":{"message":"budget denied","type":"permission_error","code":"budget_exceeded"}}')
                return
            forwarded.append(body)
            usage = {"prompt_tokens": 20, "completion_tokens": 3, "total_tokens": 23}
            self.send_response(200)
            streaming = body.get("stream", False)
            self.send_header("Content-Type", "text/event-stream" if streaming else "application/json")
            self.end_headers()
            base = {"id": "chatcmpl-fixture", "created": 1, "model": "gpt-4o-mini-2024-07-18"}
            if streaming:
                frames = [
                    {**base, "object": "chat.completion.chunk", "choices": [{"index": 0, "delta": {"role": "assistant", "content": "proxy verified"}, "finish_reason": None}]},
                    {**base, "object": "chat.completion.chunk", "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}], "usage": usage},
                ]
                for frame in frames:
                    self.wfile.write(("data: " + json.dumps(frame) + "\n\n").encode())
                self.wfile.write(b"data: [DONE]\n\n")
            else:
                self.wfile.write(json.dumps({**base, "object": "chat.completion", "choices": [{"index": 0, "message": {"role": "assistant", "content": "proxy verified"}, "finish_reason": "stop"}], "usage": usage}).encode())

    server = ThreadingHTTPServer(("0.0.0.0", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    plugin = tmp_path / "paw-guard"
    plugin.mkdir()
    (plugin / "plugin.yaml").write_text('name: paw-guard\nversion: "0.1"\ndescription: proxy contract fixture\n')
    (plugin / "__init__.py").write_text('''
def stamp(request, session_id="", **kw):
    request = dict(request)
    request["extra_headers"] = {**request.get("extra_headers", {}), "X-PAW-Run-ID": session_id}
    return {"request": request}
def register(ctx):
    ctx.register_middleware("llm_request", stamp)
''')
    name = "paw-a1-contract-" + uuid4().hex[:10]
    api_key = uuid4().hex + uuid4().hex
    try:
        subprocess.run([
            "docker", "create", "--rm", "--name", name, "--memory=1g", "--cpus=1",
            "--add-host=host.docker.internal:host-gateway", "-p", "127.0.0.1::8642",
            "-e", f"API_SERVER_KEY={api_key}", "-e", f"OPENAI_API_KEY={key}",
            "-e", f"HERMES_CONFIG_SET=model.provider=custom;model.base_url=http://host.docker.internal:{server.server_port}/v1;model.default=gpt-4o-mini;model.api_key={key};agent.max_turns=2",
            "paw-hermes:s0",
        ], check=True, capture_output=True)
        subprocess.run(["docker", "cp", str(plugin / "__init__.py"), f"{name}:/opt/paw-plugins/paw-guard/__init__.py"], check=True, capture_output=True)
        subprocess.run(["docker", "start", name], check=True, capture_output=True)
        port = subprocess.check_output(["docker", "port", name, "8642/tcp"], text=True).strip().rsplit(":", 1)[1]
        with httpx.Client(base_url=f"http://127.0.0.1:{port}", headers={"Authorization": f"Bearer {api_key}"}, timeout=5) as client:
            end = time.monotonic() + 90
            while time.monotonic() < end:
                try:
                    if client.get("/health").status_code == 200:
                        break
                except httpx.HTTPError:
                    pass
                time.sleep(.5)
            else:
                raise AssertionError(subprocess.check_output(["docker", "logs", name], stderr=subprocess.STDOUT, text=True)[-4000:])
            for accept in (True, False):
                session = str(uuid4())
                if accept:
                    allowed.add(session)
                before = len(forwarded)
                response = client.post("/v1/runs", json={"input": "Reply briefly", "session_id": session})
                response.raise_for_status()
                hid = response.json()["run_id"]
                end = time.monotonic() + 60
                while time.monotonic() < end:
                    result = client.get(f"/v1/runs/{hid}").json()
                    if result["status"] in ("completed", "failed", "cancelled", "interrupted"):
                        break
                    time.sleep(.25)
                assert any(i == session for i, _ in calls), "middleware failed to preserve PAW identity"
                if accept:
                    assert result["status"] == "completed", result
                    assert "proxy verified" in result["output"]
                    assert len(forwarded) > before
                else:
                    assert len(forwarded) == before
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True)
        server.shutdown()
        server.server_close()
