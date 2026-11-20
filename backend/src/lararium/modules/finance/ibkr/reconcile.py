"""Reconciling the ledger against what IBKR reports (design 5.11, Decision 13).

Every instrument's reported quantity (Open Positions for securities, the Cash Report's
endingCash for currencies) must equal the sum of the account's non-void postings with a
booking date at or before the report's day, within the instrument's tolerance.

Every run checks every account of the connection at its latest reported day, of securities
and of currencies each, and the connection's reconciliation warnings are what that check
finds: so they follow from what is stored, whatever order statements arrive in. A run also
checks the end of each window it fetched; a difference found there that the latest day does
not show (on an earlier day, or on a day whose Open Positions were empty) is only told.

The owner is told of each warning text once, when it is new; a difference is never corrected.
Warnings name the last four digits of the account id, the instrument's symbol and the day,
never a personal value.
"""

import logging
from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Literal
from uuid import UUID

from sqlalchemy import ColumnElement, func, select
from sqlalchemy.dialects.postgresql import distinct_on
from sqlalchemy.ext.asyncio import AsyncSession

from lararium.core.connections import ConnectionRecord, set_warnings
from lararium.core.connectors import SyncContext
from lararium.modules.finance import models
from lararium.modules.finance.ibkr.records import CASH_SECTION, POSITIONS_SECTION
from lararium.modules.finance.money import to_plain_str, tolerance

logger = logging.getLogger(__name__)

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
# What the connection's config once kept to choose which warnings a run could replace; they
# are removed where they are still stored.
OLD_KEYS = ("ibkr_warnings", "ibkr_reconciled")


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
    # The differences at each account's latest reported day: the connection's warnings.
    mismatches: list[Mismatch]
    # The differences at the end of this run's own windows, the latest of each instrument:
    # told when new, never kept.
    found: list[Mismatch]


def _of_kind(kind: Kind) -> ColumnElement[bool]:
    i = models.Instrument
    return i.kind == "fiat" if kind == "currencies" else i.kind != "fiat"


async def _latest_reported(session: AsyncSession, account_id: UUID, kind: Kind) -> date | None:
    """The latest day the account has a reported snapshot of the kind: for currencies, the
    latest Cash Report's toDate; for securities, the latest Open Positions day that listed a
    position (an empty section stores nothing to find)."""
    s, i = models.BalanceSnapshot, models.Instrument
    return await session.scalar(
        select(func.max(s.as_of_date))
        .join(i, i.id == s.instrument_id)
        .where(s.account_id == account_id, s.kind == "reported", _of_kind(kind))
    )


async def _differences(
    session: AsyncSession, ibkr_id: str, account_id: UUID, day: date, kinds: set[Kind]
) -> list[tuple[UUID, Mismatch]]:
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
            select(i.id, i.symbol, i.kind).where(i.id.in_([instrument for instrument, _ in found]))
        )
    }
    result: list[tuple[UUID, Mismatch]] = []
    for instrument_id, difference in found:
        row = described[instrument_id]
        kind: Kind = "currencies" if row.kind == "fiat" else "securities"
        result.append((instrument_id, Mismatch(ibkr_id, kind, row.symbol, day, difference)))
    return result


def _ordered(found: Iterable[Mismatch]) -> list[Mismatch]:
    return sorted(found, key=lambda m: (m.ibkr_id, m.symbol, m.day))


async def check(
    session: AsyncSession,
    accounts: Mapping[str, UUID],
    windows: Iterable[tuple[str, date]],
    sections: frozenset[tuple[str, str]],
) -> Checked:
    """The differences of every account in `accounts` (IBKR account id to finance account) at
    its latest reported day of each kind, and those at each (IBKR account id, window end
    date) of this run, the latest day of each instrument only: a difference that persists
    is told once, not once per day. A window's kind is checked only where its statement has
    the section; a kind with no reported day is not checked at its latest."""
    latest: defaultdict[tuple[str, date], set[Kind]] = defaultdict(set)
    for ibkr_id, account_id in accounts.items():
        for kind in KINDS:
            day = await _latest_reported(session, account_id, kind)
            if day is not None:
                latest[(ibkr_id, day)].add(kind)
    current: list[Mismatch] = []
    for (ibkr_id, day), kinds in sorted(latest.items()):
        found = await _differences(session, ibkr_id, accounts[ibkr_id], day, kinds)
        current += [mismatch for _, mismatch in found]

    own: defaultdict[tuple[str, date], set[Kind]] = defaultdict(set)
    for ibkr_id, day in set(windows):
        if ibkr_id not in accounts:
            continue
        if (ibkr_id, POSITIONS_SECTION) in sections:
            own[(ibkr_id, day)].add("securities")
        if (ibkr_id, CASH_SECTION) in sections:
            own[(ibkr_id, day)].add("currencies")
    # By (account, instrument): the latest day of each instrument only.
    told: dict[tuple[str, UUID], Mismatch] = {}
    for (ibkr_id, day), kinds in sorted(own.items(), key=lambda w: (w[0][1], w[0][0])):
        for instrument_id, mismatch in await _differences(
            session, ibkr_id, accounts[ibkr_id], day, kinds
        ):
            told[(ibkr_id, instrument_id)] = mismatch
    return Checked(_ordered(current), _ordered(told.values()))


async def _notify(ctx: SyncContext, text: str) -> None:
    """Tell the owner; a failure to send is logged and never fails the sync. Only the
    exception's class is logged: its message could carry the notifier's webhook URL."""
    try:
        await ctx.notifier.send(text)
    except Exception as error:
        logger.warning("sending an IBKR notification failed (%s)", type(error).__name__)


async def publish(
    ctx: SyncContext, connection: ConnectionRecord, checked: Checked, flagged: Iterable[str]
) -> None:
    """Replace the connection's own warnings (flags and reconciliation differences, by their
    prefixes) with this run's, keeping warnings of other origins; and tell the owner, in one
    message, about each flag and difference whose text was not among the previous warnings."""
    previous = connection.warnings
    warnings = [w for w in previous if not w.startswith((FLAGGED_PREFIX, WARNING_PREFIX))]
    names = sorted(flagged)
    flags = [FLAGGED_PREFIX + name for name in names]
    differences = list(dict.fromkeys(m.warning for m in checked.mismatches))
    warnings += flags + differences
    # A new query can flag dozens of fields at once: they share one line.
    new_names = [name for name, flag in zip(names, flags, strict=True) if flag not in previous]
    lines = [FLAGGED_PREFIX + ", ".join(new_names)] if new_names else []
    told = dict.fromkeys([*differences, *(m.warning for m in checked.found)])
    lines += [warning for warning in told if warning not in previous]
    if lines:
        await _notify(ctx, f"{connection.display_name}: " + "\n".join(lines))
    if warnings != previous:
        await set_warnings(ctx.command, connection.id, warnings)
