"""Which of two sources of one event posts it (design §5.7, Decision 14): the rule eToro's API
and statement share with Trading 212's API and CSV export.

The event posts once, under its key, from whichever sources are stored. When the API has it,
its values make the postings and the other source only enriches; when only the other has it,
the other's post. A **disagreement** is a difference between the two in a field that makes a
posting or its date; each opens one review item (source_reviews), both values kept. Taking the
other source's value ("statement") is the owner's decision, which holds for the values it was
made on, so regeneration replays it; a later change in either value reopens the review. An open
item whose field both sources state and agree on closes on its own.

An event is a frozen dataclass of the fields its connector compares (None: a source does not
state it); `together` names fields that follow one taken (a booking date takes its moment).
Values are compared as values (500.0 is 500) and kept as text; no message here names one.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import date
from decimal import Decimal
from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from lararium.core.commands import CommandContext
from lararium.modules.finance.money import to_plain_str
from lararium.modules.finance.source_reviews import (
    AGREED,
    Take,
    close_agreed,
    link_reviews,
    open_review,
)

if TYPE_CHECKING:
    from _typeshed import DataclassInstance

# (field, the API's value, the other source's value).
type Disagreement = tuple[str, str, str]


def review_text(value: object) -> str:
    """A value as a review keeps it: a number without trailing zeros, a date in ISO, text as
    it is."""
    if isinstance(value, Decimal):
        plain = to_plain_str(value.normalize())
        return "0" if plain in ("-0", "0") else plain
    if isinstance(value, date):
        return value.isoformat()
    return "" if value is None else str(value)


def disagreements(fields: Sequence[str], api: object, other: object) -> list[Disagreement]:
    """Each field the two sources state and differ in, in `fields`' order."""
    found: list[Disagreement] = []
    for field in fields:
        theirs, ours = getattr(api, field), getattr(other, field)
        if theirs is None or ours is None or theirs == ours:
            continue
        found.append((field, review_text(theirs), review_text(ours)))
    return found


def agreements(fields: Sequence[str], api: object, other: object) -> list[str]:
    """The fields both sources state and agree on: a field one leaves out agrees on nothing,
    so an open review of it stays open."""
    return [
        field
        for field in fields
        if (theirs := getattr(api, field)) is not None
        and (ours := getattr(other, field)) is not None
        and theirs == ours
    ]


def posting_values[E: DataclassInstance](
    api: E | None,
    other: E | None,
    fields: Sequence[str],
    takes: Mapping[str, Take],
    together: Mapping[str, Sequence[str]] | None = None,
) -> E:
    """The values the event posts: the API's, but each field the owner took from the other
    source (and the fields `together` ties to it); the other's when the API has none."""
    if api is None:
        if other is None:
            raise ValueError("an event needs a source")
        return other
    if other is None:
        return api
    taken: dict[str, object] = {}
    for field in fields:
        if takes.get(field) == "statement":
            for name in (field, *(together or {}).get(field, ())):
                taken[name] = getattr(other, name)
    return replace(api, **taken) if taken else api


async def decided_takes(
    session: AsyncSession, connection_id: UUID, key: str, found: Sequence[Disagreement]
) -> dict[str, Take]:
    """The owner's take on each disagreeing field, where the latest item they resolved for it
    had these very values; a field whose values changed since has none (its review reopens)."""
    if not found:
        return {}
    rows = await session.execute(
        text(
            "select d.field, d.take, r.api_value, r.statement_value"
            " from finance.source_decisions d"
            " join lateral (select api_value, statement_value from finance.source_reviews r"
            " where r.connection_id = d.connection_id and r.event_key = d.event_key"
            " and r.field = d.field and r.resolved_at is not null and r.resolved_by <> :agreed"
            " order by r.resolved_at desc, r.opened_at desc limit 1) r on true"
            " where d.connection_id = :c and d.event_key = :e"
        ),
        {"c": connection_id, "e": key, "agreed": AGREED},
    )
    decided = {row.field: (row.take, row.api_value, row.statement_value) for row in rows}
    result: dict[str, Take] = {}
    for field, theirs, ours in found:
        decision = decided.get(field)
        if decision is not None and decision[1:] == (theirs, ours) and decision[0] == "statement":
            result[field] = "statement"
    return result


async def decided_keys(session: AsyncSession, connection_id: UUID, prefix: str) -> set[str]:
    """The event keys starting `prefix` the owner has decided on: each run applies them, so a
    decision posts at the next run."""
    keys = await session.scalars(
        text(
            "select distinct event_key from finance.source_decisions"
            " where connection_id = :c and starts_with(event_key, :p)"
        ),
        {"c": connection_id, "p": prefix},
    )
    return set(keys)


@dataclass(frozen=True)
class Choice[E]:
    """The values an event posts; its disagreements, to review once posted; and the fields
    both sources state and agree on, whose open items close."""

    values: E
    found: list[Disagreement]
    agreed: list[str]


async def choose[E: DataclassInstance](
    session: AsyncSession,
    connection_id: UUID,
    key: str,
    api: E | None,
    other: E | None,
    fields: Sequence[str],
    together: Mapping[str, Sequence[str]] | None = None,
) -> Choice[E]:
    found: list[Disagreement] = []
    agreed: list[str] = []
    if api is not None and other is not None:
        found, agreed = disagreements(fields, api, other), agreements(fields, api, other)
    takes = await decided_takes(session, connection_id, key, found)
    return Choice(posting_values(api, other, fields, takes, together), found, agreed)


async def record_reviews[E](
    ctx: CommandContext,
    connection_id: UUID,
    key: str,
    transaction_id: UUID | None,
    choice: Choice[E],
) -> list[str]:
    """Link the event's review items to its transaction, close the open ones on fields the
    two sources now agree on, and open one for each disagreement the owner has not seen with
    these values. Returns the fields of the items it opened, to tell the owner once."""
    await link_reviews(ctx, connection_id, key, transaction_id)
    await close_agreed(ctx, connection_id, key, choice.agreed)
    opened: list[str] = []
    for field, theirs, ours in choice.found:
        if await open_review(ctx, connection_id, transaction_id, key, field, theirs, ours):
            opened.append(field)
    return opened
