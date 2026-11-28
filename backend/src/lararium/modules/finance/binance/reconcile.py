"""Binance's reported balances against the records posted (design 5.11, plan Decision 11).

An account's reports are the times it has `reported` snapshots at; a wallet's report is
complete, so an instrument a report does not list is 0 there (`reported_at`). An interval is two
consecutive reports of one account: an instrument's reported change is its quantity at the
second less its quantity at the first, its posted sum the account's non-void postings of it
with `from_at < occurred_at <= to_at` (what the `hybrid` rule adds after the first report and
the second already holds). A difference beyond the instrument's tolerance is stored, named in
the connection's warnings and told once; it is never corrected. One that no longer holds (the
record that explains it arrived) is closed.

Every interval is checked once it exists: a run checks every interval ending after
`config["reconciled_through"]` (the newest report the last reconciliation covered), every
interval holding a posting the run created, changed or removed, and every stored difference
still open. So a daily snapshot of the first sync and a report whose sync failed later are
checked, a late record closes its interval and a change to an old record reopens it. Warnings
and messages name the wallet, the asset and the two times, never an amount.

A run that commits postings queues what it touched (`queue_touched`) in the same transaction,
and the reconciliation that checks them clears the queue in its own: a sync that fails between
the two leaves them for the next one.
"""

import itertools
import re
from collections import defaultdict
from collections.abc import Collection
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import MAX_PREC, Decimal, localcontext
from uuid import UUID

from sqlalchemy import Select, func, select, text, union
from sqlalchemy.dialects.postgresql import distinct_on
from sqlalchemy.ext.asyncio import AsyncSession

from lararium.core.commands import CommandContext
from lararium.core.connections import get_connection, set_warnings, update_config
from lararium.core.errors import DisplayableError
from lararium.core.json_types import JSONValue
from lararium.modules.finance import models
from lararium.modules.finance.binance import notices
from lararium.modules.finance.binance.accounts import ACCOUNT_MAP_KEY, wallet_name
from lararium.modules.finance.binance.balances import millis
from lararium.modules.finance.binance.fetch import record_type
from lararium.modules.finance.binance.models import ReconciliationDifference
from lararium.modules.finance.binance.records import Run
from lararium.modules.finance.binance.tables import moment
from lararium.modules.finance.money import to_plain_str, tolerance

# (account, instrument, occurred_at) of a posting a run created, changed or removed.
type Point = tuple[UUID, UUID, datetime]
type Touched = Collection[Point]

# The newest report time (epoch ms) the last reconciliation covered.
RECONCILED_KEY = "reconciled_through"
# Per "<account id>:<instrument id>", the earliest and latest `occurred_at` (epoch ms) of the
# postings committed runs touched that no reconciliation has checked yet.
TOUCHED_KEY = "touched_pending"
# Earn's flexible position grows all day with the real-time reward, whose record is dated at
# the day's end: between two reports it is ahead of its records by the reward still accruing
# at the later one, or behind them by what had already accrued at the earlier one.
_EARN = "earn"
_REALTIME = "REALTIME"
_ACCRUING = timedelta(hours=48)
_SHOWN = "%Y-%m-%d %H:%M:%S"
_MOMENT = r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}"
# The warnings `reconcile` writes, and only those.
_WARNING = re.compile(
    rf"Binance .+: .+ changed by something no record explains between {_MOMENT}"
    rf" and {_MOMENT} \(UTC\)"
)


@dataclass(frozen=True)
class Difference:
    """An interval whose reported change the postings in it do not explain."""

    account_id: UUID
    instrument_id: UUID
    from_at: datetime
    to_at: datetime
    reported_change: Decimal
    posted_sum: Decimal

    @property
    def key(self) -> tuple[UUID, UUID, datetime, datetime]:
        return (self.account_id, self.instrument_id, self.from_at, self.to_at)


