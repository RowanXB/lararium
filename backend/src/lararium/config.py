from functools import lru_cache
from typing import Literal, Self

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="LARARIUM_")

    database_url: str
    migration_database_url: str | None = None
    auth_mode: Literal["dev", "tailscale"] = "tailscale"
    owner_login: str | None = None

    @model_validator(mode="after")
    def _tailscale_needs_owner_login(self) -> Self:
        if self.auth_mode == "tailscale" and not self.owner_login:
            raise ValueError(
                "owner_login (LARARIUM_OWNER_LOGIN) is required when auth_mode is tailscale"
            )
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()  # pyright: ignore[reportCallIssue]  # fields are read from LARARIUM_* env vars
