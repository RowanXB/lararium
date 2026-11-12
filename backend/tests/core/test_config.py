import base64

import pytest
from pydantic import ValidationError

from lararium.config import Settings

DATABASE_URL = "postgresql+psycopg://x@localhost/x"
MASTER_KEY = base64.b64encode(bytes(range(32))).decode()


@pytest.fixture(autouse=True)
def _no_auth_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LARARIUM_AUTH_MODE", raising=False)
    monkeypatch.delenv("LARARIUM_OWNER_LOGIN", raising=False)
    monkeypatch.delenv("LARARIUM_MASTER_KEY", raising=False)


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
