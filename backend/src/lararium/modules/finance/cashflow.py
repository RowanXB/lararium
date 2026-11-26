"""Income and spending over a range of days, in the reporting currency.

Money comes in or goes out where a transaction posts to an account outside net worth: an
income, expense or equity account, which is also where a connector puts the unknown side of
what it reads (Unassigned). A posting there is the mirror of what happened to the owner's
money, so its negation is the flow: positive is income, negative is spending. A transfer
between own accounts posts to In transit, an asset, and a trade moves value inside one
account, so neither is a flow. Opening balances are where an account's history starts, not
income. Only money counts: a security arriving from nowhere (a corporate action, a transfer
in kind) is not income.

An account left out of reports is treated as if it were someone else's: money moved between
it and an account still in the reports is a flow, income when it arrives and spending when it
leaves, so net worth's change and income less spending agree. These moves are counted in the
totals and periods but kept apart from categories and payees (`hidden_accounts`), as a
transfer to a hidden pension is saving, not a shop.

A category is whatever text the transaction carries, not one of a fixed list, so categories
the owner defines or a rule assigns later need no change to what this returns.
"""

from collections import defaultdict
from collections.abc import Collection
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from typing import Literal
from uuid import UUID

from sqlalchemy import any_, bindparam, select, text
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.ext.asyncio import AsyncSession

from lararium.modules.finance import models
from lararium.modules.finance.system_accounts import system_account_ids
from lararium.modules.finance.valuation import require_series_range

Bucket = Literal["day", "week", "month"]

TOP_PAYEES = 10


@dataclass(frozen=True)
class PeriodTotal:
    start: date  # the period's first day; a week starts on Monday
    income: Decimal
    spending: Decimal


@dataclass(frozen=True)
class CategoryTotal:
    category: str | None  # None: the source gave no category
    income: Decimal
    spending: Decimal


@dataclass(frozen=True)
class PayeeTotal:
    name: str
    spending: Decimal


@dataclass(frozen=True)
class HiddenAccountTotal:
    """Money moved between one account left out of reports and those still in them."""

    account_id: UUID
    name: str
    moved_in: Decimal  # from the hidden account into the reports: income
    moved_out: Decimal  # from the reports into the hidden account: spending


@dataclass(frozen=True)
class CashFlow:
    start: date
    end: date
    bucket: Bucket
    income: Decimal
    spending: Decimal  # positive: what went out
    net: Decimal  # income less spending
    missing_count: int  # flows left out for lack of an FX rate
    periods: list[PeriodTotal]  # every period of the range, in order
    categories: list[CategoryTotal]  # largest spending first
    payees: list[PayeeTotal]  # the TOP_PAYEES largest, by spending
    hidden_accounts: list[HiddenAccountTotal]  # by name; their moves are in the totals too


# Each flow valued on its booking day. The FX rate is looked up once per currency and day,
# not once per posting. `category` is the one place a flow's category is decided: today the
# source's own, later the owner's override or an automatic one before it.
_REPORTING = """
    reporting as (
      select i.id
      from core.settings st
      join finance.instrument_identifiers ii
        on ii.scheme = 'iso4217' and ii.value = st.value #>> '{}'
      join finance.instruments i on i.id = ii.instrument_id and i.kind = 'fiat'
      where st.key = 'finance.reporting_currency'
    )
"""

_FLOWS = (
    "with"
    + _REPORTING
    + """,
    flows as (
      select t.id as transaction_id, t.booking_date as day, t.category,
        coalesce(nullif(t.counterparty_name, ''), nullif(t.description, '')) as payee,
        t.include_in_spending, p.instrument_id, -p.quantity as quantity
      from finance.postings p
      join finance.transactions t on t.id = p.transaction_id
      join finance.accounts a on a.id = p.account_id
      join finance.instruments i on i.id = p.instrument_id
      where t.status <> 'void' and t.booking_date between :start and :end
        and a.ledger_type in ('income', 'expense', 'equity')
        and a.id <> all(:skipped)
        and i.kind = 'fiat' and not i.hidden and p.quantity <> 0
        and not exists (
          select 1 from finance.postings x
          where x.transaction_id = t.id and x.account_id = any(:excluded)
        )
        -- A transfer's settlement (its fee, or its conversion) posts only to In transit and
        -- Unassigned; it belongs to the transfer's two accounts.
        and not exists (
          select 1 from finance.transfers tr
          where tr.settlement_transaction_id = t.id
            and (tr.from_account_id = any(:excluded) or tr.to_account_id = any(:excluded))
        )
    ),
    rates as (
      select k.instrument_id, k.day,
        finance.v1_fx_rate(k.instrument_id, (select reporting.id from reporting), k.day)
          as fx_rate
      from (select distinct flows.instrument_id, flows.day from flows) k
    )
    select flows.transaction_id, flows.day, flows.category, flows.payee,
      flows.include_in_spending, flows.quantity * rates.fx_rate as amount
    from flows
    join rates on rates.instrument_id = flows.instrument_id and rates.day = flows.day
    order by flows.day, flows.transaction_id
"""
)


