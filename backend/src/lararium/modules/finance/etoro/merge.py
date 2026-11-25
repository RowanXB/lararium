"""Which source posts an eToro trade event both the API and the statement have (design §5.7,
Decision 14).

An open or a close posts once, under its key (`open:` / `close:<positionId>`), from whichever
sources are stored. When the API has the event its values make the postings and the
statement's only enrich; when only the statement has it, the statement's post, and the API's
later arrival re-posts the same transaction in place.

A **disagreement** is a difference between the two in a value that makes a posting or its date:
the units, the cash amount, the profit, or the booking date (after the UTC rule, design §5.8).
Only like facts are reviewed (design §5.7): an open's units and cash as the position was
opened (PnL, the "Open Position" row) and as a partial close left them (the part closed:
trade history, Closed Positions) are different facts, so while the sources are at those
different stages their units and cash open and close no review and tell nobody; they are
reviewed once both describe the same. What closed is the whole position unless a source
shows a partial close, so a full close's trade history is compared with the "Open Position"
row at once. The booking date is the same fact at every stage and always compared. What posts,
an owner's decision included, does not depend on the stage.
Each opens one review item on the transaction, both values kept, and is told to the owner once
(`notify`). Other shared fields (names, rates, spreads) are kept from both without review.

Resolving an item is the owner's decision (source_reviews): taking the statement's value makes
that field post from the statement. A decision holds for the values it was made on, so
regeneration replays it; a later change in either source's value reopens the review, and the
API's value posts until the owner decides again. An open item whose two sources come to agree
closes on its own, resolved `api` by "sources agree", untold; only a field both sources
state counts, so one a source stops stating keeps its item open. The profit makes no posting
(a close posts its units and cash), so taking the statement's profit changes no posting; it
settles the item.

Messages name a position by its last four digits and the field, never a value.
"""

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Literal
from uuid import UUID

from lararium.core.connectors import SyncContext
from lararium.core.json_types import JSONValue
from lararium.modules.finance import source_merge
from lararium.modules.finance.etoro.state import Run, ending
from lararium.modules.finance.source_merge import Choice

# The fields a disagreement is about, in the order reviews name them.
FIELDS = ("units", "cash", "profit", "booking_date")
# Taking the statement's booking date takes its moment too.
TOGETHER = {"booking_date": ("occurred_at",)}


# What an open's or close's figures describe: the position as opened; what closed, being
# the whole position; or what closed, a part (a partial close) or not known to be the whole.
type Describes = Literal["opened", "closed_whole", "closed_part"]
# The fields whose figures depend on that; the booking date does not.
_STAGED = frozenset({"units", "cash"})


@dataclass(frozen=True)
class TradeEvent:
    """What one source says of an open or a close: its units and cash (magnitudes; the
    posting gives the signs), the profit a close made (None for an open, or unstated), when
    it happened, and what it describes (`comparable` reads it)."""

    units: Decimal
    cash: Decimal
    profit: Decimal | None
    booking_date: date
    occurred_at: datetime
    describes: Describes


# Against the ledger --------------------------------------------------------------------------


def comparable(api: TradeEvent, statement: TradeEvent) -> bool:
    """Whether the two describe the same fact (design §5.7): both the position as opened, both what
    closed, or one of each when what closed is the whole position."""
    kinds = {api.describes, statement.describes}
    return (
        len(kinds) == 1
        or kinds == {"closed_whole", "closed_part"}
        or (kinds == {"opened", "closed_whole"})
    )


async def choose(
    run: Run, key: str, api: TradeEvent | None, statement: TradeEvent | None
) -> Choice[TradeEvent]:
    """The values the event posts (an owner's decision applied as at every stage), and what
    the two sources disagree and agree on. Two that describe unlike facts leave units and
    cash unreviewed: no review on them is opened or closed until both describe the same."""
    choice = await source_merge.choose(
        run.session, run.connection_id, key, api, statement, FIELDS, TOGETHER
    )
    if api is None or statement is None or comparable(api, statement):
        return choice
    return Choice(
        choice.values,
        [d for d in choice.found if d[0] not in _STAGED],
        [f for f in choice.agreed if f not in _STAGED],
    )


async def review(run: Run, key: str, transaction_id: UUID, choice: Choice[TradeEvent]) -> None:
    """Link the event's review items to its transaction, close the open ones on fields the
    two sources now agree on, and open one for each disagreement the owner has not seen with
    these values."""
    await source_merge.record_reviews(run.command, run.connection_id, key, transaction_id, choice)


async def decided_positions(run: Run) -> set[str]:
    """The positions whose events the owner has decided on: each run applies them, so a
    decision posts at the next sync."""
    keys: set[str] = set()
    for prefix in ("open:", "close:"):
        keys |= await source_merge.decided_keys(run.session, run.connection_id, prefix)
    return {key.split(":", 1)[1] for key in keys}


async def notify(ctx: SyncContext, display_name: str) -> dict[str, JSONValue]:
    """Tell the owner of the open review items not told yet, once (source_merge); returns
    the cursor's record of those told."""
    return await source_merge.notify_reviews(
        ctx,
        display_name,
        sources="eToro's API and statement",
        name=lambda key: f"{key.split(':', 1)[0]} of position {ending(key.split(':', 1)[1])}",
        label="an eToro",
    )
