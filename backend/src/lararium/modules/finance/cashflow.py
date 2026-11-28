"""Income and spending over a range of days, in the reporting currency.

Money comes in or goes out where a transaction posts to an account outside net worth: an
income, expense or equity account, which is also where a connector puts the unknown side of
what it reads (Unassigned). A posting there is the mirror of what happened to the owner's
money, so its negation is the flow: positive is income, negative is spending, except
that a positive flow the source marks as a refund (money back for an earlier payment) is
negative spending, counted in the period it is booked. A transfer
between own accounts posts to In transit, an asset, and a trade moves value inside one
account, so neither is a flow. Opening balances are where an account's history starts, not
income. Money counts, and an asset other than money only when the source states its
transaction is income of a kind (a coin reward, and its reversal), valued at the asset's
price on its booking day: a security arriving from nowhere (a corporate action, a transfer
in kind) or a coin spent on a fee is neither income nor spending.

An account left out of reports is treated as if it were someone else's: money moved between
it and an account still in the reports is a flow, income when it arrives and spending when it
leaves, so net worth's change and income less spending agree. These moves are counted in the
totals and periods but kept apart from categories and payees (`hidden_accounts`), as a
transfer to a hidden pension is saving, not a shop.

A category is whatever text decides it, not one of a fixed list: the owner's category for the
whole transaction; else, for income only, the kind of income the source states; else the
source's own category (income-categories design §4.2). `flows` is the one definition of a
flow, its category and its value, which every report reads.
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
from lararium.modules.finance.owner_categories import OWNER_CATEGORY_SQL
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
class Flow:
    """Income or spending: one posting outside net worth, negated."""

    transaction_id: UUID
    day: date  # the booking day, on which it is valued
    # The asset or liability account its transaction posts to (not In transit), first by
    # position: whose money it is. None when it posts to none (a transfer's settlement).
    account_id: UUID | None
    account_name: str | None
    instrument_id: UUID
    instrument_code: str
    quantity: Decimal  # in the instrument; positive is income
    amount: Decimal | None  # in the reporting currency; None without a price or FX rate
    category: str | None  # the effective category
    has_owner_category: bool  # the owner's category decides it, so splits don't apply
    source_category: str | None
    income_kind: str | None
    owner_category: str | None
    payee: str | None  # the counterparty, else the description
    description: str
    counterparty_name: str | None
    include_in_spending: bool | None
    refund: bool  # the source marks it money back for an earlier payment: negative spending


@dataclass(frozen=True)
class CashFlow:
    start: date
    end: date
    bucket: Bucket
    income: Decimal
    spending: Decimal  # positive: what went out
    net: Decimal  # income less spending
    missing_count: int  # flows left out for lack of a price or an FX rate
    periods: list[PeriodTotal]  # every period of the range, in order
    categories: list[CategoryTotal]  # largest spending first
    payees: list[PayeeTotal]  # the TOP_PAYEES largest, by spending
    hidden_accounts: list[HiddenAccountTotal]  # by name; their moves are in the totals too


# Every amount, and every split's share of one, is rounded once to this many places. Postgres
# gives a product all its digits: an inverse FX rate (1 / rate) has twenty, and times a coin's
# eight places and a price the amount is past Decimal's 28 significant digits; so is a third
# of an amount. Adding such amounts rounds, at different points when summing by period and by
# category, so the two would not add up to the same total. With a fixed scale every sum is
# exact while totals stay below 10**16.
_PLACES = 12
_SHARE_UNIT = Decimal(1).scaleb(-_PLACES)

# Each flow valued on its booking day, once per instrument and day, not once per posting:
# money at the day's FX rate, another asset as net worth values a holding (valuation.py), at
# its latest price on or before the day converted at the day's rate. The owner's category is
# read once per flow, in `base`, and the effective category decided from it in the final
# select (income-categories design §4.2).
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
    base as (
      select t.id as transaction_id, t.booking_date as day, p.position, p.instrument_id,
        -p.quantity as quantity, t.category as source_category, t.income_kind,
        """
    + OWNER_CATEGORY_SQL
    + """ as owner_category,
        coalesce(nullif(t.counterparty_name, ''), nullif(t.description, '')) as payee,
        t.description, t.counterparty_name, t.include_in_spending, t.refund
      from finance.postings p
      join finance.transactions t on t.id = p.transaction_id
      join finance.accounts a on a.id = p.account_id
      join finance.instruments i on i.id = p.instrument_id
      where t.status <> 'void' and t.booking_date between :start and :end
        and a.ledger_type in ('income', 'expense', 'equity')
        and a.id <> all(:skipped)
        and not i.hidden and p.quantity <> 0
        -- Another asset is income only when the source says what kind, whatever its sign:
        -- a reward and its reversal both count, as money does.
        and (i.kind = 'fiat' or t.income_kind is not null)
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
    valuations as (
      select k.instrument_id, k.day,
        pr.unit_price
          * finance.v1_fx_rate(pr.price_currency_id, (select reporting.id from reporting), k.day)
          as unit_value
      from (select distinct base.instrument_id, base.day from base) k
      join finance.instruments i on i.id = k.instrument_id
      left join lateral (
        select 1::numeric as unit_price, i.id as price_currency_id
        where i.kind = 'fiat'
        union all
        (select pc.price, pc.quote_instrument_id
         from finance.prices pc
         where i.kind <> 'fiat' and pc.instrument_id = i.id and pc.as_of_date <= k.day
         order by pc.as_of_date desc, (pc.source = 'manual') desc, pc.recorded_at desc
         limit 1)
      ) pr on true
    )
    select base.transaction_id, base.day, own.id as account_id, own.name as account_name,
      base.instrument_id, i.symbol as instrument_code, base.quantity,
      round(base.quantity * valuations.unit_value, """
    + str(_PLACES)
    + """) as amount,
      coalesce(base.owner_category,
               case when base.quantity > 0 and not base.refund then base.income_kind end,
               base.source_category) as category,
      base.owner_category is not null as has_owner_category,
      base.source_category, base.income_kind, base.owner_category, base.payee,
      base.description, base.counterparty_name, base.include_in_spending, base.refund
    from base
    join valuations
      on valuations.instrument_id = base.instrument_id and valuations.day = base.day
    join finance.instruments i on i.id = base.instrument_id
    left join lateral (
      select a.id, a.name
      from finance.postings ap
      join finance.accounts a on a.id = ap.account_id
      where ap.transaction_id = base.transaction_id
        and a.ledger_type in ('asset', 'liability') and a.id is distinct from :in_transit
      order by ap.position
      limit 1
    ) own on true
    order by base.day, base.transaction_id, base.position
"""
)


