"""Reconciling the ledger against what IBKR reports (design 5.11, Decision 13).

For each account, at the end date of each window a sync fetched, every instrument's reported
quantity (Open Positions for securities, the Cash Report's endingCash for currencies) must
equal the sum of the account's non-void postings with a booking date at or before that day,
within the instrument's tolerance. A difference becomes a connection warning and, once per
new value, a notification; it is never corrected. Warnings name the last four digits of the
account id, the instrument's symbol and the day, never a personal value.
"""

import logging
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Literal
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import distinct_on
from sqlalchemy.ext.asyncio import AsyncSession

from lararium.core.connections import ConnectionRecord, set_warnings, update_config
from lararium.core.connectors import SyncContext
from lararium.core.json_types import JSONValue
from lararium.modules.finance import models
from lararium.modules.finance.ibkr.records import POSITIONS_SECTION
from lararium.modules.finance.money import to_plain_str, tolerance

logger = logging.getLogger(__name__)

CASH_SECTION = "CashReport"
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


type Kind = Literal["securities", "currencies"]
# The machine-readable record of this module's warnings in the connection's config: a list
# of {account, kind, text}, so a warning is replaced by the full account id and kind, never
# by the last four digits its text shows.
RECORD_KEY = "ibkr_warnings"


@dataclass(frozen=True)
class Mismatch:
    ibkr_id: str
    kind: Kind
    symbol: str
    day: date
    difference: Decimal

    @property
    def warning(self) -> str:
        return (
            f"{WARNING_PREFIX}{self.ibkr_id[-4:]} {self.symbol} differs from IBKR's report"
            f" on {self.day.isoformat()} by {to_plain_str(self.difference)}"
        )


@dataclass(frozen=True)
class Checked:
    mismatches: list[Mismatch]
    # The latest window end date each (IBKR account id, kind) was actually reconciled at.
    reconciled: dict[tuple[str, Kind], date]


async def check(
    session: AsyncSession,
    accounts: Mapping[str, UUID],
    windows: Iterable[tuple[str, date]],
    sections: frozenset[tuple[str, str]],
) -> Checked:
    """The differences at each (IBKR account id, window end date), the latest day of each
    instrument only: a difference that persists is one warning, not one per day. A kind is
    reconciled only where the window's statement has its section."""
    latest: dict[tuple[str, UUID], Mismatch] = {}
    reconciled: dict[tuple[str, Kind], date] = {}
    for ibkr_id, day in sorted(set(windows), key=lambda w: (w[1], w[0])):
        account_id = accounts.get(ibkr_id)
        if account_id is None:
            continue
        kinds: dict[Kind, bool] = {
            "securities": (ibkr_id, POSITIONS_SECTION) in sections,
            "currencies": (ibkr_id, CASH_SECTION) in sections,
        }
        for kind, present in kinds.items():
            if present:
                reconciled[(ibkr_id, kind)] = day
        found = await reconcile(
            session,
            account_id,
            day,
            await reported_on(session, account_id, day),
            securities=kinds["securities"],
            currencies=kinds["currencies"],
        )
        i = models.Instrument
        described = {
            row.id: row
            for row in await session.execute(
                select(i.id, i.symbol, i.kind).where(
                    i.id.in_([instrument_id for instrument_id, _ in found])
                )
            )
        }
        for instrument_id, difference in found:
            row = described[instrument_id]
            kind: Kind = "currencies" if row.kind == "fiat" else "securities"
            latest[(ibkr_id, instrument_id)] = Mismatch(ibkr_id, kind, row.symbol, day, difference)
    mismatches = sorted(latest.values(), key=lambda m: (m.ibkr_id, m.symbol, m.day))
    return Checked(mismatches, reconciled)


async def _notify(ctx: SyncContext, text: str) -> None:
    """Tell the owner; a failure to send is logged and never fails the sync. Only the
    exception's class is logged: its message could carry the notifier's webhook URL."""
    try:
        await ctx.notifier.send(text)
    except Exception as error:
        logger.warning("sending an IBKR notification failed (%s)", type(error).__name__)


async def replaceable(
    session: AsyncSession, accounts: Mapping[str, UUID], checked: Checked
) -> set[tuple[str, Kind]]:
    """The (account, kind) pairs whose warnings this run may replace: those it reconciled at
    the account's latest stored report date. An older statement (uploaded late) must not
    replace what the newest one says."""
    s = models.BalanceSnapshot
    result: set[tuple[str, Kind]] = set()
    for (ibkr_id, kind), day in checked.reconciled.items():
        stored = await session.scalar(
            select(func.max(s.as_of_date)).where(
                s.account_id == accounts[ibkr_id], s.kind == "reported"
            )
        )
        if stored is None or day >= stored:
            result.add((ibkr_id, kind))
    return result


def _recorded(connection: ConnectionRecord) -> list[dict[str, str]]:
    stored = connection.config.get(RECORD_KEY)
    if not isinstance(stored, list):
        return []
    return [
        {"account": str(e["account"]), "kind": str(e["kind"]), "text": str(e["text"])}
        for e in stored
        if isinstance(e, dict) and {"account", "kind", "text"} <= e.keys()
    ]


async def publish(
    ctx: SyncContext,
    connection: ConnectionRecord,
    checked: Checked,
    replace: set[tuple[str, Kind]],
    flagged: Iterable[str],
) -> None:
    """Set the connection's warnings and tell the owner about each warning text that was not
    among the previous ones.

    Flagged warnings are replaced by this run's. Reconciliation warnings are replaced for
    the (account, kind) pairs in `replace`; a difference for any other pair is only
    notified, and the warnings of pairs this run did not reconcile stay. Warnings of other
    origins are kept."""
    previous = connection.warnings
    recorded = _recorded(connection)
    dropped = {e["text"] for e in recorded if (e["account"], e["kind"]) in replace}
    kept = [e for e in recorded if e["text"] not in dropped]
    warnings = [w for w in previous if not w.startswith(FLAGGED_PREFIX) and w not in dropped]
    flags = [FLAGGED_PREFIX + name for name in sorted(flagged)]
    warnings += flags
    added = [m for m in checked.mismatches if (m.ibkr_id, m.kind) in replace]
    for mismatch in added:
        if mismatch.warning not in warnings:
            warnings.append(mismatch.warning)
        kept.append({"account": mismatch.ibkr_id, "kind": mismatch.kind, "text": mismatch.warning})
    for warning in [*flags, *(m.warning for m in checked.mismatches)]:
        if warning not in previous:
            await _notify(ctx, f"{connection.display_name}: {warning}")
    if warnings != previous:
        await set_warnings(ctx.command, connection.id, warnings)
    if kept != recorded:
        record: JSONValue = [dict(e) for e in kept]
        await update_config(ctx.command, connection.id, {RECORD_KEY: record})
