"""The opening balance adjustment (design 5.8, plan Decision 12): what a wallet held before the
history its records go back to.

At each account's earliest report (the earliest time any of its instruments was reported, a
daily snapshot included; `reconcile.report_times`), for every instrument the account has a
snapshot or a posting of: its quantity in that report (0 when the report does not list it, as
a report is complete; `reconcile.reported_at`: a futures wallet's is its wallet balance, which
leaves its unrealised profit out) less the sum of the account's non-void postings of it up to
that time, the opening itself left out. An asset first reported later is not an
opening: its appearance is a reconciliation difference unless a record explains it.

It is derived, not decided: set afresh after every normalising run once the first sync's
backfill is complete, so a late record from before the earliest report moves it, and
regenerating from the raw records gives the same ledger. A residual of 0 voids it.

It is dated the day before the account's earliest other record (at 00:00 UTC), or at the
earliest report when the account has no record, and never after the earliest report: what
the records and reports disagree about after it is a reconciliation difference, never part of
the opening.
"""

from datetime import UTC, datetime, time, timedelta
from decimal import MAX_PREC, Decimal, localcontext
from uuid import UUID

from sqlalchemy import ColumnElement, and_, func, not_, select

from lararium.modules.finance import models
from lararium.modules.finance.binance.reconcile import report_times, reported_at, wallets
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


async def _instruments(run: Run, account_id: UUID) -> set[UUID]:
    """Every instrument the account has a posting of (its openings included, so one no longer
    needed is voided)."""
    t, p = models.Transaction, models.Posting
    rows = await run.session.scalars(
        select(p.instrument_id)
        .distinct()
        .join(t, t.id == p.transaction_id)
        .where(p.account_id == account_id, t.status != "void")
    )
    return set(rows)


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
    made = 0
    accounts = await wallets(run.session, run.connection.id)
    with localcontext(prec=MAX_PREC):
        for account_id, wallet in sorted(accounts.items(), key=lambda item: item[1]):
            times = await report_times(run.session, account_id)
            if not times:
                for instrument_id in sorted(await _instruments(run, account_id)):
                    await remove(run, f"{OPENING_PREFIX}{wallet}:{instrument_id}")
                continue
            earliest = times[0]
            reported = await reported_at(run.session, account_id, earliest, wallet)
            first = await _earliest_record(run, account_id)
            for instrument_id in sorted({*reported, *await _instruments(run, account_id)}):
                key = f"{OPENING_PREFIX}{wallet}:{instrument_id}"
                quantity = reported.get(instrument_id, Decimal(0))
                residual = quantity - await _posted(run, account_id, instrument_id, earliest)
                if residual == 0:
                    await remove(run, key)
                    continue
                at = earliest if first is None else min(first - timedelta(days=1), earliest)
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
