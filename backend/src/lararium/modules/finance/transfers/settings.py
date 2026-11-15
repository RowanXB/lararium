from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from lararium.core.errors import SettingNotFoundError
from lararium.core.settings_store import get_setting
from lararium.modules.finance.errors import TransferSettingsError

SETTING_KEY = "finance.transfers"


@dataclass(frozen=True)
class TransferSettings:
    # The receiving record must occur from window_before before the sending one's time to
    # window_after after it. Together they are shorter than a week, so a weekly transfer's
    # window never reaches the next occurrence.
    window_before: timedelta
    window_after: timedelta
    overdue: timedelta  # an awaiting transfer is overdue after this long


# A Faster Payment arrives the same day; BACS takes 3 working days, up to 5 calendar days
# over a weekend.
DEFAULTS = TransferSettings(
    window_before=timedelta(days=1), window_after=timedelta(days=5), overdue=timedelta(days=7)
)
_WEEK = timedelta(days=7)

_FIELDS = {
    "window_before_days": "window_before",
    "window_after_days": "window_after",
    "overdue_days": "overdue",
}


async def load_transfer_settings(session: AsyncSession) -> TransferSettings:
    """The defaults, overridden by whatever the setting `finance.transfers` gives:
    `{"window_before_days", "window_after_days", "overdue_days"}`, each optional and a
    positive whole number of days, with the two windows together shorter than 7 days. A
    malformed setting raises TransferSettingsError."""
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
        if isinstance(given, bool) or not isinstance(given, int) or given <= 0:
            raise TransferSettingsError(
                f"{SETTING_KEY}.{key} must be a positive integer, got {given!r}"
            )
        days[field] = timedelta(days=given)
    if days["window_before"] + days["window_after"] >= _WEEK:
        raise TransferSettingsError(
            f"{SETTING_KEY}: window_before_days plus window_after_days must be shorter than"
            " 7 days, or a weekly transfer would also pair with the next week's"
        )
    return TransferSettings(**days)
