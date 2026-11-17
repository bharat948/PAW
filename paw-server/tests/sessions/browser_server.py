"""Disposable API/database for browser acceptance; no model provider or live installation is used."""

import os
import threading
import time
from tempfile import TemporaryDirectory
from urllib.parse import urlparse
from uuid import uuid4

import psycopg
import uvicorn
from alembic import command
from tests.runtime.fakes import FakeRuntime, completed, delta

from paw_server.core.config import Settings
from paw_server.db.migrations_info import alembic_config
from paw_server.db.pool import create_pool
from paw_server.main import create_app
from paw_server.modules.ingestion.embedding import FakeEmbedder
from paw_server.modules.ingestion.storage import MemoryObjectStore
from paw_server.modules.runtime.agent_worker import AgentWorker
from paw_server.modules.sessions.checkpoints import CLOSE, OPEN

TOKEN = "browser-test-token-002b-not-a-secret"


def main():
    admin = os.environ["PAW_TEST_DATABASE_URL"]
    name = "paw_browser_" + uuid4().hex[:12]
    with psycopg.connect(admin, autocommit=True) as c:
        c.execute(f'CREATE DATABASE "{name}"')
    url = urlparse(admin)._replace(path="/" + name).geturl()
    stop = threading.Event()
    pool = None
    try:
        command.upgrade(alembic_config(url), "head")
        with TemporaryDirectory(prefix="paw-browser-journal-") as folder:
            settings = Settings(
                _env_file=None,
                env="ci",
                database_url=url,
                owner_token=TOKEN,
                auth_mode="token",
                deletion_journal_dir=folder,
                cors_origins=["http://127.0.0.1:15174"],
                worker_poll_s=0.1,
            )
            rt = FakeRuntime()
            release = threading.Event()

            def script(hid):
                output = (
                    "Acknowledged."
                    + OPEN
                    + '{"task_id":"fixture","status":"completed","goal":"record request","summary":"request recorded","evidence":[],"next_steps":[]}'
                    + CLOSE
                )
                reconnect = rt.created[-1]["input"] == "Reconnect this answer"
                if reconnect:
                    release.clear()
                time.sleep(0.2)
                yield delta(output[:12])
                if reconnect and not release.wait(20):
                    raise RuntimeError("browser did not release the held test run")
                yield delta(output[12:])
                yield completed(output)

            rt.script = script
            pool = create_pool(settings)
            pool.open()
            worker = AgentWorker(pool, rt, settings)

            def loop():
                while not stop.wait(0.05):
                    worker.run_once()

            t = threading.Thread(target=loop, daemon=True)
            t.start()
            app = create_app(
                settings, store=MemoryObjectStore(), embedder=FakeEmbedder(), runtime=rt
            )

            @app.post("/test/release-runtime")
            def release_runtime():
                release.set()
                return {"released": True}

            uvicorn.run(app, host="127.0.0.1", port=18002, log_level="warning")
    finally:
        stop.set()
        if pool:
            pool.close()
        with psycopg.connect(admin, autocommit=True) as c:
            c.execute(f'DROP DATABASE "{name}" WITH (FORCE)')


if __name__ == "__main__":
    main()
