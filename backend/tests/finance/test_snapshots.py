from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from financial_gate.modules.finance.snapshots import record_snapshot


async def test_snapshot_as_of_date_uses_reporting_timezone(run, make_account, gbp) -> None:
    acc = await make_account()
    snap = await run(
        record_snapshot,
        account_id=acc.id,
        instrument_id=gbp.id,
        quantity=Decimal("1200.00"),
        as_of=datetime(2026, 3, 31, 23, 30, tzinfo=UTC),
    )
    assert (snap.as_of_date, snap.kind) == (date(2026, 4, 1), "manual")


async def test_snapshot_rejects_naive_datetime(run, make_account, gbp) -> None:
    acc = await make_account()
    with pytest.raises(ValueError):
        await run(
            record_snapshot,
            account_id=acc.id,
            instrument_id=gbp.id,
            quantity=Decimal("1"),
            as_of=datetime(2026, 3, 31, 23, 30),  # noqa: DTZ001
        )
