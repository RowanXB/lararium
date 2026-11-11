from datetime import date, datetime
from zoneinfo import ZoneInfo


def local_date(ts: datetime, tz: ZoneInfo) -> date:
    """Return the calendar date of an aware timestamp in the given time zone."""
    if ts.utcoffset() is None:
        raise ValueError("local_date needs a timezone-aware datetime")
    return ts.astimezone(tz).date()
