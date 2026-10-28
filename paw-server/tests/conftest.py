import os
import uuid
from urllib.parse import urlparse

import psycopg
import pytest
from fastapi.testclient import TestClient

from paw_server.core.config import Settings
from paw_server.main import create_app

UNREACHABLE = "postgresql://x:y@127.0.0.1:1/none"


@pytest.fixture
def settings() -> Settings:
    return Settings(
        database_url=UNREACHABLE, owner_token="t" * 20, env="ci", db_connect_timeout_s=1
    )  # type: ignore[arg-type]


@pytest.fixture
def client(settings: Settings):
    with TestClient(create_app(settings), raise_server_exceptions=False) as c:
        yield c


@pytest.fixture
def db_url():
    """A throwaway database on the server named by PAW_TEST_DATABASE_URL."""
    admin = os.environ.get("PAW_TEST_DATABASE_URL")
    if not admin:
        pytest.skip("PAW_TEST_DATABASE_URL not set")
    name = f"paw_test_{uuid.uuid4().hex[:8]}"
    with psycopg.connect(admin, autocommit=True) as c:
        c.execute(f'CREATE DATABASE "{name}"')
    yield urlparse(admin)._replace(path=f"/{name}").geturl()
    with psycopg.connect(admin, autocommit=True) as c:
        c.execute(f'DROP DATABASE "{name}" WITH (FORCE)')


# ---- integration helpers ---------------------------------------------------------------------
import threading  # noqa: E402
from dataclasses import dataclass, field  # noqa: E402

from alembic import command  # noqa: E402

from paw_server.db.migrations_info import alembic_config  # noqa: E402
from paw_server.db.pool import create_pool  # noqa: E402
from paw_server.modules.ingestion.embedding import FakeEmbedder  # noqa: E402
from paw_server.modules.ingestion.pipeline import Worker  # noqa: E402
from paw_server.modules.ingestion.storage import MemoryObjectStore  # noqa: E402

TOKEN = "test-owner-token-0123456789"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


class CountingEmbedder(FakeEmbedder):
    def __init__(self) -> None:
        super().__init__()
        self.calls: list[int] = []
        self.hook = None  # optional callable(call_no) that may raise

    def embed(self, texts):
        self.calls.append(len(texts))
        if self.hook:
            self.hook(len(self.calls))
        return super().embed(texts)


@dataclass
class Env:
    url: str
    store: MemoryObjectStore
    embedder: CountingEmbedder
    overrides: dict = field(default_factory=dict)
    _n: int = 0

    def settings(self, **kw) -> Settings:
        base = dict(
            database_url=self.url,
            owner_token=TOKEN,
            env="ci",
            chunk_size=300,
            chunk_overlap=40,
            embedding_batch_size=2,
            retry_base_s=0,
            retry_max_s=0,
            max_attempts=3,
        )
        return Settings(**{**base, **self.overrides, **kw})  # type: ignore[arg-type]

    def client(self, **kw) -> TestClient:
        c = TestClient(
            create_app(self.settings(**kw), store=self.store, embedder=self.embedder),
            raise_server_exceptions=False,
        )
        c.__enter__()
        return c

    def worker(self, **kw) -> Worker:
        s = self.settings(**kw)
        self._n += 1
        pool = create_pool(s)
        pool.open()
        return Worker(pool, self.store, self.embedder, s, owner=f"w{self._n}")

    def drain(self, w: Worker, limit: int = 50) -> int:
        n = 0
        while n < limit and w.run_once():
            n += 1
        return n


@pytest.fixture
def env(db_url):
    command.upgrade(alembic_config(db_url), "head")
    e = Env(db_url, MemoryObjectStore(), CountingEmbedder())
    yield e


def upload(client, name, data, ctype="application/octet-stream", headers=None):
    return client.post(
        "/v1/documents", files={"file": (name, data, ctype)}, headers={**AUTH, **(headers or {})}
    )


def threads(fns):
    out = [None] * len(fns)

    def run(i, f):
        out[i] = f()

    ts = [threading.Thread(target=run, args=(i, f)) for i, f in enumerate(fns)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    return out
