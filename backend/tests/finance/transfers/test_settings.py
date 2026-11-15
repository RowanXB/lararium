from datetime import timedelta

import pytest

from lararium.core.settings_store import set_setting
from lararium.modules.finance.transfers import TransferSettings, load_transfer_settings
from lararium.modules.finance.transfers.settings import DEFAULTS, SETTING_KEY


async def test_defaults_without_the_setting(session) -> None:
    assert await load_transfer_settings(session) == DEFAULTS
    assert DEFAULTS == TransferSettings(
        window_before=timedelta(days=1), window_after=timedelta(days=5), overdue=timedelta(days=7)
    )


async def test_setting_overrides_what_it_gives(session, run) -> None:
    await run(set_setting, SETTING_KEY, {"window_before_days": 1, "overdue_days": 10})
    assert await load_transfer_settings(session) == TransferSettings(
        window_before=timedelta(days=1), window_after=timedelta(days=5), overdue=timedelta(days=10)
    )


@pytest.mark.parametrize(
    ("value", "message"),
    [
        ([2, 7], "must be an object"),
        ({"window_days": 3}, "unknown keys: window_days"),
        ({"window_before_days": 0}, "positive integer"),
        ({"window_after_days": -1}, "positive integer"),
        ({"overdue_days": "7"}, "positive integer"),
        ({"overdue_days": 1.5}, "positive integer"),
        ({"window_before_days": True}, "positive integer"),
        # A window of a week or more reaches the next occurrence of a weekly transfer.
        ({"window_before_days": 2, "window_after_days": 5}, "shorter than 7 days"),
        ({"window_after_days": 6}, "shorter than 7 days"),
    ],
)
async def test_invalid_setting_refused(session, run, value, message) -> None:
    await run(set_setting, SETTING_KEY, value)
    with pytest.raises(ValueError, match=message):
        await load_transfer_settings(session)