# The side of each transfer between a hidden account and one still in the reports that is on
# the latter: its own record, or the placeholder standing in for it, valued as _FLOWS values
# a flow. Its sign is the move's: positive arrived from the hidden account. A rejected
# transfer is no transfer, and its record's other side on Unassigned is a flow already.
_HIDDEN_MOVES = (
    "with"
    + _REPORTING
    + """,
    sides as (
      select tr.id,
        case when tr.from_account_id = any(:excluded) then tr.from_account_id
          else tr.to_account_id end as hidden_id,
        case when tr.from_account_id = any(:excluded) then tr.to_account_id
          else tr.from_account_id end as shown_id,
        array[tr.out_transaction_id, tr.in_transaction_id, tr.placeholder_transaction_id]
          as legs
      from finance.transfers tr
      where tr.status <> 'rejected'
        and (tr.from_account_id = any(:excluded)) <> (tr.to_account_id = any(:excluded))
    ),
    moves as (
      select t.booking_date as day, sides.hidden_id, p.instrument_id, p.quantity
      from sides
      join finance.transactions t on t.id = any(sides.legs)
      join finance.postings p on p.transaction_id = t.id and p.account_id = sides.shown_id
      join finance.instruments i on i.id = p.instrument_id
      where t.status <> 'void' and t.booking_date between :start and :end
        and i.kind = 'fiat' and not i.hidden and p.quantity <> 0
    ),
    rates as (
      select k.instrument_id, k.day,
        finance.v1_fx_rate(k.instrument_id, (select reporting.id from reporting), k.day)
          as fx_rate
      from (select distinct moves.instrument_id, moves.day from moves) k
    )
    select moves.day, moves.hidden_id, a.name, moves.quantity * rates.fx_rate as amount
    from moves
    join finance.accounts a on a.id = moves.hidden_id
    join rates on rates.instrument_id = moves.instrument_id and rates.day = moves.day
    order by a.name, a.id, moves.day
"""
)


def period_start(day: date, bucket: Bucket) -> date:
    if bucket == "month":
        return day.replace(day=1)
    if bucket == "week":
        return day - timedelta(days=day.weekday())
    return day


