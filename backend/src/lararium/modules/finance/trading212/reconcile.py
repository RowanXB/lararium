"""Reconciling a Trading 212 account against what Trading 212 reports (design §5.11,
Decision 12).

At the sync's time (when its summary arrived, report_sightings), every instrument's quantity
Trading 212 reports (its positions) and the account's cash (`availableToTrade +
reservedForOrders + inPies`, in the account's currency) must equal the sum of the account's
non-void postings that occurred at or before it, within the instrument's tolerance: a fill
after the summary is not in it yet. An instrument the ledger holds and the positions omit
counts as reported 0. Each reported figure is a `reported` snapshot at its sighting's time; a
sighting saying what the day's snapshot before it says reuses that one.

The warnings are what the latest check found: each run that has the account's summary
replaces in full the reconciliation warnings the connection's cursor records it published
last (connector_flags, never matched by their text), whatever the account is called now, and
keeps every other warning. The owner is told of each warning text once, counted only when a
send succeeds; a difference is never corrected. Warnings name the account, the instrument's
symbol, the day and the difference, never another value.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from lararium.core.connectors import SyncContext
from lararium.core.json_types import JSONValue
from lararium.modules.finance import connector_flags, models
from lararium.modules.finance.connector_run import holdings
from lararium.modules.finance.money import to_plain_str, tolerance

PLATFORM = "Trading 212"
# What every reconciliation warning holds, after the account's name (Decision 12).
DIFFERS = f" differs from {PLATFORM}'s report on "


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
    at: datetime,
    day: date,
    reported: Mapping[UUID, Decimal],
) -> list[Mismatch]:
    """Each instrument whose ledger quantity at `at` (the report's arrival, on `day`)
    differs from `reported` beyond its tolerance, by symbol."""
    i = models.Instrument
    held = await holdings(session, account_id, at)
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


async def publish(ctx: SyncContext, mismatches: list[Mismatch] | None) -> dict[str, JSONValue]:
    """Replace the reconciliation warnings the connection last published (by its cursor,
    connector_flags) with these, keeping every other warning; tell the owner, in one message,
    of each not told yet. None: no summary this run, so the last check's warnings stand.
    Returns the cursor's keys to write."""
    return await connector_flags.publish_reconciliation(
        ctx,
        None if mismatches is None else [m.warning for m in mismatches],
        label="a Trading 212",
    )
