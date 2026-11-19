"""Reconciling the ledger against what IBKR reports (design 5.11, Decision 13).

For each account, at the end date of each window a sync fetched, every instrument's reported
quantity (Open Positions for securities, the Cash Report's endingCash for currencies) must
equal the sum of the account's non-void postings with a booking date at or before that day,
within the instrument's tolerance. A difference becomes a connection warning and, once per
new value, a notification; it is never corrected. Warnings name the last four digits of the
account id, the instrument's symbol and the day, never a personal value.
"""

import logging
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import distinct_on
from sqlalchemy.ext.asyncio import AsyncSession

from lararium.core.connections import ConnectionRecord, set_warnings
from lararium.core.connectors import SyncContext
from lararium.core.json_types import JSONValue
from lararium.modules.finance import models
from lararium.modules.finance.ibkr.records import POSITIONS_SECTION
from lararium.modules.finance.money import to_plain_str, tolerance

logger = logging.getLogger(__name__)

CASH_SECTION = "CashReport"
# The last difference the owner was told about, per account and instrument (Monzo's pattern).
NOTIFIED_KEY = "reconciliation_notified"
WARNING_PREFIX = "IBKR ···"
FLAGGED_PREFIX = "IBKR flagged: "


async def reconcile(
    session: AsyncSession,
    account_id: UUID,
    on: date,
    reported: Mapping[UUID, Decimal],
    *,
    securities: bool = True,
    currencies: bool = True,
) -> list[tuple[UUID, Decimal]]:
    """(instrument, ledger - reported) for each instrument whose difference exceeds its
    tolerance, as of the end of `on`. An instrument the ledger holds and `reported` omits
    counts as reported 0; `securities` and `currencies` say which kinds are checked at all
    (a statement without the section says nothing about them)."""
    t, p, i = models.Transaction, models.Posting, models.Instrument
    held = {
        row.instrument_id: row.total
        for row in await session.execute(
            select(p.instrument_id, func.sum(p.quantity).label("total"))
            .join(t, t.id == p.transaction_id)
            .where(p.account_id == account_id, t.status != "void", t.booking_date <= on)
            .group_by(p.instrument_id)
        )
    }
    ids = set(held) | set(reported)
    if not ids:
        return []
    instruments = {
        row.id: row
        for row in await session.execute(select(i.id, i.kind, i.decimals).where(i.id.in_(ids)))
    }
    differences: list[tuple[UUID, Decimal]] = []
    for instrument_id in sorted(ids):
        instrument = instruments[instrument_id]
        if not (currencies if instrument.kind == "fiat" else securities):
            continue
        difference = held.get(instrument_id, Decimal(0)) - reported.get(instrument_id, Decimal(0))
        if difference.copy_abs() > tolerance(instrument.decimals):
            differences.append((instrument_id, difference))
    return differences


async def reported_on(session: AsyncSession, account_id: UUID, day: date) -> dict[UUID, Decimal]:
    """The account's reported quantity of each instrument on the day: the latest reported
    snapshot of each, for IBKR may change a day's figure."""
    s = models.BalanceSnapshot
    rows = await session.execute(
        select(s.instrument_id, s.quantity)
        .where(s.account_id == account_id, s.kind == "reported", s.as_of_date == day)
        .ext(distinct_on(s.instrument_id))
        .order_by(s.instrument_id, s.recorded_at.desc(), s.id.desc())
    )
    return {row.instrument_id: row.quantity for row in rows}


@dataclass(frozen=True)
class Mismatch:
    ibkr_id: str
    instrument_id: UUID
    symbol: str
    day: date
    difference: Decimal

    @property
    def warning(self) -> str:
        return (
            f"{WARNING_PREFIX}{self.ibkr_id[-4:]} {self.symbol} differs from IBKR's report"
            f" on {self.day.isoformat()} by {to_plain_str(self.difference)}"
        )

    @property
    def key(self) -> str:
        return f"{self.ibkr_id}:{self.instrument_id}"


async def check(
    session: AsyncSession,
    accounts: Mapping[str, UUID],
    windows: Iterable[tuple[str, date]],
    sections: frozenset[tuple[str, str]],
) -> list[Mismatch]:
    """The differences at each (IBKR account id, window end date), the latest day of each
    instrument only: a difference that persists is one warning, not one per day."""
    latest: dict[tuple[str, UUID], Mismatch] = {}
    for ibkr_id, day in sorted(set(windows), key=lambda w: (w[1], w[0])):
        account_id = accounts.get(ibkr_id)
        if account_id is None:
            continue
        found = await reconcile(
            session,
            account_id,
            day,
            await reported_on(session, account_id, day),
            securities=(ibkr_id, POSITIONS_SECTION) in sections,
            currencies=(ibkr_id, CASH_SECTION) in sections,
        )
        symbols = {
            row.id: row.symbol
            for row in await session.execute(
                select(models.Instrument.id, models.Instrument.symbol).where(
                    models.Instrument.id.in_([instrument_id for instrument_id, _ in found])
                )
            )
        }
        for instrument_id, difference in found:
            latest[(ibkr_id, instrument_id)] = Mismatch(
                ibkr_id, instrument_id, symbols[instrument_id], day, difference
            )
    return sorted(latest.values(), key=lambda m: (m.ibkr_id, m.symbol, m.day))


async def _notify(ctx: SyncContext, text: str) -> None:
    """Tell the owner; a failure to send is logged and never fails the sync."""
    try:
        await ctx.notifier.send(text)
    except Exception:
        logger.warning("sending an IBKR notification failed", exc_info=True)


async def publish(
    ctx: SyncContext,
    connection: ConnectionRecord,
    mismatches: Sequence[Mismatch],
    checked: Iterable[str],
    flagged: Iterable[str],
) -> dict[str, JSONValue]:
    """Replace the reconciliation warnings of the accounts checked, keep the flagged ones
    and add the new, tell the owner about each new difference and set of flags once, and
    return the connection's notified-differences config value (for the caller to store)."""
    checked = set(checked)
    stored = connection.config.get(NOTIFIED_KEY)
    notified: dict[str, JSONValue] = dict(stored) if isinstance(stored, dict) else {}
    warnings = [
        w
        for w in connection.warnings
        if not any(w.startswith(f"{WARNING_PREFIX}{ibkr_id[-4:]} ") for ibkr_id in checked)
    ]
    new_flags = [name for name in sorted(flagged) if (FLAGGED_PREFIX + name) not in warnings]
    warnings += [FLAGGED_PREFIX + name for name in new_flags]
    current = {m.key for m in mismatches}
    for key in [k for k in notified if k.split(":", 1)[0] in checked and k not in current]:
        del notified[key]
    for mismatch in mismatches:
        warnings.append(mismatch.warning)
        difference = to_plain_str(mismatch.difference)
        if notified.get(mismatch.key) != difference:
            notified[mismatch.key] = difference
            await _notify(ctx, f"{connection.display_name}: {mismatch.warning}")
    if new_flags:
        listed = "; ".join(FLAGGED_PREFIX + name for name in new_flags)
        await _notify(ctx, f"{connection.display_name}: {listed}")
    if warnings != connection.warnings:
        await set_warnings(ctx.command, connection.id, warnings)
    return notified