# An account's reports: one definition, which the opening uses too. Every wallet's reports are
# its snapshots, but a futures wallet's quantities are its account's `wallet_balance` (Decision
# 11), and the Options wallet's its margin account's `margin_balance`: their snapshots also carry
# unrealised profit, which no record explains.
# The derivatives wallets' typed account reports, by wallet key: the label read, the report
# table, its table of assets, and the column that holds what the records change.
ACCOUNT_REPORTS: dict[str, tuple[str, str, str, str]] = {
    "um_futures": (
        "um-account",
        "binance_um_account",
        "binance_um_account_assets",
        "wallet_balance",
    ),
    "cm_futures": (
        "cm-account",
        "binance_cm_account",
        "binance_cm_account_assets",
        "wallet_balance",
    ),
    "options": (
        "opt-account",
        "binance_opt_account",
        "binance_opt_account_asset",
        "margin_balance",
    ),
}
# The wallets whose report has no row of its own per read (its answer is the list of its assets,
# so an empty one stores nothing typed): the read itself, by the raw record of the read its
# snapshots were cut from, and the fields of each asset, with their signs, that hold what the
# records change. The Portfolio Margin wallet's is its `totalWalletBalance` less what it owes
# (`crossMarginBorrowed`, `crossMarginInterest`), without the unrealised profit its snapshot also
# carries (design 5.11).
READ_REPORTS: dict[str, tuple[str, tuple[tuple[str, int], ...]]] = {
    "portfolio_margin": (
        "pm-balance",
        (("totalWalletBalance", 1), ("crossMarginBorrowed", -1), ("crossMarginInterest", -1)),
    ),
}


async def report_times(session: AsyncSession, account_id: UUID) -> list[datetime]:
    """The times the account was reported at, oldest first: each recorded report
    (`binance_account_reports`, an empty one included) and, so that nothing depends on which
    was written first, each `as_of` of its `reported` snapshots."""
    s, r = models.BalanceSnapshot, models.AccountReport
    snapshots = select(s.as_of).where(s.account_id == account_id, s.kind == "reported")
    reports = select(r.as_of).where(r.account_id == account_id)
    rows = await session.scalars(union(snapshots, reports).order_by("as_of"))
    return list(rows)


async def reported_at(
    session: AsyncSession, account_id: UUID, at: datetime, wallet: str | None = None
) -> dict[UUID, Decimal]:
    """Each instrument's quantity in the account's report at `at` (the latest recorded of a
    time). A report is complete: an instrument it does not list is 0 there.

    A futures wallet's (`wallet`, `ACCOUNT_REPORTS`) is its wallet balance instead, the Options
    wallet's its margin balance (`_wallet_balances`), when its account was read then; a daily
    snapshot of the first sync was no read, and is a wallet balance itself; nor is the empty
    report of a wallet Portfolio Margin holds (`balances.empty_reports`), which is 0. The
    Portfolio Margin wallet's (`READ_REPORTS`) is its wallet balance net of its debt in the read
    made then (`_read_balances`)."""
    if wallet in ACCOUNT_REPORTS:
        found = await _wallet_balances(session, account_id, ACCOUNT_REPORTS[wallet], at)
        if found is not None:
            return found
    if wallet in READ_REPORTS:
        found = await _read_balances(session, account_id, READ_REPORTS[wallet], at)
        if found is not None:
            return found
    s = models.BalanceSnapshot
    rows = await session.execute(
        select(s.instrument_id, s.quantity)
        .where(s.account_id == account_id, s.kind == "reported", s.as_of == at)
        .ext(distinct_on(s.instrument_id))
        .order_by(s.instrument_id, s.recorded_at.desc(), s.id.desc())
    )
    return {row.instrument_id: row.quantity for row in rows}


async def _read_at(
    session: AsyncSession, connection_id: UUID | None, label: str, at: datetime
) -> UUID | None:
    """The raw record of the label's read made at `at` (its `read_from`), the latest if several;
    None when no read of the label was made then."""
    return await session.scalar(
        text(
            "select id from core.raw_records where connection_id = :c and record_type = :t"
            " and (content ->> 'read_from_ms')::bigint = :ms order by id desc limit 1"
        ),
        {"c": connection_id, "t": record_type(label), "ms": millis(at)},
    )