def _next_period(start: date, bucket: Bucket) -> date | None:
    """The first day of the period after the one starting on `start`; None past the last day."""
    try:
        if bucket == "month":
            return date(start.year + start.month // 12, start.month % 12 + 1, 1)
        return start + timedelta(days=7 if bucket == "week" else 1)
    except (OverflowError, ValueError):
        return None


def _periods(start: date, end: date, bucket: Bucket) -> list[date]:
    periods = [period_start(start, bucket)]
    while (following := _next_period(periods[-1], bucket)) is not None and following <= end:
        periods.append(following)
    return periods


async def _splits(
    session: AsyncSession, transaction_ids: Collection[UUID]
) -> dict[UUID, list[tuple[str, Decimal]]]:
    """Each transaction's category splits, in order."""
    s = models.TransactionCategorySplit
    rows = await session.execute(
        select(s.transaction_id, s.category, s.quantity)
        # One array parameter, not one per id: a long range can hold more transactions than
        # a statement takes parameters.
        .where(
            s.transaction_id
            == any_(bindparam("ids", list(transaction_ids), type_=ARRAY(PG_UUID(as_uuid=True))))
        )
        .order_by(s.transaction_id, s.position)
    )
    splits: dict[UUID, list[tuple[str, Decimal]]] = defaultdict(list)
    for row in rows:
        splits[row.transaction_id].append((row.category, row.quantity))
    return splits


def _by_category(
    amount: Decimal, category: str | None, splits: list[tuple[str, Decimal]]
) -> list[tuple[str | None, Decimal]]:
    """The amount shared between its transaction's category splits, in their proportions;
    the last takes what rounding leaves, so the shares add up to the amount exactly."""
    total = sum((quantity for _, quantity in splits), Decimal(0))
    if not splits or total == 0:
        return [(category, amount)]
    shares: list[tuple[str | None, Decimal]] = []
    left = amount
    for name, quantity in splits[:-1]:
        share = amount * quantity / total
        shares.append((name, share))
        left -= share
    shares.append((splits[-1][0], left))
    return shares


async def cash_flow(
    session: AsyncSession,
    start: date,
    end: date,
    bucket: Bucket = "month",
    excluded: Collection[UUID] = (),
) -> CashFlow:
    """Income and spending from start to end, both included.

    A transaction touching one of the `excluded` accounts is left out whole, and so is the
    settlement of a transfer to or from one, which touches neither side; what such a transfer
    moved on the side still in the reports counts instead (hidden_accounts). Spending the
    source itself marks as not spending (include_in_spending false) is left out too.
    """
    require_series_range(start, end)
    opening = (await system_account_ids(session)).get("opening_balances")
    skipped = [] if opening is None else [opening]
    uuids = ARRAY(PG_UUID(as_uuid=True))
    rows = (
        await session.execute(
            text(_FLOWS).bindparams(
                bindparam("skipped", type_=uuids), bindparam("excluded", type_=uuids)
            ),
            {"start": start, "end": end, "skipped": skipped, "excluded": list(excluded)},
        )
    ).all()
    splits = await _splits(session, {row.transaction_id for row in rows})
    moves = (
        await session.execute(
            text(_HIDDEN_MOVES).bindparams(bindparam("excluded", type_=uuids)),
            {"start": start, "end": end, "excluded": list(excluded)},
        )
    ).all()

    zero = Decimal(0)
    income_by_period: dict[date, Decimal] = defaultdict(Decimal)
    spending_by_period: dict[date, Decimal] = defaultdict(Decimal)
    income_by_category: dict[str | None, Decimal] = defaultdict(Decimal)
    spending_by_category: dict[str | None, Decimal] = defaultdict(Decimal)
    spending_by_payee: dict[str, Decimal] = defaultdict(Decimal)
    missing_count = 0
    for row in rows:
        amount: Decimal | None = row.amount
        if amount is None:
            missing_count += 1
            continue
        period = period_start(row.day, bucket)
        if amount > 0:
            income_by_period[period] += amount
            for category, share in _by_category(amount, row.category, splits[row.transaction_id]):
                income_by_category[category] += share
        elif row.include_in_spending is not False:
            spending_by_period[period] -= amount
            for category, share in _by_category(amount, row.category, splits[row.transaction_id]):
                spending_by_category[category] -= share
            if row.payee is not None:
                spending_by_payee[row.payee] -= amount

    hidden: dict[UUID, HiddenAccountTotal] = {}
    for move in moves:
        amount = move.amount
        if amount is None:
            missing_count += 1
            continue
        period = period_start(move.day, bucket)
        total = hidden.get(move.hidden_id) or HiddenAccountTotal(
            move.hidden_id, move.name, zero, zero
        )
        if amount > 0:
            income_by_period[period] += amount
            total = HiddenAccountTotal(
                total.account_id, total.name, total.moved_in + amount, total.moved_out
            )
        else:
            spending_by_period[period] -= amount
            total = HiddenAccountTotal(
                total.account_id, total.name, total.moved_in, total.moved_out - amount
            )
        hidden[move.hidden_id] = total

    categories = [
        CategoryTotal(category, income_by_category[category], spending_by_category[category])
        for category in income_by_category.keys() | spending_by_category.keys()
    ]
    categories.sort(key=lambda c: (-c.spending, -c.income, c.category or ""))
    payees = sorted(spending_by_payee.items(), key=lambda item: (-item[1], item[0]))
    income = sum(income_by_period.values(), zero)
    spending = sum(spending_by_period.values(), zero)
    return CashFlow(
        start=start,
        end=end,
        bucket=bucket,
        income=income,
        spending=spending,
        net=income - spending,
        missing_count=missing_count,
        periods=[
            PeriodTotal(period, income_by_period[period], spending_by_period[period])
            for period in _periods(start, end, bucket)
        ],
        categories=categories,
        payees=[PayeeTotal(name, spending) for name, spending in payees[:TOP_PAYEES]],
        hidden_accounts=list(hidden.values()),
    )
