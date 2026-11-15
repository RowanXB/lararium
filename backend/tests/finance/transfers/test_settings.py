from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest

from lararium.core.settings_store import set_setting
from lararium.modules.finance.transfers import (
    TransferSettings,
    due_notifications,
    is_overdue,
    load_transfer_settings,
)
from lararium.modules.finance.transfers.queries import TransferRecord
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
        ({"window_before_days": 0}, "from 1 to 3650"),
        ({"window_after_days": -1}, "from 1 to 3650"),
        ({"overdue_days": "7"}, "from 1 to 3650"),
        ({"overdue_days": 1.5}, "from 1 to 3650"),
        ({"window_before_days": True}, "from 1 to 3650"),
        # Bounded before any timedelta is built, so none overflows (OverflowError, a 500).
        ({"overdue_days": 3651}, "from 1 to 3650"),
        ({"overdue_days": 10**9}, "from 1 to 3650"),
        ({"window_before_days": 10**9}, "from 1 to 3650"),
        ({"window_after_days": 10**12}, "from 1 to 3650"),
        # A window of a week or more reaches the next occurrence of a weekly transfer.
        ({"window_before_days": 2, "window_after_days": 5}, "shorter than 7 days"),
        ({"window_after_days": 6}, "shorter than 7 days"),
    ],
)
async def test_invalid_setting_refused(session, run, value, message) -> None:
    await run(set_setting, SETTING_KEY, value)
    with pytest.raises(ValueError, match=message):
        await load_transfer_settings(session)


async def test_longest_overdue_loads_and_is_usable(session, run) -> None:
    """The largest overdue the setting accepts still works where it is subtracted from now."""
    await run(set_setting, SETTING_KEY, {"overdue_days": 3650})
    settings = await load_transfer_settings(session)
    assert settings.overdue == timedelta(days=3650)
    now = datetime(2026, 11, 15, tzinfo=UTC)
    transfer = TransferRecord(
        id=uuid4(),
        from_account_id=uuid4(),
        to_account_id=uuid4(),
        instrument_id=uuid4(),
        amount=Decimal(1),
        status="awaiting",
        review_reason=None,
        out_transaction_id=uuid4(),
        in_transaction_id=None,
        placeholder_transaction_id=None,
        settlement_transaction_id=None,
        opened_at=now,
        matched_at=None,
        notified_at=None,
        created_at=now,
    )
    assert not is_overdue(transfer, now, settings)
    assert await due_notifications(session, now) == []
