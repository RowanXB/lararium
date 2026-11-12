from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from sqlalchemy import exc, text

from lararium.modules.finance.snapshots import record_snapshot


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


@pytest.mark.parametrize("bad", ["NaN", "Infinity", "-Infinity", "sNaN"])
async def test_snapshot_quantity_must_be_finite(run, make_account, gbp, bad) -> None:
    acc = await make_account()
    with pytest.raises(ValueError, match="finite"):
        await run(
            record_snapshot,
            account_id=acc.id,
            instrument_id=gbp.id,
            quantity=Decimal(bad),
            as_of=datetime(2026, 3, 31, 23, 30, tzinfo=UTC),
        )


async def test_database_rejects_non_finite_snapshot_quantity(
    session, run, make_account, gbp
) -> None:
    acc = await make_account()
    snap = await run(
        record_snapshot,
        account_id=acc.id,
        instrument_id=gbp.id,
        quantity=Decimal("1"),
        as_of=datetime(2026, 3, 31, 23, 30, tzinfo=UTC),
    )
    with pytest.raises(exc.IntegrityError, match="check constraint"):
        await session.execute(
            text("update finance.balance_snapshots set quantity = 'Infinity' where id = :s"),
            {"s": snap.id},
        )
