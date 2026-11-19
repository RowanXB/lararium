"""Reconciling the ledger against what IBKR reports (design 5.11, Decision 13).

For each account, at the end date of each window a sync fetched, every instrument's reported
quantity (Open Positions for securities, the Cash Report's endingCash for currencies) must
equal the sum of the account's non-void postings with a booking date at or before that day,
within the instrument's tolerance. A difference becomes a connection warning and, once per
new value, a notification; it is never corrected. Warnings name the last four digits of the
account id, the instrument's symbol and the day, never a personal value.

The latest day each account's securities and currencies were reconciled at is kept in the
connection's config, and a run whose own statements end earlier (an older statement fetched
late) checks that day again: what it posts may settle, or make, a difference there.
"""

import logging
from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
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
KINDS: tuple[Kind, ...] = ("securities", "currencies")
# The machine-readable record of this module's warnings in the connection's config: a list
# of {account, kind, text}, so a warning is replaced by the full account id and kind, never
# by the last four digits its text shows.
RECORD_KEY = "ibkr_warnings"
# The latest day each (account, kind) was reconciled at: {account: {kind: "YYYY-MM-DD"}}.
RECONCILED_KEY = "ibkr_reconciled"


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
    # The current differences: the latest day of each instrument, the rechecked latest
    # reconciled day included.
    mismatches: list[Mismatch]
    # The latest day each (IBKR account id, kind) was actually reconciled at.
    reconciled: dict[tuple[str, Kind], date]
    # The differences at this run's own window end dates, which the owner is told of even
    # when a later day's check supersedes them.
    found: list[Mismatch] = field(default_factory=list[Mismatch])


def reconciled_days(connection: ConnectionRecord) -> dict[tuple[str, Kind], date]:
    """The latest day each (account, kind) was reconciled at, from the connection's config."""
    stored = connection.config.get(RECONCILED_KEY)
    result: dict[tuple[str, Kind], date] = {}
    if not isinstance(stored, dict):
        return result
    for ibkr_id, days in stored.items():
        if not isinstance(days, dict):
            continue
        for kind in KINDS:
            value = days.get(kind)
            if isinstance(value, str):
                result[(ibkr_id, kind)] = date.fromisoformat(value)
    return result


async def check(
    session: AsyncSession,
    accounts: Mapping[str, UUID],
    windows: Iterable[tuple[str, date]],
    sections: frozenset[tuple[str, str]],
    previous: Mapping[tuple[str, Kind], date] | None = None,
) -> Checked:
    """The differences at each (IBKR account id, window end date), the latest day of each
    instrument only: a difference that persists is one warning, not one per day. A kind is
    reconciled only where the window's statement has its section.

    `previous` (reconciled_days) is where each (account, kind) was last reconciled: when this
    run's windows end earlier, that day is checked again for that kind, as this run's
    postings may have changed the ledger up to it."""
    own: defaultdict[tuple[str, date], set[Kind]] = defaultdict(set)
    reached: dict[tuple[str, Kind], date] = {}
    for ibkr_id, day in set(windows):
        present: dict[Kind, bool] = {
            "securities": (ibkr_id, POSITIONS_SECTION) in sections,
            "currencies": (ibkr_id, CASH_SECTION) in sections,
        }
        for kind in KINDS:
            if present[kind]:
                own[(ibkr_id, day)].add(kind)
                reached[(ibkr_id, kind)] = max(day, reached.get((ibkr_id, kind), day))
    again: defaultdict[tuple[str, date], set[Kind]] = defaultdict(set)
    for (ibkr_id, kind), day in (previous or {}).items():
        if (ibkr_id, kind) not in reached or reached[(ibkr_id, kind)] < day:
            again[(ibkr_id, day)].add(kind)

    async def differences(ibkr_id: str, day: date, kinds: set[Kind]) -> list[tuple[UUID, Mismatch]]:
        account_id = accounts[ibkr_id]
        found = await reconcile(
            session,
            account_id,
            day,
            await reported_on(session, account_id, day),
            securities="securities" in kinds,
            currencies="currencies" in kinds,
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
        result: list[tuple[UUID, Mismatch]] = []
        for instrument_id, difference in found:
            row = described[instrument_id]
            kind: Kind = "currencies" if row.kind == "fiat" else "securities"
            result.append((instrument_id, Mismatch(ibkr_id, kind, row.symbol, day, difference)))
        return result

    # By (account, instrument): the latest day of each instrument only.
    latest_own: dict[tuple[str, UUID], Mismatch] = {}
    reconciled: dict[tuple[str, Kind], date] = {}
    for (ibkr_id, day), kinds in sorted(own.items(), key=lambda w: (w[0][1], w[0][0])):
        if ibkr_id not in accounts:
            continue
        for kind in kinds:
            reconciled[(ibkr_id, kind)] = day
        for instrument_id, mismatch in await differences(ibkr_id, day, kinds):
            latest_own[(ibkr_id, instrument_id)] = mismatch
    rechecked: dict[tuple[str, UUID], Mismatch] = {}
    again_kinds: set[tuple[str, Kind]] = set()
    for (ibkr_id, day), kinds in sorted(again.items(), key=lambda w: (w[0][1], w[0][0])):
        if ibkr_id not in accounts:
            continue
        for kind in kinds:
            reconciled[(ibkr_id, kind)] = day
            again_kinds.add((ibkr_id, kind))
        for instrument_id, mismatch in await differences(ibkr_id, day, kinds):
            rechecked[(ibkr_id, instrument_id)] = mismatch
    # For a kind checked again, the current differences are that day's: this run's own,
    # older days are only told.
    current = [m for m in latest_own.values() if (m.ibkr_id, m.kind) not in again_kinds]
    current += rechecked.values()

    def ordered(found: Iterable[Mismatch]) -> list[Mismatch]:
        return sorted(found, key=lambda m: (m.ibkr_id, m.symbol, m.day))

    return Checked(ordered(current), reconciled, ordered(latest_own.values()))


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
    origins are kept. The latest day each pair was reconciled at is recorded for the next
    run (check's `previous`)."""
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
    told = dict.fromkeys([*flags, *(m.warning for m in [*checked.found, *checked.mismatches])])
    for warning in told:
        if warning not in previous:
            await _notify(ctx, f"{connection.display_name}: {warning}")
    if warnings != previous:
        await set_warnings(ctx.command, connection.id, warnings)
    values: dict[str, JSONValue] = {}
    if kept != recorded:
        values[RECORD_KEY] = [dict(e) for e in kept]
    days = reconciled_days(connection)
    merged = dict(days)
    for pair, day in checked.reconciled.items():
        merged[pair] = max(day, merged.get(pair, day))
    if merged != days:
        stored: dict[str, dict[str, JSONValue]] = {}
        for (ibkr_id, kind), day in sorted(merged.items()):
            stored.setdefault(ibkr_id, {})[kind] = day.isoformat()
        values[RECONCILED_KEY] = {k: v for k, v in stored.items()}
    if values:
        await update_config(ctx.command, connection.id, values)