async def _wallet_balances(
    session: AsyncSession, account_id: UUID, report: tuple[str, str, str, str], at: datetime
) -> dict[UUID, Decimal] | None:
    """Each instrument's balance without unrealised profit (`report`'s column: a futures
    account's `wallet_balance`, the options account's `margin_balance`) in the account's typed
    report at `at`: the latest stored at or before it, since an answer is stored only when it
    changed (`balances.store_reports`), so the one before is what the read at `at` said. None
    when the account was not read at `at` (a daily snapshot, an empty report): nothing typed is
    stored then and no raw read of the label was made then. A report states the column for
    every asset (`balances` refuses one that does not)."""
    label, table, assets, column = report
    connection_id = await session.scalar(
        select(models.Account.source_connection_id).where(models.Account.id == account_id)
    )
    read = await session.scalar(
        text(
            f"select max(snapshot_at) from finance.{table}"
            " where connection_id = :c and snapshot_at <= :t"
        ),
        {"c": connection_id, "t": at},
    )
    if read is None:
        return None
    if read != at and await _read_at(session, connection_id, label, at) is None:
        return None
    rows = await session.execute(
        text(
            f"select i.instrument_id, sum(a.{column}) as held"
            f" from finance.{assets} a"
            " join finance.instrument_identifiers i"
            "  on i.scheme = 'binance_asset' and i.value = a.asset"
            " where a.connection_id = :c and a.snapshot_at = :s"
            " group by i.instrument_id"
        ),
        {"c": connection_id, "s": read},
    )
    return {row.instrument_id: Decimal(row.held) for row in rows if row.held != 0}


async def _read_balances(
    session: AsyncSession,
    account_id: UUID,
    report: tuple[str, tuple[tuple[str, int], ...]],
    at: datetime,
) -> dict[UUID, Decimal] | None:
    """Each instrument's `report` fields, signed (the Portfolio Margin wallet's
    `totalWalletBalance` less its debt), in the read made at `at` (its `read_from`, the
    snapshots' `as_of`), summed per asset; None when no read was made then (an empty report).
    An empty answer is an empty report. A read states each field for every asset (`balances`
    refuses one that does not)."""
    label, fields = report
    connection_id = await session.scalar(
        select(models.Account.source_connection_id).where(models.Account.id == account_id)
    )
    read = await _read_at(session, connection_id, label, at)
    if read is None:
        return None
    terms = " + ".join(f":s{i} * (e ->> :f{i})::numeric" for i in range(len(fields)))
    rows = await session.execute(
        text(
            f"select i.instrument_id, sum({terms}) as held"
            " from core.raw_records r"
            " cross join lateral jsonb_array_elements(r.content -> 'body') e"
            " join finance.instrument_identifiers i"
            "  on i.scheme = 'binance_asset' and i.value = e ->> 'asset'"
            " where r.id = :r group by i.instrument_id"
        ),
        {
            "r": read,
            **{f"f{i}": name for i, (name, _) in enumerate(fields)},
            **{f"s{i}": sign for i, (_, sign) in enumerate(fields)},
        },
    )
    return {row.instrument_id: Decimal(row.held) for row in rows if row.held != 0}


async def _posted(
    session: AsyncSession, account_id: UUID, after: datetime, through: datetime
) -> dict[UUID, Decimal]:
    t, p = models.Transaction, models.Posting
    rows = await session.execute(
        select(p.instrument_id, func.sum(p.quantity).label("total"))
        .join(t, t.id == p.transaction_id)
        .where(
            p.account_id == account_id,
            t.status != "void",
            t.occurred_at > after,
            t.occurred_at <= through,
        )
        .group_by(p.instrument_id)
    )
    return {row.instrument_id: row.total for row in rows}


async def wallets(session: AsyncSession, connection_id: UUID) -> dict[UUID, str]:
    """The connection's wallet accounts: account id to wallet key."""
    mapped = (await get_connection(session, connection_id)).config.get(ACCOUNT_MAP_KEY)
    found: dict[UUID, str] = {}
    for key, value in (mapped if isinstance(mapped, dict) else {}).items():
        if isinstance(value, str):
            try:
                found[UUID(value)] = key
            except ValueError:
                continue
    return found