# Money crossing between the hidden accounts and those still in the reports: the postings on
# a shown asset or liability account (not In transit) of a transaction that either posts to a
# hidden account itself (a Monzo pot move) or is a side of a transfer to or from one (its own
# record, or the placeholder standing in for it), valued as _FLOWS values a flow. Its sign is
# the move's: positive arrived from the hidden account. A rejected transfer is no transfer,
# and its record's other side on Unassigned is a flow already.
_HIDDEN_MOVES = (
    "with"
    + _REPORTING
    + """,
    crossing as (
      select x.transaction_id, x.account_id as hidden_id
      from finance.postings x
      where x.account_id = any(:excluded)
      union
      select leg.id, case when tr.from_account_id = any(:excluded)
                       then tr.from_account_id else tr.to_account_id end
      from finance.transfers tr
      cross join lateral unnest(array[tr.out_transaction_id, tr.in_transaction_id,
                                      tr.placeholder_transaction_id]) as leg(id)
      where tr.status <> 'rejected' and leg.id is not null
        and (tr.from_account_id = any(:excluded) or tr.to_account_id = any(:excluded))
    ),
    -- One hidden account per transaction, should it touch several: the first by id.
    named as (
      select distinct on (crossing.transaction_id) crossing.transaction_id, crossing.hidden_id
      from crossing
      order by crossing.transaction_id, crossing.hidden_id
    ),
    moves as (
      select t.booking_date as day, named.hidden_id, p.instrument_id, p.quantity
      from named
      join finance.transactions t on t.id = named.transaction_id
      join finance.postings p on p.transaction_id = t.id
      join finance.accounts a on a.id = p.account_id
      join finance.instruments i on i.id = p.instrument_id
      where a.ledger_type in ('asset', 'liability')
        and a.id <> all(:excluded) and a.id is distinct from :in_transit
        and t.status <> 'void' and t.booking_date between :start and :end
        and i.kind = 'fiat' and not i.hidden and p.quantity <> 0
    ),
    rates as (
      select k.instrument_id, k.day,
        finance.v1_fx_rate(k.instrument_id, (select reporting.id from reporting), k.day)
          as fx_rate
      from (select distinct moves.instrument_id, moves.day from moves) k
    )
    select moves.day, moves.hidden_id, a.name,
      round(moves.quantity * rates.fx_rate, """
    + str(_PLACES)
    + """) as amount
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


def periods_of(start: date, end: date, bucket: Bucket) -> list[date]:
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
        share = (amount * quantity / total).quantize(_SHARE_UNIT)
        shares.append((name, share))
        left -= share
    shares.append((splits[-1][0], left))
    return shares


async def flows(
    session: AsyncSession, start: date, end: date, excluded: Collection[UUID]
) -> list[Flow]:
    """Every flow from start to end, both included, by day.

    A transaction touching one of the `excluded` accounts is left out whole, and so is the
    settlement of a transfer to or from one, which touches neither side. What a transfer to
    or from a hidden account moved is not a flow here (cash_flow's hidden_accounts).
    """
    require_series_range(start, end)
    system = await system_account_ids(session)
    opening = system.get("opening_balances")
    uuids = ARRAY(PG_UUID(as_uuid=True))
    rows = await session.execute(
        text(_FLOWS).bindparams(
            bindparam("skipped", type_=uuids),
            bindparam("excluded", type_=uuids),
            bindparam("in_transit", type_=PG_UUID(as_uuid=True)),
        ),
        {
            "start": start,
            "end": end,
            "skipped": [] if opening is None else [opening],
            "excluded": list(excluded),
            "in_transit": system.get("in_transit"),
        },
    )
    return [Flow(**row) for row in rows.mappings()]


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
    entries = await flows(session, start, end, excluded)
    # The owner's category covers the whole transaction, so its splits are not read.
    splits = await _splits(session, {e.transaction_id for e in entries if not e.has_owner_category})
    system = await system_account_ids(session)
    uuids = ARRAY(PG_UUID(as_uuid=True))
    moves = (
        await session.execute(
            text(_HIDDEN_MOVES).bindparams(
                bindparam("excluded", type_=uuids),
                bindparam("in_transit", type_=PG_UUID(as_uuid=True)),
            ),
            {
                "start": start,
                "end": end,
                "excluded": list(excluded),
                "in_transit": system.get("in_transit"),
            },
        )
    ).all()

    zero = Decimal(0)
    income_by_period: dict[date, Decimal] = defaultdict(Decimal)
    spending_by_period: dict[date, Decimal] = defaultdict(Decimal)
    income_by_category: dict[str | None, Decimal] = defaultdict(Decimal)
    spending_by_category: dict[str | None, Decimal] = defaultdict(Decimal)
    spending_by_payee: dict[str, Decimal] = defaultdict(Decimal)
    missing_count = 0
    for entry in entries:
        amount = entry.amount
        if amount is None:
            missing_count += 1
            continue
        period = period_start(entry.day, bucket)
        shares = _by_category(amount, entry.category, splits[entry.transaction_id])
        if amount > 0 and not entry.refund:
            income_by_period[period] += amount
            for category, share in shares:
                income_by_category[category] += share
        elif entry.include_in_spending is not False:
            spending_by_period[period] -= amount
            for category, share in shares:
                spending_by_category[category] -= share
            if entry.payee is not None:
                spending_by_payee[entry.payee] -= amount

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
            for period in periods_of(start, end, bucket)
        ],
        categories=categories,
        payees=[PayeeTotal(name, spending) for name, spending in payees[:TOP_PAYEES]],
        hidden_accounts=list(hidden.values()),
    )
