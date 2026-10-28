import json
from enum import StrEnum
from functools import lru_cache
from typing import Annotated, Literal

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Environment(StrEnum):
    LOCAL = "local"
    CI = "ci"
    STAGING = "staging"
    PRODUCTION = "production"


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

    # auth (single owner; D006)
    owner_token: str = Field(min_length=16)

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

    # retrieval
    search_rate_limit_per_min: int = Field(default=60, ge=1)
    search_candidates: int = Field(default=50, ge=1, le=200)
    vector_max_distance: float = Field(default=0.75, gt=0, le=2)  # cosine; drops irrelevant vectors
    rrf_k: int = Field(default=60, ge=1)
    excerpt_chars: int = Field(default=500, ge=50)

    # worker / job runner (D005)
    worker_poll_s: float = Field(default=1.0, gt=0)
    lease_seconds: int = Field(default=60, ge=5)
    max_attempts: int = Field(default=5, ge=1)
    retry_base_s: float = Field(default=2.0, ge=0)
    retry_max_s: float = Field(default=300.0, ge=0)

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
        if self.embedding_provider == "openai" and not self.embedding_api_key:
            raise ValueError("PAW_EMBEDDING_API_KEY is required for the openai provider")
        if self.db_pool_min > self.db_pool_max:
            raise ValueError("PAW_DB_POOL_MIN must not exceed PAW_DB_POOL_MAX")
        if self.env in (Environment.STAGING, Environment.PRODUCTION):
            if "paw_local_only" in self.database_url or "paw_local_only" in self.owner_token:
                raise ValueError("local-only database credentials are not allowed in this env")
            if any(o.startswith("http://") for o in self.cors_origins):
                raise ValueError("CORS origins must be https in staging/production")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
