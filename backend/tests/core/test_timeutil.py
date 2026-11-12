from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

import pytest

from lararium.core.timeutil import local_date

LONDON = ZoneInfo("Europe/London")


def test_local_date_crosses_midnight_in_bst() -> None:
    assert local_date(datetime(2026, 3, 31, 23, 30, tzinfo=UTC), LONDON) == date(2026, 4, 1)


def test_local_date_rejects_naive_datetime() -> None:
    with pytest.raises(ValueError):
        local_date(datetime(2026, 3, 31, 23, 30), LONDON)  # noqa: DTZ001
