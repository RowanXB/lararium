import base64
import binascii
import re
from functools import lru_cache
from typing import Literal, Self

from pydantic import SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Decoded here rather than in lararium.core.secrets, which encrypts with it: the webhook
# receiver imports this module and nothing from core.
MASTER_KEY_BYTES = 32


def load_master_key(value: SecretStr) -> bytes:
    """Decode the base64 master key; it must be exactly 32 bytes."""
    # The messages never include the value.
    try:
        key = base64.b64decode(value.get_secret_value(), validate=True)
    except binascii.Error:
        raise ValueError("the master key must be base64 of 32 bytes") from None
    if len(key) != MASTER_KEY_BYTES:
        raise ValueError(f"the master key must be base64 of 32 bytes, got {len(key)} bytes")
    return key


# The webhook secret is a path segment of the URL providers deliver to, so it must need no
# escaping (RFC 3986 unreserved characters), e.g. `openssl rand -hex 32`.
_URL_SAFE = re.compile(r"[A-Za-z0-9._~-]+")


# Anyone who learns the URL can post to the receiver, so it must not be guessable.
MIN_WEBHOOK_SECRET_LENGTH = 32


def _check_webhook_secret(value: SecretStr | None) -> SecretStr | None:
    if value is None:
        return value
    secret = value.get_secret_value()
    if not _URL_SAFE.fullmatch(secret):
        raise ValueError("must be non-empty and use only letters, digits and . _ ~ -")
    if len(secret) < MIN_WEBHOOK_SECRET_LENGTH:
        raise ValueError(
            f"must be at least {MIN_WEBHOOK_SECRET_LENGTH} characters, e.g. `openssl rand -hex 32`"
        )
    return value


# For every settings class: errors name the field, never its value, as the values include
# secrets (and a database URL carries its password).
ENV_SETTINGS = SettingsConfigDict(env_prefix="LARARIUM_", hide_input_in_errors=True)


class Settings(BaseSettings):
    model_config = ENV_SETTINGS

    database_url: str
    auth_mode: Literal["dev", "tailscale"] = "tailscale"
    owner_login: str | None = None
    # Base64 of 32 bytes; encrypts stored credentials (see lararium.core.secrets).
    master_key: SecretStr | None = None
    # Where the owner's browser reaches Lararium, e.g. for OAuth redirect URLs.
    public_base_url: str | None = None
    # Feishu custom bot for notifications; neither set means no notifications.
    feishu_webhook_url: SecretStr | None = None
    feishu_secret: SecretStr | None = None
    # Where the webhook receiver (lararium.webhook_in) is reached from the internet, and the
    # secret in its path. With both set, syncs register `{base}/monzo/{secret}` with Monzo.
    webhook_base_url: str | None = None
    webhook_secret: SecretStr | None = None

    @field_validator("webhook_base_url", "webhook_secret", mode="before")
    @classmethod
    def _empty_is_unset(cls, value: object) -> object:
        return None if value == "" else value

    @field_validator("public_base_url", "webhook_base_url")
    @classmethod
    def _no_trailing_slash(cls, value: str | None) -> str | None:
        return None if value is None else value.rstrip("/") or None

    @field_validator("webhook_secret")
    @classmethod
    def _url_safe_secret(cls, value: SecretStr | None) -> SecretStr | None:
        return _check_webhook_secret(value)

    @field_validator("master_key")
    @classmethod
    def _usable_master_key(cls, value: SecretStr | None) -> SecretStr | None:
        # A malformed key fails here, at startup, not at the first sync. Empty is checked below.
        if value is not None and value.get_secret_value():
            load_master_key(value)
        return value

    def webhook(self) -> tuple[str, SecretStr] | None:
        """The webhook base URL and secret, or None unless both are set."""
        if self.webhook_base_url is None or self.webhook_secret is None:
            return None
        return self.webhook_base_url, self.webhook_secret

    @model_validator(mode="after")
    def _tailscale_needs_owner_login(self) -> Self:
        if self.auth_mode == "tailscale" and not self.owner_login:
            raise ValueError(
                "owner_login (LARARIUM_OWNER_LOGIN) is required when auth_mode is tailscale"
            )
        return self

    @model_validator(mode="after")
    def _tailscale_needs_master_key(self) -> Self:
        if self.auth_mode == "tailscale" and (
            self.master_key is None or not self.master_key.get_secret_value()
        ):
            raise ValueError(
                "master_key (LARARIUM_MASTER_KEY) is required when auth_mode is tailscale"
            )
        return self


class WebhookInSettings(BaseSettings):
    """The webhook receiver's settings: only what it needs.

    The receiver is the one component reachable from the internet, so it gets neither the
    master key nor anything else Settings requires (an owner login, say).
    """

    model_config = ENV_SETTINGS

    # The receiver's own role (lararium_webhook_in), which can only insert into the inbox.
    database_url: str
    webhook_secret: SecretStr

    @field_validator("webhook_secret")
    @classmethod
    def _url_safe_secret(cls, value: SecretStr) -> SecretStr:
        _check_webhook_secret(value)
        return value


@lru_cache
def get_settings() -> Settings:
    return Settings()  # pyright: ignore[reportCallIssue]  # fields are read from LARARIUM_* env vars
