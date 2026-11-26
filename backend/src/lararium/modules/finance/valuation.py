"""Thin wrappers over the versioned valuation functions in the database (migration 0006).

The SQL functions are the shared contract; BI and MCP call the same ones directly.
"""

from collections.abc import Collection
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Literal, cast
from uuid import UUID

from sqlalchemy import Date, TextClause, bindparam, text
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.ext.asyncio import AsyncSession

from lararium.modules.finance.errors import FinanceValidationError
from lararium.modules.finance.system_accounts import system_account_ids

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


def _excluding(sql: str) -> TextClause:
    return text(sql).bindparams(bindparam("excluded", type_=ARRAY(PG_UUID(as_uuid=True))))


# v1_valuation_asof without the excluded accounts' holdings, and without what In transit
# holds for a transfer to or from one of them: In transit is shared by every transfer, so
# leaving out the account alone would keep the money on its way to or from it. In transit
# is a transactions-mode asset, so its balance is its postings to the day, valued as
# v1_valuation_asof values a holding. `{day}` is the day's expression.
_SHOWN_HOLDINGS = """
    select v.account_id, v.account_name, v.instrument_id, v.symbol, v.quantity, v.unit_price,
      v.price_currency_id, v.fx_rate, v.value, v.status
    from finance.v1_valuation_asof({day}) v
    where v.account_id <> all(:excluded) and v.account_id is distinct from :in_transit
    union all
    select a.id, a.name, b.instrument_id, i.symbol, b.quantity, pr.unit_price,
      pr.price_currency_id, fx.rate,
      case when pr.unit_price is not null and fx.rate is not null
        then b.quantity * pr.unit_price * fx.rate end,
      case
        when pr.unit_price is null then 'missing_price'
        when fx.rate is null then 'missing_fx'
        else 'ok'
      end
    from (
      select p.instrument_id, sum(p.quantity) as quantity
      from finance.postings p
      join finance.transactions t on t.id = p.transaction_id
      where p.account_id = :in_transit and t.status <> 'void' and t.booking_date <= {day}
        and not exists (
          select 1 from finance.transfers tr
          where t.id in (tr.out_transaction_id, tr.in_transaction_id,
                         tr.placeholder_transaction_id, tr.settlement_transaction_id)
            and (tr.from_account_id = any(:excluded) or tr.to_account_id = any(:excluded))
        )
      group by p.instrument_id
    ) b
    join finance.accounts a on a.id = :in_transit
    join finance.instruments i on i.id = b.instrument_id
    left join lateral (
      select 1::numeric as unit_price, i.id as price_currency_id
      where i.kind = 'fiat'
      union all
      (select pc.price, pc.quote_instrument_id
       from finance.prices pc
       where i.kind <> 'fiat' and pc.instrument_id = i.id and pc.as_of_date <= {day}
       order by pc.as_of_date desc, (pc.source = 'manual') desc, pc.recorded_at desc
       limit 1)
    ) pr on true
    cross join lateral (
      select finance.v1_fx_rate(
        pr.price_currency_id,
        (select ii.instrument_id
         from core.settings st
         join finance.instrument_identifiers ii
           on ii.scheme = 'iso4217' and ii.value = st.value #>> '{{}}'
         join finance.instruments ri on ri.id = ii.instrument_id and ri.kind = 'fiat'
         where st.key = 'finance.reporting_currency'),
        {day}
      ) as rate
    ) fx
    where not i.hidden and b.quantity <> 0
"""


def _shown(sql: str) -> TextClause:
    return _excluding(sql).bindparams(bindparam("in_transit", type_=PG_UUID(as_uuid=True)))


async def holdings_asof(
    session: AsyncSession, day: date, excluded: Collection[UUID] = ()
) -> list[HoldingRow]:
    """Holdings on the day, without those of the `excluded` accounts or what In transit holds
    for a transfer to or from one of them."""
    if excluded:
        rows = await session.execute(
            _shown(
                "select * from ("
                + _SHOWN_HOLDINGS.format(day="cast(:day as date)")
                + ") h order by account_name, symbol, account_id, instrument_id"
            ),
            {
                "day": day,
                "excluded": list(excluded),
                "in_transit": (await system_account_ids(session)).get("in_transit"),
            },
        )
    else:
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


def require_series_range(start: date, end: date) -> None:
    if end < start:
        raise FinanceValidationError("the series must not end before it starts")
    if (end - start).days + 1 > MAX_SERIES_DAYS:
        raise FinanceValidationError(f"a series covers at most {MAX_SERIES_DAYS} days")


# v1_net_worth_series over _SHOWN_HOLDINGS.
_NET_WORTH_EXCLUDING = (
    """
    select d.day,
      coalesce(sum(v.value) filter (where v.status = 'ok'), 0) as value,
      (count(*) filter (where v.status <> 'ok'))::int as missing_count
    from (select :start + n as day from generate_series(0, :end - :start) as n) d
    left join lateral (
"""
    + _SHOWN_HOLDINGS.format(day="d.day")
    + """
    ) v on true
    group by d.day
    order by d.day
"""
)


async def net_worth_series(
    session: AsyncSession, start: date, end: date, excluded: Collection[UUID] = ()
) -> list[NetWorthPoint]:
    """Net worth for every day from start to end, both included, valuing the holdings
    holdings_asof would give for `excluded`."""
    require_series_range(start, end)
    if excluded:
        rows = await session.execute(
            _shown(_NET_WORTH_EXCLUDING).bindparams(
                bindparam("start", type_=Date), bindparam("end", type_=Date)
            ),
            {
                "start": start,
                "end": end,
                "excluded": list(excluded),
                "in_transit": (await system_account_ids(session)).get("in_transit"),
            },
        )
    else:
        rows = await session.execute(
            text("select * from finance.v1_net_worth_series(:start, :end) order by day"),
            {"start": start, "end": end},
        )
    return [
        NetWorthPoint(day=row.day, value=row.value, missing_count=row.missing_count) for row in rows
    ]