async def asset_code(session: AsyncSession, instrument_id: UUID) -> str:
    """Binance's code of an instrument: its `binance_asset` identifier, else its symbol (a
    fiat currency)."""
    code = await session.scalar(
        text(
            "select value from finance.instrument_identifiers"
            " where instrument_id = :i and scheme = 'binance_asset' order by value limit 1"
        ),
        {"i": instrument_id},
    )
    if code is not None:
        return code
    return await session.scalar(
        text("select symbol from finance.instruments where id = :i"), {"i": instrument_id}
    )


async def _accruing(
    session: AsyncSession, connection_id: UUID, instrument_id: UUID, at: datetime
) -> Decimal:
    """The largest `REALTIME` reward of the asset in the 48 hours before `at`: how far Earn's
    position may be from its records, either way (design 5.11)."""
    largest = await session.scalar(
        text(
            "select max(rewards) from finance.binance_earn_flexible_rewards"
            " where connection_id = :c and asset = :a and type = :t"
            " and time > :since and time <= :at"
        ),
        {
            "c": connection_id,
            "a": await asset_code(session, instrument_id),
            "t": _REALTIME,
            "since": at - _ACCRUING,
            "at": at,
        },
    )
    return Decimal(largest) if largest is not None else Decimal(0)


async def queue_touched(ctx: CommandContext, connection_id: UUID, touched: Touched) -> None:
    """Add `touched` to `config["touched_pending"]`, in the caller's transaction (the one that
    commits the postings): per (account, instrument), the earliest and latest time touched."""
    if not touched:
        return
    config = (await get_connection(ctx.session, connection_id)).config
    held = config.get(TOUCHED_KEY)
    spans: dict[str, JSONValue] = dict(held) if isinstance(held, dict) else {}
    for account_id, instrument_id, at in touched:
        key, ms = f"{account_id}:{instrument_id}", millis(at)
        low, high = ms, ms
        span = spans.get(key)
        if isinstance(span, list) and len(span) == 2:
            first, last = span
            if isinstance(first, int) and isinstance(last, int):
                low, high = min(first, ms), max(last, ms)
        spans[key] = [low, high]
    if spans != held:
        await update_config(ctx, connection_id, {TOUCHED_KEY: spans})


def _pending(config: dict[str, JSONValue]) -> list[tuple[UUID, datetime, datetime]]:
    """The queued spans (`queue_touched`): (account, earliest, latest)."""
    held = config.get(TOUCHED_KEY)
    found: list[tuple[UUID, datetime, datetime]] = []
    for key, span in (held if isinstance(held, dict) else {}).items():
        if not (isinstance(span, list) and len(span) == 2):
            continue
        low, high = span
        try:
            account_id = UUID(key.split(":")[0])
        except ValueError:
            continue
        if isinstance(low, int) and isinstance(high, int):
            found.append((account_id, moment(low), moment(high)))
    return found


def reconciled_through(config: dict[str, JSONValue]) -> datetime | None:
    found = config.get(RECONCILED_KEY)
    if isinstance(found, int) and not isinstance(found, bool):
        return moment(found)
    return None


