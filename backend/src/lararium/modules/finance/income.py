"""The Income page's report: what came in over a range, by period, category and entry.

It reads the same flows as the cash-flow report (`cashflow.flows`), keeps the positive ones
that are not refunds (a refund is negative spending there, never income)
and groups them. An entry is the positive flows of one transaction and instrument added up,
in the source's instrument (a reward of 0.00012 BTC is one entry) beside its value in the
reporting currency. An entry without a price or an FX rate is listed with no amount and left
out of every total, as the cash-flow report leaves out such a flow (income-categories design
§4.5).

Unlike the cash-flow report (and so the Overview's income card), this one does not share an
amount between its transaction's category splits: an entry has one effective category, and
the category totals are its entries added up, so they always agree with the entries listed.
A split income (a Monzo one the owner has not categorised) is therefore counted under its
category here and shared between its splits there, and the per-category numbers of the two
can differ while their totals agree.
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
from lararium.modules.finance.owner_categories import UNCATEGORISED, known_owner_categories


@dataclass(frozen=True)
class IncomeCategory:
    category: str | None  # None: neither the owner nor the source gave one
    amount: Decimal


@dataclass(frozen=True)
class IncomePeriod:
    start: date  # the period's first day; a week starts on Monday
    total: Decimal
    by_category: list[IncomeCategory]


@dataclass(frozen=True)
class IncomeEntry:
    transaction_id: UUID
    booking_date: date
    account_id: UUID | None
    account_name: str | None
    description: str
    counterparty_name: str | None
    quantity: Decimal  # in the instrument the source gave
    instrument: str  # its code: a currency or an asset's symbol
    amount: Decimal | None  # in the reporting currency; None without a price or FX rate
    source_category: str | None
    income_kind: str | None
    owner_category: str | None
    category: str | None  # the effective category


@dataclass(frozen=True)
class IncomeReport:
    start: date
    end: date
    bucket: Bucket
    total: Decimal
    missing_count: int  # entries left out of the totals for lack of a price or an FX rate
    periods: list[IncomePeriod]  # every period of the range, in order
    categories: list[IncomeCategory]  # largest first, then by name; none last
    entries: list[IncomeEntry]  # newest first, then by transaction id
    known_categories: list[str]  # every owner category in use, for the picker
    # What the Overview counts as income from hidden accounts and this page does not list.
    hidden_moved_in: Decimal


def _entries(positive: list[Flow]) -> list[IncomeEntry]:
    """One entry per transaction and instrument: the flows' quantities and amounts added."""
    groups: dict[tuple[UUID, UUID], list[Flow]] = defaultdict(list)
    for flow in positive:
        groups[flow.transaction_id, flow.instrument_id].append(flow)
    entries: list[IncomeEntry] = []
    for parts in groups.values():
        first = parts[0]
        # One part without a value leaves the whole entry without one: a partial sum would
        # read as the entry's worth.
        amount: Decimal | None = Decimal(0)
        for part in parts:
            amount = None if amount is None or part.amount is None else amount + part.amount
        entries.append(
            IncomeEntry(
                transaction_id=first.transaction_id,
                booking_date=first.day,
                account_id=first.account_id,
                account_name=first.account_name,
                description=first.description,
                counterparty_name=first.counterparty_name,
                quantity=sum((part.quantity for part in parts), Decimal(0)),
                instrument=first.instrument_code,
                amount=amount,
                source_category=first.source_category,
                income_kind=first.income_kind,
                owner_category=first.owner_category,
                category=first.category,
            )
        )
    # Newest first; two sorts, because the date reverses and the id does not.
    entries.sort(key=lambda e: e.transaction_id)
    entries.sort(key=lambda e: e.booking_date, reverse=True)
    return entries


def _by_amount(totals: dict[str | None, Decimal]) -> list[IncomeCategory]:
    ordered = sorted(totals.items(), key=lambda item: (-item[1], item[0] is None, item[0] or ""))
    return [IncomeCategory(category, amount) for category, amount in ordered]


async def income_report(
    session: AsyncSession,
    start: date,
    end: date,
    bucket: Bucket = "month",
    excluded: Collection[UUID] = (),
    account_id: UUID | None = None,
    category: str | None = None,
) -> IncomeReport:
    """What came in from start to end, both included.

    `account_id` narrows everything to one account's entries. `category` narrows the listed
    entries only, never the totals: "uncategorised" picks those with no category.
    """
    positive = [
        f for f in await flows(session, start, end, excluded) if f.quantity > 0 and not f.refund
    ]
    if account_id is not None:
        positive = [f for f in positive if f.account_id == account_id]
    entries = _entries(positive)

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

    hidden_moved_in = Decimal(0)
    if account_id is None:
        moved = await cash_flow(session, start, end, bucket, excluded)
        hidden_moved_in = sum((h.moved_in for h in moved.hidden_accounts), Decimal(0))

    return IncomeReport(
        start=start,
        end=end,
        bucket=bucket,
        total=sum(by_period.values(), Decimal(0)),
        missing_count=missing_count,
        periods=[
            IncomePeriod(period, by_period[period], _by_amount(by_period_category[period]))
            for period in periods_of(start, end, bucket)
        ],
        categories=_by_amount(by_category),
        entries=shown,
        known_categories=await known_owner_categories(session),
        hidden_moved_in=hidden_moved_in,
    )
