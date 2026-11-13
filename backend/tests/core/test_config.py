import base64

import pytest
from pydantic import ValidationError

from lararium.config import Settings, WebhookInSettings

DATABASE_URL = "postgresql+psycopg://x@localhost/x"
MASTER_KEY = base64.b64encode(bytes(range(32))).decode()


@pytest.fixture(autouse=True)
def _no_auth_env(monkeypatch: pytest.MonkeyPatch) -> None:
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
    assert Settings(**dev, webhook_secret="s3cret").webhook() is None
    assert Settings(**dev, webhook_base_url="", webhook_secret="").webhook() is None

    settings = Settings(**dev, webhook_base_url="https://hooks.example/", webhook_secret="s3cret")
    webhook = settings.webhook()
    assert webhook is not None
    base_url, secret = webhook
    # Without the trailing slash, so `{base}/monzo/{secret}` has no empty segment.
    assert base_url == "https://hooks.example"
    assert secret.get_secret_value() == "s3cret"
    assert "s3cret" not in repr(settings)


@pytest.mark.parametrize("secret", ["has/slash", "has space", "pct%41", "q?x", "frag#x", "é"])
def test_webhook_secret_must_be_url_safe(secret: str) -> None:
    with pytest.raises(ValidationError, match="webhook_secret"):
        Settings(database_url=DATABASE_URL, auth_mode="dev", webhook_secret=secret)
    with pytest.raises(ValidationError, match="webhook_secret"):
        WebhookInSettings(database_url=DATABASE_URL, webhook_secret=secret)


def test_webhook_in_settings_need_only_database_url_and_secret() -> None:
    # Tailscale mode is the default, yet the receiver needs no owner login or master key.
    settings = WebhookInSettings(database_url=DATABASE_URL, webhook_secret="s3cret")
    assert settings.webhook_secret.get_secret_value() == "s3cret"
    with pytest.raises(ValidationError, match="webhook_secret"):
        WebhookInSettings(database_url=DATABASE_URL)  # pyright: ignore[reportCallIssue]
    with pytest.raises(ValidationError, match="webhook_secret"):
        WebhookInSettings(database_url=DATABASE_URL, webhook_secret="")
