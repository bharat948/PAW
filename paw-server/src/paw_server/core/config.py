import json
from enum import StrEnum
from functools import lru_cache
from typing import Annotated, Literal

from cryptography.fernet import Fernet
from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Environment(StrEnum):
    LOCAL = "local"
    CI = "ci"
    STAGING = "staging"
    PRODUCTION = "production"
    HOME = "home"  # persistent self-hosted install (PAW-001D)


_PLACEHOLDERS = (
    "paw_local_only",
    "changeme",
    "change-me",
    "change_me",
    "replace",
    "example",
    "todo",
)


class Settings(BaseSettings):
    """Runtime configuration. Credentials come from the environment, never from the repo."""

    model_config = SettingsConfigDict(env_prefix="PAW_", env_file=".env", extra="ignore")

    env: Environment = Environment.LOCAL
    database_url: str = Field(min_length=1)
    log_level: str = "INFO"
    cors_origins: Annotated[list[str], NoDecode] = []
    db_pool_min: int = Field(default=1, ge=1)
    db_pool_max: int = Field(default=5, ge=1)
    db_connect_timeout_s: float = Field(default=2.0, gt=0)
    release_revision: str = "dev"

    # deployment identity (PAW-001D); only enforced when env=home
    installation_id: str = ""
    enabled_profile: Literal["foundation", "admin-agent"] = "foundation"
    public_base_url: str | None = None  # the one https origin browsers use

    # auth (single owner; D006)
    owner_token: str = Field(min_length=16)

    # authentication (PAW-001D-06). token = the dev owner token only; google = Google sign-in only; both = either
    auth_mode: Literal["token", "google", "both"] = "token"
    google_client_id: str = ""
    google_client_secret: str = ""
    allowed_emails: Annotated[
        list[str], NoDecode
    ] = []  # Google accounts allowed to sign in as the owner
    api_public_url: str = (
        "http://localhost:8000"  # browser-visible API origin (OAuth redirect base)
    )
    # key for connector secrets at rest (Fernet, urlsafe base64 of 32 bytes); generate with `make connector-key`
    connector_key: str = ""
    # Admin Hermes runtime (PAW-002). Empty url or key = the agent endpoints answer 503 agent_unavailable.
    hermes_url: str = ""
    hermes_key: str = ""
    agent_queue_limit: int = Field(default=5, ge=1, le=100)  # queued runs across all sessions
    agent_model: str = (
        "gpt-4o-mini"  # recorded as the requested model; Hermes does not report the served one
    )
    web_url: str = "http://localhost:5173"  # where the browser lands after Google sign-in
    session_ttl_s: int = Field(default=86400, ge=60)  # absolute session lifetime
    session_idle_s: int = Field(default=14400, ge=60)  # sign out after this much inactivity
    stepup_window_s: int = Field(
        default=43200, ge=0
    )  # recent-login window for sensitive actions (002F-R05: 12 h)

    # object storage (S3-compatible; D003)
    s3_endpoint_url: str | None = None
    s3_bucket: str = "paw-originals"
    s3_access_key: str = ""
    s3_secret_key: str = ""
    s3_region: str = "us-east-1"
    s3_create_bucket: bool = False

    # upload limits
    max_upload_bytes: int = Field(default=10 * 1024 * 1024, gt=0)

    # chunking (part of the ingest config hash)
    chunk_size: int = Field(default=1000, ge=100)
    chunk_overlap: int = Field(default=150, ge=0)
    max_chunks_per_document: int = Field(default=5000, ge=1)
    max_tabular_rows: int = Field(default=200_000, ge=1)
    max_pdf_pages: int = Field(default=500, ge=1)

    # embeddings (D004): provider adapter; "fake" is deterministic and free
    embedding_provider: Literal["fake", "openai"] = "fake"
    embedding_model: str = "fake-hash-v1"
    embedding_dim: int = 1536
    embedding_api_base: str = "https://api.openai.com/v1"
    embedding_api_key: str = ""
    embedding_timeout_s: float = Field(default=20.0, gt=0)
    embedding_batch_size: int = Field(default=64, ge=1)
    embedding_daily_token_budget: int = Field(default=2_000_000, ge=0)

    # chat / RAG answers: provider adapter; "fake" is an offline extractive stand-in (no model)
    llm_provider: Literal["fake", "openai"] = "fake"
    llm_model: str = "gpt-4o-mini"
    llm_api_base: str = "https://api.openai.com/v1"
    llm_api_key: str = ""  # falls back to PAW_EMBEDDING_API_KEY when empty
    llm_timeout_s: float = Field(default=60.0, gt=0)
    llm_max_output_tokens: int = Field(default=700, ge=16)
    llm_daily_token_budget: int = Field(default=500_000, ge=0)
    chat_rate_limit_per_min: int = Field(default=20, ge=1)
    chat_source_chars: int = Field(default=1500, ge=100)  # per-source context cap
    chat_max_history: int = Field(default=12, ge=0)

    # retrieval
    search_rate_limit_per_min: int = Field(default=60, ge=1)
    search_candidates: int = Field(default=50, ge=1, le=200)
    vector_max_distance: float = Field(default=0.75, gt=0, le=2)  # cosine; drops irrelevant vectors
    search_max_per_document: int = Field(
        default=3, ge=1
    )  # keeps one big file from flooding results
    rrf_k: int = Field(default=60, ge=1)
    excerpt_chars: int = Field(default=500, ge=50)

    # worker / job runner (D005)
    worker_poll_s: float = Field(default=1.0, gt=0)
    lease_seconds: int = Field(default=60, ge=5)
    max_attempts: int = Field(default=5, ge=1)
    retry_base_s: float = Field(default=2.0, ge=0)
    retry_max_s: float = Field(default=300.0, ge=0)

    @field_validator("allowed_emails", mode="before")
    @classmethod
    def _split_emails(cls, v: object) -> object:
        """Accept a JSON list or a comma-separated string; compare case-insensitively."""
        if isinstance(v, str):
            v = v.strip()
            v = json.loads(v) if v.startswith("[") else [e for e in v.split(",")]
        if isinstance(v, list):
            return [str(e).strip().lower() for e in v if str(e).strip()]
        return v

    @field_validator("cors_origins", mode="before")
    @classmethod
    def _split_origins(cls, v: object) -> object:
        """Accept a JSON list or a comma-separated string."""
        if isinstance(v, str):
            v = v.strip()
            if v.startswith("["):
                return json.loads(v)
            return [o.strip() for o in v.split(",") if o.strip()]
        return v

    @model_validator(mode="after")
    def _check(self) -> "Settings":
        if self.chunk_overlap >= self.chunk_size:
            raise ValueError("PAW_CHUNK_OVERLAP must be smaller than PAW_CHUNK_SIZE")
        if self.embedding_dim != 1536:
            raise ValueError(
                "PAW_EMBEDDING_DIM must be 1536 (schema vector(1536)); reindex to change"
            )
        if self.llm_provider == "openai" and not (self.llm_api_key or self.embedding_api_key):
            raise ValueError(
                "PAW_LLM_API_KEY (or PAW_EMBEDDING_API_KEY) is required for the openai LLM"
            )
        if self.embedding_provider == "openai" and not self.embedding_api_key:
            raise ValueError("PAW_EMBEDDING_API_KEY is required for the openai provider")
        if self.auth_mode in ("google", "both") and not (
            self.google_client_id and self.google_client_secret and self.allowed_emails
        ):
            raise ValueError(
                "PAW_GOOGLE_CLIENT_ID, PAW_GOOGLE_CLIENT_SECRET and PAW_ALLOWED_EMAILS are required "
                "when PAW_AUTH_MODE is google or both"
            )
        if self.connector_key:
            try:
                Fernet(self.connector_key.encode())
            except (ValueError, TypeError) as exc:
                raise ValueError("PAW_CONNECTOR_KEY must be a valid Fernet key") from exc
        if self.db_pool_min > self.db_pool_max:
            raise ValueError("PAW_DB_POOL_MIN must not exceed PAW_DB_POOL_MAX")
        if self.env in (Environment.STAGING, Environment.PRODUCTION, Environment.HOME):
            if "paw_local_only" in self.database_url or "paw_local_only" in self.owner_token:
                raise ValueError("local-only database credentials are not allowed in this env")
            if any(o.startswith("http://") for o in self.cors_origins):
                raise ValueError("CORS origins must be https in staging/production/home")
        if self.env is Environment.HOME:
            self._check_home()
        return self

    def _check_home(self) -> None:
        """A home install holds real data: refuse dev defaults and placeholders at startup."""
        if self.auth_mode != "google":
            raise ValueError(
                "PAW_AUTH_MODE must be google in the home environment (no owner token)"
            )
        for name in ("api_public_url", "web_url"):
            if not getattr(self, name).startswith("https://"):
                raise ValueError(
                    f"PAW_{name.upper()} must be an https:// origin in the home environment"
                )
        if not self.installation_id.strip():
            raise ValueError("PAW_INSTALLATION_ID is required in the home environment")
        if not (self.public_base_url or "").startswith("https://"):
            raise ValueError(
                "PAW_PUBLIC_BASE_URL must be an https:// origin in the home environment"
            )
        secrets = {
            "PAW_OWNER_TOKEN": self.owner_token,
            "PAW_S3_ACCESS_KEY": self.s3_access_key,
            "PAW_S3_SECRET_KEY": self.s3_secret_key,
            "PAW_DATABASE_URL": self.database_url,
        }
        for name, value in secrets.items():
            if not value or any(m in value.lower() for m in _PLACEHOLDERS):
                raise ValueError(f"{name} is empty or a placeholder; set a real secret for home")


@lru_cache
def get_settings() -> Settings:
    return Settings()
