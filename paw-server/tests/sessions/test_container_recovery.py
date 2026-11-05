"""T205 and R04. Opt in with PAW_LEDGER_REAL=1; only fresh, disposable containers are removed.

Uses the previously authorized S0 provider credentials without printing or copying them.
The owner's existing Hermes container and database are never changed.
"""

import os
import subprocess
import sys
import time
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from tests.test_agent_integration import Harness, sql

from paw_server.modules.identity import service as identity
from paw_server.modules.runtime.hermes_adapter import HermesAdapter
from paw_server.modules.sessions import assembler

pytestmark = [
    pytest.mark.integration,
    pytest.mark.hermes_real,
    pytest.mark.skipif(
        os.environ.get("PAW_LEDGER_REAL") != "1", reason="opt-in isolated real-container gate"
    ),
]


class DisposableHermes:
    def __init__(self):
        self.name = "paw-002b-test-" + uuid4().hex[:12]
        self.envfile = Path(
            os.environ.get("PAW_HERMES_ENV", str(Path.home() / ".config/paw/s0.env"))
        )
        self.key = next(
            line.partition("=")[2].strip().strip("\"'")
            for line in self.envfile.read_text().splitlines()
            if line.startswith("API_SERVER_KEY=")
        )
        self.adapter = None

    def start(self):
        result = subprocess.run(
            [
                "docker",
                "run",
                "-d",
                "--rm",
                "--memory=1g",
                "--cpus=1",
                "--name",
                self.name,
                "--env-file",
                str(self.envfile),
                "-e",
                "PAW_MCP_URL=",
                "-e",
                "HERMES_CONFIG_SET=model.provider=openai-api;model.default=gpt-4o-mini",
                "-p",
                "127.0.0.1::8642",
                "paw-hermes:s0",
            ],
            capture_output=True,
        )
        if result.returncode:
            raise RuntimeError("Could not start the isolated Hermes test container")
        port = (
            subprocess.run(
                ["docker", "port", self.name, "8642/tcp"],
                capture_output=True,
                text=True,
                check=True,
            )
            .stdout.strip()
            .rsplit(":", 1)[-1]
        )
        self.adapter = HermesAdapter("http://127.0.0.1:" + port, self.key)
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            if self.adapter.healthy() and not self.adapter.missing_capabilities():
                return self.adapter
            time.sleep(0.5)
        raise RuntimeError("Isolated Hermes did not become ready")

    def stop(self):
        subprocess.run(["docker", "rm", "-f", self.name], capture_output=True, check=False)


@pytest.fixture
def container():
    box = DisposableHermes()
    try:
        box.start()
        yield box
    finally:
        box.stop()


def test_t205_recreate_api_worker_and_hermes_without_local_state(env, container):
    first = Harness(env, container.adapter)
    second = None
    try:
        sid = first.session()
        fact = "MARMOT-" + uuid4().hex[:8]
        first.send(
            sid,
            message=f"The nickname of our fictional garden is {fact}. Remember it. Reply briefly.",
        )
        assert first.worker.run_once()
        next_run = first.send(
            sid,
            message="What is the nickname of our fictional garden? Reply with the nickname only.",
        )
        rid = UUID(next_run["id"])
        with first.pool.connection() as c:
            profile = identity.get_active(c, "admin-hermes")
            expected = assembler.assemble(
                c,
                UUID(sid),
                rid,
                instructions=profile.compiled.run_instructions,
                profile_hash=profile.compiled.compiled_hash,
            )
            c.commit()
        first.c.__exit__(None, None, None)
        first.pool.close()
        container.stop()  # no volumes: deletes SQLite, memory and every runtime-local cache
        rt = container.start()
        second = Harness(env, rt)
        assert second.worker.recover() == 0
        assert second.worker.run_once()
        result = second.run(next_run["id"])
        assert result["state"] == "SUCCEEDED" and fact in result["output"]
        assert sql(env, "SELECT assembly_hash FROM agent_runs WHERE id=%s", rid) == [
            (expected.hash,)
        ]
    finally:
        if second:
            second.c.__exit__(None, None, None)
            second.pool.close()
        else:
            first.pool.close()


def worker_process(h, container):
    settings = h.settings.model_copy(
        update={
            "hermes_url": container.adapter._http.base_url.__str__().rstrip("/"),
            "hermes_key": container.key,
        }
    )
    process = subprocess.Popen(
        [
            sys.executable,
            "-c",
            """
import sys
from paw_server.core.config import Settings
from paw_server.db.pool import create_pool
from paw_server.modules.runtime.agent_worker import AgentWorker
from paw_server.modules.runtime.hermes_adapter import HermesAdapter
s = Settings.model_validate_json(sys.stdin.read())
p = create_pool(s)
p.open()
try:
    AgentWorker(p, HermesAdapter(s.hermes_url, s.hermes_key), s).run_once()
finally:
    p.close()
""",
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    process.stdin.write(settings.model_dump_json())
    process.stdin.close()
    return process


def wait_for(predicate, timeout=90):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.02)
    raise AssertionError("Timed out waiting for runtime evidence")


def test_r04_worker_resumes_a_real_run_that_finished_while_worker_was_down(env, container):
    h = Harness(env, container.adapter)
    process = None
    try:
        run = h.send(h.session(), message="Reply with exactly: recovery confirmed")
        rid = UUID(run["id"])
        process = worker_process(h, container)
        hid = wait_for(
            lambda: sql(env, "SELECT hermes_run_id FROM agent_runs WHERE id=%s", rid)[0][0]
        )
        process.kill()
        process.wait(timeout=10)
        wait_for(lambda: container.adapter.get_run(hid).terminal)
        assert h.worker.recover() == 1
        outcome = h.run(run["id"])
        assert outcome["state"] == "SUCCEEDED", (outcome["state"], outcome["error_code"])
        assert "recovery confirmed" in outcome["output"].lower()
        assert h.worker.recover() == 0
        assert sql(
            env,
            "SELECT count(*) FROM agent_session_messages WHERE run_id=%s AND kind='assistant'",
            rid,
        ) == [(1,)]
    finally:
        if process and process.poll() is None:
            process.kill()
            process.wait(timeout=10)
        h.c.__exit__(None, None, None)
        h.pool.close()


def test_runtime_recreation_preserves_partial_output_as_unfinished(env, container):
    h = Harness(env, container.adapter)
    process = None
    try:
        run = h.send(
            h.session(),
            message="Write a long numbered list of 500 imaginary garden names, one per line. Begin immediately.",
        )
        rid = UUID(run["id"])
        process = worker_process(h, container)
        wait_for(
            lambda: sql(
                env,
                "SELECT count(*) FROM agent_run_events WHERE run_id=%s AND type='message.delta'",
                rid,
            )[0][0]
        )
        process.kill()
        process.wait(timeout=10)
        assert h.run(run["id"])["state"] == "RUNNING"
        container.stop()
        h.worker.runtime = container.start()
        assert h.worker.recover() == 1
        outcome = h.run(run["id"])
        assert outcome["state"] == "INTERRUPTED"
        assert outcome["output"]
        assert sql(
            env,
            "SELECT status FROM agent_session_messages WHERE run_id=%s AND kind='assistant'",
            rid,
        ) == [("unfinished",)]
        assert h.worker.recover() == 0
    finally:
        if process and process.poll() is None:
            process.kill()
            process.wait(timeout=10)
        h.c.__exit__(None, None, None)
        h.pool.close()
