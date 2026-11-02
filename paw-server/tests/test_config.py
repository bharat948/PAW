import pytest
from pydantic import ValidationError

from paw_server.core.config import Settings


def test_database_url_required(monkeypatch):
    monkeypatch.delenv("PAW_DATABASE_URL", raising=False)
    with pytest.raises(ValidationError):
        Settings(_env_file=None)  # type: ignore[call-arg]


def test_production_rejects_local_credentials():
    with pytest.raises(ValidationError):
        Settings(
            database_url="postgresql://paw:paw_local_only@db/paw",
            owner_token="x" * 20,
            env="production",
        )  # type: ignore[arg-type]


def test_production_rejects_http_cors():
    with pytest.raises(ValidationError):
        Settings(
            database_url="postgresql://u:p@db/paw", env="production", cors_origins=["http://x"]
        )  # type: ignore[arg-type]


def test_pool_bounds():
    with pytest.raises(ValidationError):
        Settings(
            database_url="postgresql://u:p@db/p", owner_token="x" * 20, db_pool_min=5, db_pool_max=2
        )


def test_cors_origins_accepts_csv_and_json(monkeypatch):
    monkeypatch.setenv("PAW_DATABASE_URL", "postgresql://u:p@db/p")
    monkeypatch.setenv("PAW_OWNER_TOKEN", "x" * 20)
    monkeypatch.setenv("PAW_CORS_ORIGINS", "http://a, http://b")
    assert Settings(_env_file=None).cors_origins == ["http://a", "http://b"]
    monkeypatch.setenv("PAW_CORS_ORIGINS", '["http://c"]')
    assert Settings(_env_file=None).cors_origins == ["http://c"]


# --- home environment (PAW-001D, D-T03) ---

HOME_OK = dict(
    env="home",
    database_url="postgresql://paw:Xk29vQ8sLm@db:5432/paw",
    owner_token="o" * 32,
    s3_endpoint_url="http://s3:9000",
    s3_access_key="AKIAHOMEKEY0123456789",
    s3_secret_key="s3cr3t-value-0123456789",
    installation_id="home-laptop-1",
    public_base_url="https://paw.mydomain.dev",
    auth_mode="google",
    google_client_id="client-id.apps.googleusercontent.com",
    google_client_secret="g-secret-value-0123456789",
    allowed_emails="Owner@Example.org",
    api_public_url="https://paw.mydomain.dev/api",
    web_url="https://paw.mydomain.dev",
)


def home(**over):
    return Settings(_env_file=None, **{**HOME_OK, **over})  # type: ignore[arg-type]


def test_home_accepts_complete_config():
    assert home().env == "home"


@pytest.mark.parametrize(
    "over",
    [
        {"installation_id": ""},
        {"public_base_url": None},
        {"public_base_url": "http://paw.mydomain.dev"},
        {"owner_token": "changeme-changeme-changeme"},
        {"s3_secret_key": ""},
        {"s3_access_key": "paw_local_only"},
        {"database_url": "postgresql://paw:paw_local_only@db/paw"},
        {"cors_origins": ["http://localhost:5173"]},
        {"auth_mode": "token"},
        {"auth_mode": "both"},
        {"api_public_url": "http://paw.mydomain.dev/api"},
        {"web_url": "http://paw.mydomain.dev"},
        {"allowed_emails": ""},
        {"google_client_secret": ""},
    ],
)
def test_home_rejects_unsafe_config(over):
    with pytest.raises(ValidationError):
        home(**over)


def test_allowed_emails_are_normalised():
    assert home(allowed_emails=" A@x.com, b@Y.com ,").allowed_emails == ["a@x.com", "b@y.com"]


def test_google_mode_requires_client_and_allow_list_outside_home():
    base = dict(database_url="postgresql://u:p@h/d", owner_token="t" * 20, auth_mode="google")
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **base)  # type: ignore[arg-type]
    ok = Settings(  # type: ignore[arg-type]
        _env_file=None,
        **base,
        google_client_id="i",
        google_client_secret="s",
        allowed_emails="a@x.com",
    )
    assert ok.auth_mode == "google"