async def check(
    session: AsyncSession,
    connection_id: UUID,
    touched: Touched,
) -> list[Difference]:
    """The differences among the connection's intervals that end after
    `config["reconciled_through"]` (all of them when it is not set) and those holding one of
    `touched`'s (account, instrument, `occurred_at`) or a time of a span queued in
    `config["touched_pending"]`; every instrument of such an interval is checked."""
    config = (await get_connection(session, connection_id)).config
    through = reconciled_through(config)
    # Per account, spans of touched times: a point is a span of one moment.
    spans: defaultdict[UUID, list[tuple[datetime, datetime]]] = defaultdict(list)
    for account_id, _, at in touched:
        spans[account_id].append((at, at))
    for account_id, low, high in _pending(config):
        spans[account_id].append((low, high))
    accounts = list(await session.scalars(_owned(connection_id).order_by(models.Account.id)))
    owned = await wallets(session, connection_id)
    earn = {a for a, key in owned.items() if key == _EARN}
    decimals: dict[UUID, int] = {}
    found: list[Difference] = []
    with localcontext(prec=MAX_PREC):
        for account_id in accounts:
            times = await report_times(session, account_id)
            for from_at, to_at in itertools.pairwise(times):
                fresh = through is None or to_at > through
                held = any(low <= to_at and from_at < high for low, high in spans[account_id])
                if not fresh and not held:
                    continue
                wallet = owned.get(account_id)
                before = await reported_at(session, account_id, from_at, wallet)
                after = await reported_at(session, account_id, to_at, wallet)
                posted = await _posted(session, account_id, from_at, to_at)
                for instrument_id in sorted({*before, *after, *posted}):
                    zero = Decimal(0)
                    change = after.get(instrument_id, zero) - before.get(instrument_id, zero)
                    total = Decimal(posted.get(instrument_id, zero))
                    ahead = change - total
                    if instrument_id not in decimals:
                        decimals[instrument_id] = (
                            await session.get_one(models.Instrument, instrument_id)
                        ).decimals
                    if ahead.copy_abs() <= tolerance(decimals[instrument_id]):
                        continue
                    if account_id in earn and ahead.copy_abs() <= await _accruing(
                        session, connection_id, instrument_id, to_at
                    ):
                        continue
                    found.append(
                        Difference(account_id, instrument_id, from_at, to_at, change, total)
                    )
    return sorted(found, key=lambda d: (d.to_at, d.account_id, d.instrument_id))


async def newest_report(session: AsyncSession, connection_id: UUID) -> datetime | None:
    """The newest report time of any of the connection's accounts (`report_times`' sources)."""
    s, r = models.BalanceSnapshot, models.AccountReport
    owned = _owned(connection_id)
    snapshot = await session.scalar(
        select(func.max(s.as_of)).where(s.kind == "reported", s.account_id.in_(owned))
    )
    report = await session.scalar(select(func.max(r.as_of)).where(r.account_id.in_(owned)))
    found = [at for at in (snapshot, report) if at is not None]
    return max(found) if found else None


def _owned(connection_id: UUID) -> Select[UUID]:
    a = models.Account
    return select(a.id).where(a.source_connection_id == connection_id)


# What is stored.


async def _stored(
    session: AsyncSession, connection_id: UUID
) -> dict[tuple[UUID, UUID, datetime, datetime], ReconciliationDifference]:
    d = ReconciliationDifference
    rows = await session.scalars(select(d).where(d.account_id.in_(_owned(connection_id))))
    return {(r.account_id, r.instrument_id, r.from_at, r.to_at): r for r in rows}


async def open_differences(session: AsyncSession, connection_id: UUID) -> list[Difference]:
    """The connection's differences that still hold, oldest interval first."""
    d = ReconciliationDifference
    rows = await session.execute(
        select(d.account_id, d.instrument_id, d.from_at, d.to_at, d.reported_change, d.posted_sum)
        .where(d.closed_at.is_(None), d.account_id.in_(_owned(connection_id)))
        .order_by(d.to_at, d.from_at, d.account_id, d.instrument_id)
    )
    return [Difference(*row) for row in rows]


def _change(row: ReconciliationDifference) -> dict[str, JSONValue]:
    return {
        "account_id": str(row.account_id),
        "instrument_id": str(row.instrument_id),
        "from_at": row.from_at.isoformat(),
        "to_at": row.to_at.isoformat(),
        "reported_change": to_plain_str(row.reported_change),
        "posted_sum": to_plain_str(row.posted_sum),
        "closed_at": row.closed_at.isoformat() if row.closed_at is not None else None,
    }


def _require(difference: Difference) -> None:
    """What the table's CHECKs say, checked before writing."""
    if not difference.from_at < difference.to_at:
        raise DisplayableError("a Binance reconciliation interval must end after it starts")
    if not (difference.reported_change.is_finite() and difference.posted_sum.is_finite()):
        raise DisplayableError("a Binance reconciliation difference must be a finite number")


