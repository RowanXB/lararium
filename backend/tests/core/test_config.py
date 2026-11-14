import base64

import pytest
from pydantic import ValidationError
from pydantic_settings import BaseSettings

from lararium import config
from lararium.config import Settings, WebhookInSettings

DATABASE_URL = "postgresql+psycopg://x@localhost/x"
SECRET = "0123456789abcdef0123456789abcdef"  # 32 characters, the minimum
MASTER_KEY = base64.b64encode(bytes(range(32))).decode()


@pytest.fixture(autouse=True)
def _no_auth_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LARARIUM_PUBLIC_BASE_URL", raising=False)
    monkeypatch.delenv("LARARIUM_AUTH_MODE", raising=False)
    monkeypatch.delenv("LARARIUM_OWNER_LOGIN", raising=False)
    monkeypatch.delenv("LARARIUM_MASTER_KEY", raising=False)
    monkeypatch.delenv("LARARIUM_WEBHOOK_BASE_URL", raising=False)
    monkeypatch.delenv("LARARIUM_WEBHOOK_SECRET", raising=False)


def test_tailscale_mode_is_the_default_and_requires_owner_login() -> None:
    with pytest.raises(ValidationError, match="owner_login"):
        Settings(database_url=DATABASE_URL)


def test_dev_mode_needs_no_owner_login_or_master_key() -> None:
    settings = Settings(database_url=DATABASE_URL, auth_mode="dev")
    assert (settings.owner_login, settings.master_key) == (None, None)


def test_settings_require_master_key_in_tailscale_mode() -> None:
    with pytest.raises(ValidationError, match="master_key"):
        Settings(database_url=DATABASE_URL, owner_login="me@example.com")
    settings = Settings(
        database_url=DATABASE_URL, owner_login="me@example.com", master_key=MASTER_KEY
    )
    assert settings.master_key is not None
    assert settings.master_key.get_secret_value() == MASTER_KEY
    assert MASTER_KEY not in repr(settings)


def test_webhook_needs_both_base_url_and_secret() -> None:
    dev = {"database_url": DATABASE_URL, "auth_mode": "dev"}
    assert Settings(**dev).webhook() is None
    assert Settings(**dev, webhook_base_url="https://hooks.example").webhook() is None
    assert Settings(**dev, webhook_secret=SECRET).webhook() is None
    assert Settings(**dev, webhook_base_url="", webhook_secret="").webhook() is None

    settings = Settings(**dev, webhook_base_url="https://hooks.example/", webhook_secret=SECRET)
    webhook = settings.webhook()
    assert webhook is not None
    base_url, secret = webhook
    # Without the trailing slash, so `{base}/monzo/{secret}` has no empty segment.
    assert base_url == "https://hooks.example"
    assert secret.get_secret_value() == SECRET
    assert SECRET not in repr(settings)


@pytest.mark.parametrize(
    "secret",
    [
        "has/slash" + "a" * 32,
        "has space" + "a" * 32,
        "pct%41" + "a" * 32,
        "q?x" + "a" * 32,
        "é" * 40,
    ],
)
def test_webhook_secret_must_be_url_safe(secret: str) -> None:
    with pytest.raises(ValidationError, match="webhook_secret"):
        Settings(database_url=DATABASE_URL, auth_mode="dev", webhook_secret=secret)
    with pytest.raises(ValidationError, match="webhook_secret"):
        WebhookInSettings(database_url=DATABASE_URL, webhook_secret=secret)


def test_webhook_in_settings_need_only_database_url_and_secret() -> None:
    # Tailscale mode is the default, yet the receiver needs no owner login or master key.
    settings = WebhookInSettings(database_url=DATABASE_URL, webhook_secret=SECRET)
    assert settings.webhook_secret.get_secret_value() == SECRET
    with pytest.raises(ValidationError, match="webhook_secret"):
        WebhookInSettings(database_url=DATABASE_URL)  # pyright: ignore[reportCallIssue]
    with pytest.raises(ValidationError, match="webhook_secret"):
        WebhookInSettings(database_url=DATABASE_URL, webhook_secret="")


@pytest.mark.parametrize("secret", ["short", "s3cret", "a" * 31])
def test_webhook_secret_must_be_at_least_32_characters(secret: str) -> None:
    with pytest.raises(ValidationError, match="at least 32"):
        Settings(database_url=DATABASE_URL, auth_mode="dev", webhook_secret=secret)
    with pytest.raises(ValidationError, match="at least 32"):
        WebhookInSettings(database_url=DATABASE_URL, webhook_secret=secret)


def test_public_base_url_has_no_trailing_slash() -> None:
    settings = Settings(database_url=DATABASE_URL, auth_mode="dev", public_base_url="https://host/")
    assert settings.public_base_url == "https://host"


@pytest.mark.parametrize(
    "key",
    [
        "not base64!",
        base64.b64encode(bytes(31)).decode(),
        base64.b64encode(bytes(33)).decode(),
    ],
    ids=["bad-base64", "31-bytes", "33-bytes"],
)
@pytest.mark.parametrize("auth_mode", ["dev", "tailscale"])
def test_malformed_master_key_fails_at_startup(key: str, auth_mode: str) -> None:
    with pytest.raises(ValidationError, match="master key must be base64 of 32 bytes") as raised:
        Settings(
            database_url=DATABASE_URL,
            auth_mode=auth_mode,  # pyright: ignore[reportArgumentType]
            owner_login="me@example.com",
            master_key=key,  # pyright: ignore[reportArgumentType]
        )
    assert key not in str(raised.value)


def test_every_settings_class_hides_values_in_errors() -> None:
    classes = [
        value
        for value in vars(config).values()
        if isinstance(value, type) and issubclass(value, BaseSettings) and value is not BaseSettings
    ]
    assert {c.__name__ for c in classes} >= {"Settings", "WebhookInSettings"}
    for cls in classes:
        assert cls.model_config.get("hide_input_in_errors") is True, cls.__name__


def test_webhook_in_errors_quote_neither_the_secret_nor_the_database_password() -> None:
    url = "postgresql+psycopg://lararium_webhook_in:DBPASSWORD@db/lararium"
    with pytest.raises(ValidationError) as bad_secret:
        WebhookInSettings(database_url=url, webhook_secret="has+plus/SECRETVALUE" + "a" * 32)
    with pytest.raises(ValidationError) as missing:
        WebhookInSettings(database_url=url)  # pyright: ignore[reportCallIssue]

    for error in (bad_secret, missing):
        text = str(error.value)
        assert "SECRETVALUE" not in text and "DBPASSWORD" not in text
