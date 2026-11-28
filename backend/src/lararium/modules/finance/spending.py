"""The Spending page's report: what went out over a range, by period, category and entry.

It mirrors the Income page's report (income.py) on the other side of `cashflow.flows`: the
flows the cash-flow report counts as spending, a negative flow or a positive one the source
marks as a refund, unless the source marks it as not spending (`include_in_spending` false).
An entry is the flows of one transaction and instrument added up, signed as spending: what
went out is positive and a refund is negative, so a category's total can be negative when its
refunds outweigh its payments. An entry without a price or an FX rate is listed with no
amount and left out of every total, as the cash-flow report leaves out such a flow
(spending-page design §3.1).

As on the Income page, an entry has one effective category and does not share its amount
between its transaction's category splits, so the category totals are its entries added up.
"""

from collections import defaultdict
from collections.abc import Collection
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from lararium.modules.finance.cashflow import (
    Bucket,
    Flow,
    cash_flow,
    flows,
    period_start,
    periods_of,
)
from lararium.modules.finance.income import by_amount
from lararium.modules.finance.owner_categories import UNCATEGORISED, known_owner_categories


@dataclass(frozen=True)
class SpendingCategory:
    category: str | None  # None: neither the owner nor the source gave one
    amount: Decimal


@dataclass(frozen=True)
class SpendingPeriod:
    start: date  # the period's first day; a week starts on Monday
    total: Decimal
    by_category: list[SpendingCategory]


@dataclass(frozen=True)
class SpendingEntry:
    transaction_id: UUID
    booking_date: date
    account_id: UUID | None
    account_name: str | None
    description: str
    counterparty_name: str | None
    quantity: Decimal  # in the instrument the source gave; positive went out
    instrument: str  # its code: a currency or an asset's symbol
    amount: Decimal | None  # in the reporting currency; None without a price or FX rate
    source_category: str | None
    owner_category: str | None
    category: str | None  # the effective category
    refund: bool  # the source marks it money back, so its amount is negative


@dataclass(frozen=True)
class SpendingReport:
    start: date
    end: date
    bucket: Bucket
    total: Decimal
    missing_count: int  # entries left out of the totals for lack of a price or an FX rate
    periods: list[SpendingPeriod]  # every period of the range, in order
    categories: list[SpendingCategory]  # largest first, then by name; none last
    entries: list[SpendingEntry]  # newest first, then by transaction id
    known_categories: list[str]  # every owner category in use on this side, for the picker
    # What the Overview counts as spending to hidden accounts and this page does not list.
    hidden_moved_out: Decimal


def _entries(spent: list[Flow]) -> list[SpendingEntry]:
    """One entry per transaction and instrument: the flows' quantities and amounts added,
    negated, so what went out is positive."""
    groups: dict[tuple[UUID, UUID], list[Flow]] = defaultdict(list)
    for flow in spent:
        groups[flow.transaction_id, flow.instrument_id].append(flow)
    entries: list[SpendingEntry] = []
    for parts in groups.values():
        first = parts[0]
        # One part without a value leaves the whole entry without one: a partial sum would
        # read as the entry's worth.
        amount: Decimal | None = Decimal(0)
        for part in parts:
            amount = None if amount is None or part.amount is None else amount - part.amount
        entries.append(
            SpendingEntry(
                transaction_id=first.transaction_id,
                booking_date=first.day,
                account_id=first.account_id,
                account_name=first.account_name,
                description=first.description,
                counterparty_name=first.counterparty_name,
                quantity=-sum((part.quantity for part in parts), Decimal(0)),
                instrument=first.instrument_code,
                amount=amount,
                source_category=first.source_category,
                owner_category=first.owner_category,
                category=first.category,
                refund=first.refund,
            )
        )
    # Newest first; two sorts, because the date reverses and the id does not.
    entries.sort(key=lambda e: e.transaction_id)
    entries.sort(key=lambda e: e.booking_date, reverse=True)
    return entries


def _by_amount(totals: dict[str | None, Decimal]) -> list[SpendingCategory]:
    return [SpendingCategory(category, amount) for category, amount in by_amount(totals)]


async def spending_report(
    session: AsyncSession,
    start: date,
    end: date,
    bucket: Bucket = "month",
    excluded: Collection[UUID] = (),
    account_id: UUID | None = None,
    category: str | None = None,
) -> SpendingReport:
    """What went out from start to end, both included.

    `account_id` narrows everything to one account's entries. `category` narrows the listed
    entries only, never the totals: "uncategorised" picks those with no category.
    """
    spent = [
        f
        for f in await flows(session, start, end, excluded)
        if (f.quantity < 0 or f.refund) and f.include_in_spending is not False
    ]
    if account_id is not None:
        spent = [f for f in spent if f.account_id == account_id]
    entries = _entries(spent)

    by_period: dict[date, Decimal] = defaultdict(Decimal)
    by_period_category: dict[date, dict[str | None, Decimal]] = defaultdict(
        lambda: defaultdict(Decimal)
    )
    by_category: dict[str | None, Decimal] = defaultdict(Decimal)
    missing_count = 0
    for entry in entries:
        if entry.amount is None:
            missing_count += 1
            continue
        period = period_start(entry.booking_date, bucket)
        by_period[period] += entry.amount
        by_period_category[period][entry.category] += entry.amount
        by_category[entry.category] += entry.amount

    if category is not None:
        wanted = None if category == UNCATEGORISED else category
        shown = [e for e in entries if e.category == wanted]
    else:
        shown = entries

    hidden_moved_out = Decimal(0)
    if account_id is None:
        moved = await cash_flow(session, start, end, bucket, excluded)
        hidden_moved_out = sum((h.moved_out for h in moved.hidden_accounts), Decimal(0))

    return SpendingReport(
        start=start,
        end=end,
        bucket=bucket,
        total=sum(by_period.values(), Decimal(0)),
        missing_count=missing_count,
        periods=[
            SpendingPeriod(period, by_period[period], _by_amount(by_period_category[period]))
            for period in periods_of(start, end, bucket)
        ],
        categories=_by_amount(by_category),
        entries=shown,
        known_categories=await known_owner_categories(session, "spending"),
        hidden_moved_out=hidden_moved_out,
    )