async def warning_text(session: AsyncSession, connection_id: UUID, difference: Difference) -> str:
    """`Binance <wallet>: <asset> changed by something no record explains between <from> and
    <to> (UTC)`: no amount."""
    key = (await wallets(session, connection_id)).get(difference.account_id)
    if key is not None:
        wallet = f"Binance {wallet_name(key)}"
    else:
        wallet = await session.scalar(
            text("select name from finance.accounts where id = :a"), {"a": difference.account_id}
        )
    code = await asset_code(session, difference.instrument_id)
    since = difference.from_at.astimezone(UTC).strftime(_SHOWN)
    until = difference.to_at.astimezone(UTC).strftime(_SHOWN)
    return (
        f"{wallet}: {code} changed by something no record explains"
        f" between {since} and {until} (UTC)"
    )


async def reconcile(run: Run) -> tuple[list[Difference], list[Difference]]:
    """Check the run's intervals (its `touched` and every stored difference still open), store
    the new differences, close those that no longer hold, keep the connection's warnings to
    the open ones and queue a line for each new one; (opened, closed).

    A difference found again in an interval whose earlier one was closed is opened again (and
    told again); one still open whose figures moved is updated, not told again."""
    session, connection_id = run.session, run.connection.id
    stored = await _stored(session, connection_id)
    still = [row for row in stored.values() if row.closed_at is None]
    touched = {*run.touched, *((r.account_id, r.instrument_id, r.to_at) for r in still)}
    found = await check(session, connection_id, touched)
    now = await session.scalar(select(func.now()))
    assert now is not None
    opened: list[Difference] = []
    closed: list[Difference] = []
    for difference in found:
        _require(difference)
        row = stored.get(difference.key)
        new = row is None
        if row is None:
            row = ReconciliationDifference(
                account_id=difference.account_id,
                instrument_id=difference.instrument_id,
                from_at=difference.from_at,
                to_at=difference.to_at,
                opened_at=now,
            )
            session.add(row)
        elif row.closed_at is None and (row.reported_change, row.posted_sum) == (
            difference.reported_change,
            difference.posted_sum,
        ):
            continue
        if new or row.closed_at is not None:
            opened.append(difference)
            row.opened_at = now
        row.reported_change = difference.reported_change
        row.posted_sum = difference.posted_sum
        row.closed_at = None
        await session.flush()
        run.command.record_change(f"binance_difference:{row.id}", _change(row))
    held = {difference.key for difference in found}
    for row in still:
        if (row.account_id, row.instrument_id, row.from_at, row.to_at) in held:
            continue
        row.closed_at = now
        await session.flush()
        run.command.record_change(f"binance_difference:{row.id}", _change(row))
        closed.append(
            Difference(
                row.account_id,
                row.instrument_id,
                row.from_at,
                row.to_at,
                row.reported_change,
                row.posted_sum,
            )
        )
    if opened:
        lines = [await warning_text(session, connection_id, d) for d in opened]
        await notices.queue(run.command, connection_id, lines=lines)
    await _set_warnings(run)
    # Every interval up to the newest report is checked now, and every queued span: the next
    # run starts after them.
    newest = await newest_report(session, connection_id)
    config = (await get_connection(session, connection_id)).config
    values: dict[str, JSONValue] = {}
    if newest is not None and reconciled_through(config) != newest:
        values[RECONCILED_KEY] = millis(newest)
    remove = [TOUCHED_KEY] if TOUCHED_KEY in config else []
    if values or remove:
        await update_config(run.command, connection_id, values, remove=remove)
    return opened, closed


async def _set_warnings(run: Run) -> None:
    """The connection's difference warnings (those `warning_text` writes) are its open
    differences; every other warning is kept."""
    session, connection_id = run.session, run.connection.id
    connection = await get_connection(session, connection_id)
    kept = [w for w in connection.warnings if not _WARNING.fullmatch(w)]
    texts = [
        await warning_text(session, connection_id, d)
        for d in await open_differences(session, connection_id)
    ]
    warnings = kept + list(dict.fromkeys(texts))
    if warnings != connection.warnings:
        await set_warnings(run.command, connection_id, warnings)
