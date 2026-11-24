from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from lararium.core.errors import SettingNotFoundError
from lararium.core.settings_store import get_setting
from lararium.modules.finance.errors import TransferSettingsError

SETTING_KEY = "finance.transfers"


@dataclass(frozen=True)
class TransferSettings:
    # The receiving record must be booked from window_before calendar days before the
    # sending one's booking date to window_after after it, both included (whole days:
    # pairs.counterpart_bounds, queries.counterpart_window). Together they are shorter than
    # a week, so a weekly transfer's window never reaches the next occurrence.
    window_before: timedelta
    window_after: timedelta
    overdue: timedelta  # an awaiting transfer is overdue after this long
    # The tiers that pair by time rather than by booking date (pairs.cross_currency_tier,
    # pairs.card_tier) take two records whose occurred_at are at most this far apart, both
    # included: eToro stamps both sides of an internal transfer with one clock, and a Monzo
    # card payment and its Trading 212 deposit are 0 s apart while two deposits were 512 s
    # apart (eToro design §5.12, Task 0 check 5; Decision 16).
    near_window: timedelta = timedelta(minutes=2)


# A Faster Payment arrives the same day; BACS takes 3 working days, up to 5 calendar days
# over a weekend: counted in booking dates, so a statement row (a date, stored at 00:00)
# reaches a Monzo record at any time on that 5th day.
DEFAULTS = TransferSettings(
    window_before=timedelta(days=1), window_after=timedelta(days=5), overdue=timedelta(days=7)
)
_WEEK = timedelta(days=7)
# The most days any key may give: checked before a timedelta is built, so no value can
# overflow one, or `now - overdue`, outside TransferSettingsError.
_MAX_DAYS = 3650

_FIELDS = {
    "window_before_days": "window_before",
    "window_after_days": "window_after",
    "overdue_days": "overdue",
}


async def load_transfer_settings(session: AsyncSession) -> TransferSettings:
    """The defaults, overridden by whatever the setting `finance.transfers` gives:
    `{"window_before_days", "window_after_days", "overdue_days"}`, each optional and a
    whole number of days from 1 to 3650, with the two windows together shorter than 7 days.
    A malformed setting raises TransferSettingsError."""
    try:
        value = await get_setting(session, SETTING_KEY)
    except SettingNotFoundError:
        return DEFAULTS
    if not isinstance(value, dict):
        raise TransferSettingsError(f"{SETTING_KEY} must be an object, got {value!r}")
    unknown = set(value) - set(_FIELDS)
    if unknown:
        raise TransferSettingsError(f"{SETTING_KEY} has unknown keys: {', '.join(sorted(unknown))}")
    days = {
        "window_before": DEFAULTS.window_before,
        "window_after": DEFAULTS.window_after,
        "overdue": DEFAULTS.overdue,
    }
    for key, field in _FIELDS.items():
        if key not in value:
            continue
        given = value[key]
        # bool is an int subclass; true is not a number of days.
        if isinstance(given, bool) or not isinstance(given, int) or not 0 < given <= _MAX_DAYS:
            raise TransferSettingsError(
                f"{SETTING_KEY}.{key} must be a whole number of days from 1 to {_MAX_DAYS},"
                f" got {given!r}"
            )
        days[field] = timedelta(days=given)
    if days["window_before"] + days["window_after"] >= _WEEK:
        raise TransferSettingsError(
            f"{SETTING_KEY}: window_before_days plus window_after_days must be shorter than"
            " 7 days, or a weekly transfer would also pair with the next week's"
        )
    return TransferSettings(**days)
