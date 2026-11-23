"""The opening balance adjustment (design 5.8, plan Decision 12): what a wallet held before the
history its records go back to.

For every (account, instrument) with a reported snapshot: the earliest reported quantity less
the sum of the account's non-void postings of the instrument up to that snapshot's `as_of`,
the opening itself left out. It is derived, not decided: set afresh after every normalising
run once the first sync's backfill is complete, so a late record from before the earliest
report moves it, and regenerating from the raw records gives the same ledger. A residual of 0
voids it.

It is dated the day before the account's earliest other record (at 00:00 UTC), or at the
earliest report when the account has no record, and never after the earliest report: what
the records and reports disagree about after it is a reconciliation difference, never part of
the opening.
"""

from datetime import UTC, datetime, time, timedelta
from decimal import MAX_PREC, Decimal, localcontext
from uuid import UUID

from sqlalchemy import ColumnElement, and_, func, not_, select
from sqlalchemy.dialects.postgresql import distinct_on

from lararium.core.connections import get_connection
from lararium.modules.finance import models
from lararium.modules.finance.binance.accounts import ACCOUNT_MAP_KEY
from lararium.modules.finance.binance.records import Run, post, remove
from lararium.modules.finance.ledger import PostingInput

OPENING_PREFIX = "opening:"
OPENING_DESCRIPTION = "Opening balance adjustment"


def _not_opening(connection_id: UUID) -> ColumnElement[bool]:
    t = models.Transaction
    return not_(
        and_(
            t.connection_id == connection_id,
            func.coalesce(t.external_id, "").startswith(OPENING_PREFIX),
        )
    )


async def _earliest_record(run: Run, account_id: UUID) -> datetime | None:
    """The day the account's earliest non-void record other than an opening is booked on, at
    00:00 UTC."""
    t, p = models.Transaction, models.Posting
    day = await run.session.scalar(
        select(func.min(t.booking_date))
        .join(p, p.transaction_id == t.id)
        .where(p.account_id == account_id, t.status != "void", _not_opening(run.connection.id))
    )
    return None if day is None else datetime.combine(day, time(), UTC)


async def _earliest_reports(run: Run, account_id: UUID) -> list[tuple[UUID, Decimal, datetime]]:
    """(instrument, quantity, as_of) of the account's earliest reported snapshot of each."""
    s = models.BalanceSnapshot
    rows = await run.session.execute(
        select(s.instrument_id, s.quantity, s.as_of)
        .where(s.account_id == account_id, s.kind == "reported")
        .ext(distinct_on(s.instrument_id))
        .order_by(s.instrument_id, s.as_of, s.recorded_at.desc(), s.id.desc())
    )
    return [(row.instrument_id, row.quantity, row.as_of) for row in rows]


async def _posted(run: Run, account_id: UUID, instrument_id: UUID, through: datetime) -> Decimal:
    t, p = models.Transaction, models.Posting
    total = await run.session.scalar(
        select(func.coalesce(func.sum(p.quantity), 0))
        .join(t, t.id == p.transaction_id)
        .where(
            p.account_id == account_id,
            p.instrument_id == instrument_id,
            t.status != "void",
            t.occurred_at <= through,
            _not_opening(run.connection.id),
        )
    )
    return Decimal(total or 0)


async def set_openings(run: Run) -> int:
    """Set every wallet account's openings afresh (`opening:<wallet key>:<instrument id>`,
    against Opening balances); how many there are now."""
    mapped = (await get_connection(run.session, run.connection.id)).config.get(ACCOUNT_MAP_KEY)
    made = 0
    with localcontext(prec=MAX_PREC):
        for wallet, value in sorted((mapped if isinstance(mapped, dict) else {}).items()):
            try:
                account_id = UUID(value) if isinstance(value, str) else None
            except ValueError:
                account_id = None
            if account_id is None:
                continue
            first = await _earliest_record(run, account_id)
            for instrument_id, quantity, as_of in await _earliest_reports(run, account_id):
                key = f"{OPENING_PREFIX}{wallet}:{instrument_id}"
                residual = quantity - await _posted(run, account_id, instrument_id, as_of)
                if residual == 0:
                    await remove(run, key)
                    continue
                at = as_of if first is None else min(first - timedelta(days=1), as_of)
                await post(
                    run,
                    key,
                    occurred_at=at,
                    description=OPENING_DESCRIPTION,
                    postings=[
                        PostingInput(account_id, instrument_id, residual),
                        PostingInput(run.opening_balances, instrument_id, -residual),
                    ],
                )
                made += 1
    return made
