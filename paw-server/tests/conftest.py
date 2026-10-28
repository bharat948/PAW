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
    return Settings(database_url=UNREACHABLE, env="ci", db_connect_timeout_s=1)  # type: ignore[arg-type]


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
