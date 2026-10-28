import pytest
from pydantic import ValidationError

from paw_server.core.config import Settings


def test_database_url_required(monkeypatch):
    monkeypatch.delenv("PAW_DATABASE_URL", raising=False)
    with pytest.raises(ValidationError):
        Settings(_env_file=None)  # type: ignore[call-arg]


def test_production_rejects_local_credentials():
    with pytest.raises(ValidationError):
        Settings(database_url="postgresql://paw:paw_local_only@db/paw", env="production")  # type: ignore[arg-type]


def test_production_rejects_http_cors():
    with pytest.raises(ValidationError):
        Settings(
            database_url="postgresql://u:p@db/paw", env="production", cors_origins=["http://x"]
        )  # type: ignore[arg-type]


def test_pool_bounds():
    with pytest.raises(ValidationError):
        Settings(database_url="postgresql://u:p@db/p", db_pool_min=5, db_pool_max=2)


def test_cors_origins_accepts_csv_and_json(monkeypatch):
    monkeypatch.setenv("PAW_DATABASE_URL", "postgresql://u:p@db/p")
    monkeypatch.setenv("PAW_CORS_ORIGINS", "http://a, http://b")
    assert Settings(_env_file=None).cors_origins == ["http://a", "http://b"]
    monkeypatch.setenv("PAW_CORS_ORIGINS", '["http://c"]')
    assert Settings(_env_file=None).cors_origins == ["http://c"]
