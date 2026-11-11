from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="FG_")

    database_url: str
    migration_database_url: str | None = None
    auth_mode: Literal["dev", "tailscale"] = "dev"
    owner_login: str | None = None


@lru_cache
def get_settings() -> Settings:
    return Settings()  # pyright: ignore[reportCallIssue]  # fields are read from FG_* env vars
