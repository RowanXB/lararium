"""Reconciling a Trading 212 account against what Trading 212 reports (design §5.11,
Decision 12).

At each sync's time, every instrument's quantity Trading 212 reports (its positions) and the
account's cash (`availableToTrade + reservedForOrders + inPies`, in the account's currency)
must equal the sum of the account's non-void postings booked on or before that day, within the
instrument's tolerance. An instrument the ledger holds and the positions omit counts as
reported 0. Each reported figure is a `reported` snapshot at the sync's time, one per day
while it stays the same.

The warnings are what the latest check found: each run that has the account's summary
replaces the connection's reconciliation warnings in full, whatever the account is called
now, and keeps every other warning. The owner is told of each warning text once, when it is
new; a difference is never corrected. Warnings name the account, the instrument's symbol, the
day and the difference, never another value.
"""

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from lararium.core.commands import CommandContext
from lararium.core.connections import get_connection, set_warnings
from lararium.core.connectors import SyncContext
from lararium.modules.finance import models
from lararium.modules.finance.money import to_plain_str, tolerance
from lararium.modules.finance.snapshots import record_snapshot

logger = logging.getLogger(__name__)

PLATFORM = "Trading 212"
# What every reconciliation warning holds, after the account's name (Decision 12).
DIFFERS = f" differs from {PLATFORM}'s report on "


async def reported_snapshot(
    ctx: CommandContext,
    account_id: UUID,
    instrument_id: UUID,
    quantity: Decimal,
    as_of: datetime,
    day: date,
) -> UUID:
    """The reported snapshot of the figure: the day's latest when it says the same, else a
    new one at `as_of`."""
    s = models.BalanceSnapshot
    latest = (
        await ctx.session.execute(
            select(s.id, s.quantity)
            .where(
                s.account_id == account_id,
                s.instrument_id == instrument_id,
                s.kind == "reported",
                s.as_of_date == day,
            )
            .order_by(s.as_of.desc(), s.recorded_at.desc(), s.id.desc())
            .limit(1)
        )
    ).one_or_none()
    if latest is not None and latest.quantity == quantity:
        return latest.id
    record = await record_snapshot(
        ctx,
        account_id=account_id,
        instrument_id=instrument_id,
        quantity=quantity,
        as_of=as_of,
        as_of_date=day,
        kind="reported",
    )
    return record.id


@dataclass(frozen=True)
class Mismatch:
    account_name: str
    symbol: str
    day: date
    difference: Decimal  # the ledger's less the report's

    @property
    def warning(self) -> str:
        return (
            f"{self.account_name} {self.symbol}{DIFFERS}{self.day.isoformat()}"
            f" by {to_plain_str(self.difference)}"
        )


async def check(
    session: AsyncSession,
    account_id: UUID,
    account_name: str,
    day: date,
    reported: Mapping[UUID, Decimal],
) -> list[Mismatch]:
    """Each instrument whose ledger quantity at the end of `day` differs from `reported`
    beyond its tolerance, by symbol."""
    t, p, i = models.Transaction, models.Posting, models.Instrument
    held = {
        row.instrument_id: Decimal(row.total)
        for row in await session.execute(
            select(p.instrument_id, func.sum(p.quantity).label("total"))
            .join(t, t.id == p.transaction_id)
            .where(p.account_id == account_id, t.status != "void", t.booking_date <= day)
            .group_by(p.instrument_id)
        )
    }
    ids = set(held) | set(reported)
    if not ids:
        return []
    described = {
        row.id: row
        for row in await session.execute(select(i.id, i.symbol, i.decimals).where(i.id.in_(ids)))
    }
    found: list[Mismatch] = []
    for instrument_id in ids:
        difference = held.get(instrument_id, Decimal(0)) - reported.get(instrument_id, Decimal(0))
        instrument = described[instrument_id]
        if difference.copy_abs() > tolerance(instrument.decimals):
            found.append(Mismatch(account_name, instrument.symbol, day, difference))
    return sorted(found, key=lambda m: m.symbol)


def is_reconciliation(warning: str) -> bool:
    """A warning this module writes, under any name the account has had."""
    return DIFFERS in warning


async def publish(ctx: SyncContext, mismatches: list[Mismatch]) -> None:
    """Replace the connection's reconciliation warnings with these, keeping every other
    warning; tell the owner, in one message, of each whose text is not there yet."""
    connection = await get_connection(ctx.command.session, ctx.connection.id)
    previous = connection.warnings
    texts = list(dict.fromkeys(m.warning for m in mismatches))
    warnings = [w for w in previous if not is_reconciliation(w)] + texts
    new = [text for text in texts if text not in previous]
    if new:
        try:
            await ctx.notifier.send(f"{connection.display_name}: " + "\n".join(new))
        except Exception as error:
            # The class only: the message could carry the notifier's webhook URL.
            logger.warning("sending a Trading 212 notification failed (%s)", type(error).__name__)
    if warnings != previous:
        await set_warnings(ctx.command, connection.id, warnings)
