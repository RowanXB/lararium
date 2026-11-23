"""Which source posts an eToro trade event both the API and the statement have (design §5.7,
Decision 14).

An open or a close posts once, under its key (`open:` / `close:<positionId>`), from whichever
sources are stored. When the API has the event its values make the postings and the
statement's only enrich; when only the statement has it, the statement's post, and the API's
later arrival re-posts the same transaction in place.

A **disagreement** is a difference between the two in a value that makes a posting or its date:
the units, the cash amount, the profit, or the booking date (after the UTC rule, design §5.8).
Each opens one review item on the transaction, both values kept, and is told to the owner once
(`notify`). Other shared fields (names, rates, spreads) are kept from both without review.

Resolving an item is the owner's decision (source_reviews): taking the statement's value makes
that field post from the statement. A decision holds for the values it was made on, so
regeneration replays it; a later change in either source's value reopens the review, and the
API's value posts until the owner decides again. An open item whose two sources come to agree
closes on its own, resolved `api` by "sources agree", untold.

Messages name a position by its last four digits and the field, never a value.
"""

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import text

from lararium.core.connectors import SyncContext
from lararium.modules.finance.etoro.state import Run, ending
from lararium.modules.finance.money import to_plain_str
from lararium.modules.finance.source_reviews import AGREED, Take, close_agreed, open_review

logger = logging.getLogger(__name__)

# The fields a disagreement is about, in the order reviews name them.
FIELDS = ("units", "cash", "profit", "booking_date")


@dataclass(frozen=True)
class TradeEvent:
    """What one source says of an open or a close: its units and cash (magnitudes; the
    posting gives the signs), the profit a close made (None for an open, or unstated), and
    when it happened."""

    units: Decimal
    cash: Decimal
    profit: Decimal | None
    booking_date: date
    occurred_at: datetime


def _text(value: Decimal | date | None) -> str:
    """A value as a review keeps it: a number without trailing zeros, a date in ISO."""
    if isinstance(value, Decimal):
        plain = to_plain_str(value.normalize())
        return "0" if plain in ("-0", "0") else plain
    return "" if value is None else value.isoformat()


def disagreements(api: TradeEvent, statement: TradeEvent) -> list[tuple[str, str, str]]:
    """(field, the API's value, the statement's value) for each field they differ in. Numbers
    are compared as numbers (500.0 is 500); a profit only one source states is no
    disagreement."""
    found: list[tuple[str, str, str]] = []
    for field in FIELDS:
        theirs, ours = getattr(api, field), getattr(statement, field)
        if theirs is None or ours is None or theirs == ours:
            continue
        found.append((field, _text(theirs), _text(ours)))
    return found


def posting_values(
    api: TradeEvent | None, statement: TradeEvent | None, takes: Mapping[str, Take]
) -> TradeEvent:
    """The values the event posts: the API's, but for each field the owner took from the
    statement; the statement's when the API has none. Taking the statement's booking date
    takes its moment too."""
    if api is None:
        if statement is None:
            raise ValueError("an event needs a source")
        return statement
    if statement is None:
        return api

    def pick[T](field: str, theirs: T, ours: T) -> T:
        return ours if takes.get(field) == "statement" else theirs

    return TradeEvent(
        units=pick("units", api.units, statement.units),
        cash=pick("cash", api.cash, statement.cash),
        profit=pick("profit", api.profit, statement.profit),
        booking_date=pick("booking_date", api.booking_date, statement.booking_date),
        occurred_at=pick("booking_date", api.occurred_at, statement.occurred_at),
    )


# Against the ledger --------------------------------------------------------------------------


async def takes(run: Run, key: str, found: Sequence[tuple[str, str, str]]) -> dict[str, Take]:
    """The owner's take on each disagreeing field, where the latest item they resolved for it
    had these very values; a field whose values changed since has none (its review reopens)."""
    if not found:
        return {}
    rows = await run.session.execute(
        text(
            "select d.field, d.take, r.api_value, r.statement_value"
            " from finance.source_decisions d"
            " join lateral (select api_value, statement_value from finance.source_reviews r"
            " where r.connection_id = d.connection_id and r.event_key = d.event_key"
            " and r.field = d.field and r.resolved_at is not null and r.resolved_by <> :agreed"
            " order by r.resolved_at desc, r.opened_at desc limit 1) r on true"
            " where d.connection_id = :c and d.event_key = :e"
        ),
        {"c": run.connection_id, "e": key, "agreed": AGREED},
    )
    decided = {row.field: (row.take, row.api_value, row.statement_value) for row in rows}
    result: dict[str, Take] = {}
    for field, theirs, ours in found:
        decision = decided.get(field)
        if decision is not None and decision[1:] == (theirs, ours) and decision[0] == "statement":
            result[field] = "statement"
    return result


@dataclass(frozen=True)
class Choice:
    """The values an event posts; its disagreements, to review once posted; and whether both
    sources had it (so the fields they agree on can close their items)."""

    values: TradeEvent
    found: list[tuple[str, str, str]]
    both: bool


async def choose(
    run: Run, key: str, api: TradeEvent | None, statement: TradeEvent | None
) -> Choice:
    both = api is not None and statement is not None
    found = disagreements(api, statement) if api is not None and statement is not None else []
    return Choice(posting_values(api, statement, await takes(run, key, found)), found, both)


async def review(run: Run, key: str, transaction_id: UUID | None, choice: Choice) -> None:
    """Open a review item for each disagreement the owner has not seen with these values, and
    close the open ones on fields the two sources now agree on."""
    if choice.both:
        disagreeing = {field for field, _, _ in choice.found}
        await close_agreed(
            run.command, run.connection_id, key, [f for f in FIELDS if f not in disagreeing]
        )
    for field, theirs, ours in choice.found:
        if await open_review(
            run.command, run.connection_id, transaction_id, key, field, theirs, ours
        ):
            run.reviews.append((key, field))


async def decided_positions(run: Run) -> set[str]:
    """The positions whose events the owner has decided on: each run applies them, so a
    decision posts at the next sync."""
    keys = await run.session.scalars(
        text(
            "select distinct event_key from finance.source_decisions where connection_id = :c"
            " and (event_key like 'open:%' or event_key like 'close:%')"
        ),
        {"c": run.connection_id},
    )
    return {key.split(":", 1)[1] for key in keys}


async def notify(ctx: SyncContext, display_name: str, opened: Sequence[tuple[str, str]]) -> None:
    """Tell the owner of the review items this run opened, once; a failed send is logged by
    its class (its message could carry the notifier's address)."""
    if not opened:
        return
    named = ", ".join(
        f"{key.split(':', 1)[0]} of position {ending(key.split(':', 1)[1])} ({field})"
        for key, field in opened
    )
    try:
        await ctx.notifier.send(
            f"{display_name}: eToro's API and statement disagree on {named}; review them"
            " on the connection"
        )
    except Exception as error:
        logger.warning("sending an eToro review notification failed (%s)", type(error).__name__)
