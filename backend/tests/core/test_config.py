import pytest
from pydantic import ValidationError

from financial_gate.config import Settings

DATABASE_URL = "postgresql+psycopg://x@localhost/x"


@pytest.fixture(autouse=True)
def _no_auth_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FG_AUTH_MODE", raising=False)
    monkeypatch.delenv("FG_OWNER_LOGIN", raising=False)


def test_tailscale_mode_is_the_default_and_requires_owner_login() -> None:
    with pytest.raises(ValidationError, match="owner_login"):
        Settings(database_url=DATABASE_URL)


def test_dev_mode_needs_no_owner_login() -> None:
    assert Settings(database_url=DATABASE_URL, auth_mode="dev").owner_login is None
