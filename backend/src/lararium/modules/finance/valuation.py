"""Thin wrappers over the versioned valuation functions in the database (migration 0006).

The SQL functions are the shared contract; BI and MCP call the same ones directly.
"""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Literal, cast
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from lararium.modules.finance.errors import FinanceValidationError

HoldingStatus = Literal["ok", "missing_price", "missing_fx"]


@dataclass(frozen=True)
class BalanceRow:
    account_id: UUID
    instrument_id: UUID
    quantity: Decimal
    snapshot_quantity: Decimal | None
    snapshot_date: date | None


@dataclass(frozen=True)
class HoldingRow:
    account_id: UUID
    account_name: str
    instrument_id: UUID
    symbol: str
    quantity: Decimal
    unit_price: Decimal | None
    price_currency_id: UUID | None
    fx_rate: Decimal | None
    value: Decimal | None  # in the reporting currency; None unless status is "ok"
    status: HoldingStatus


@dataclass(frozen=True)
class NetWorthPoint:
    day: date
    value: Decimal
    missing_count: int  # holdings left out of value for lack of a price or FX rate


async def balances_asof(session: AsyncSession, day: date) -> list[BalanceRow]:
    rows = await session.execute(
        text("select * from finance.v1_balances_asof(:day) order by account_id, instrument_id"),
        {"day": day},
    )
    return [
        BalanceRow(
            account_id=row.account_id,
            instrument_id=row.instrument_id,
            quantity=row.quantity,
            snapshot_quantity=row.snapshot_quantity,
            snapshot_date=row.snapshot_date,
        )
        for row in rows
    ]


async def holdings_asof(session: AsyncSession, day: date) -> list[HoldingRow]:
    rows = await session.execute(
        text(
            "select * from finance.v1_valuation_asof(:day)"
            " order by account_name, symbol, account_id, instrument_id"
        ),
        {"day": day},
    )
    return [
        HoldingRow(
            account_id=row.account_id,
            account_name=row.account_name,
            instrument_id=row.instrument_id,
            symbol=row.symbol,
            quantity=row.quantity,
            unit_price=row.unit_price,
            price_currency_id=row.price_currency_id,
            fx_rate=row.fx_rate,
            value=row.value,
            status=cast(HoldingStatus, row.status),
        )
        for row in rows
    ]


# Each day of a series is a full valuation, so this bounds the work one call can start.
MAX_SERIES_DAYS = 3653  # ten years


async def net_worth_series(session: AsyncSession, start: date, end: date) -> list[NetWorthPoint]:
    """Net worth for every day from start to end, both included."""
    if end < start:
        raise FinanceValidationError("the series must not end before it starts")
    if (end - start).days + 1 > MAX_SERIES_DAYS:
        raise FinanceValidationError(f"a series covers at most {MAX_SERIES_DAYS} days")
    rows = await session.execute(
        text("select * from finance.v1_net_worth_series(:start, :end) order by day"),
        {"start": start, "end": end},
    )
    return [
        NetWorthPoint(day=row.day, value=row.value, missing_count=row.missing_count) for row in rows
    ]
