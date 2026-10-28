import json
from enum import StrEnum
from functools import lru_cache
from typing import Annotated

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
        if self.db_pool_min > self.db_pool_max:
            raise ValueError("PAW_DB_POOL_MIN must not exceed PAW_DB_POOL_MAX")
        if self.env in (Environment.STAGING, Environment.PRODUCTION):
            if "paw_local_only" in self.database_url:
                raise ValueError("local-only database credentials are not allowed in this env")
            if any(o.startswith("http://") for o in self.cors_origins):
                raise ValueError("CORS origins must be https in staging/production")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
