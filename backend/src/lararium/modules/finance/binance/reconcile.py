"""Binance's reported balances against the records posted (design 5.11, plan Decision 11).

An interval is two consecutive `reported` snapshots of one account and instrument: its reported
change is the second quantity less the first, its posted sum the account's non-void postings
of the instrument with `as_of(before) < occurred_at <= as_of(after)` (what the `hybrid` rule
adds after the first snapshot and the second already holds). A difference beyond the
instrument's tolerance is stored, named in the connection's warnings and told once; it is never
corrected. One that no longer holds (the record that explains it arrived) is closed.

A run checks the newest interval of every account and instrument and every interval holding a
posting the run created, changed or removed, so a late record closes its interval and a change
to an old record reopens it. Warnings and messages name the wallet, the asset and the two
times, never an amount.
"""

from collections.abc import Collection
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import MAX_PREC, Decimal, localcontext
from uuid import UUID

from sqlalchemy import Select, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from lararium.core.connections import get_connection, set_warnings
from lararium.core.errors import DisplayableError
from lararium.core.json_types import JSONValue
from lararium.modules.finance import models
from lararium.modules.finance.binance import notices
from lararium.modules.finance.binance.accounts import ACCOUNT_MAP_KEY, wallet_name
from lararium.modules.finance.binance.models import ReconciliationDifference
from lararium.modules.finance.binance.records import Run
from lararium.modules.finance.money import to_plain_str, tolerance

# (account, instrument, occurred_at) of a posting a run created, changed or removed.
type Point = tuple[UUID, UUID, datetime]
type Touched = Collection[Point]

WARNING_PREFIX = "Binance "
_FLAGS_KEY = "flags"
# Earn's flexible position runs ahead of its records by the reward still accruing.
_EARN = "earn"
_REALTIME = "REALTIME"
_ACCRUING = timedelta(hours=48)
_SHOWN = "%Y-%m-%d %H:%M:%S"


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


# The intervals, and what was posted in each.

# Phase A: every wallet's reported change is its snapshots'. Phase B gives the futures wallets
# theirs from `wallet_balance` (Decision 11: their snapshot also carries unrealised profit), as
# a second source beside this one, with those accounts left out of it.
_SNAPSHOT_INTERVALS = """
with reported as (
  select distinct on (s.account_id, s.instrument_id, s.as_of)
    s.account_id, s.instrument_id, s.as_of, s.quantity
  from finance.balance_snapshots s
  join finance.accounts a on a.id = s.account_id
  where a.source_connection_id = :c and s.kind = 'reported'
  order by s.account_id, s.instrument_id, s.as_of, s.recorded_at desc, s.id desc
),
intervals as (
  select account_id, instrument_id,
    lag(as_of) over w as from_at, lag(quantity) over w as from_quantity,
    as_of as to_at, quantity as to_quantity,
    row_number() over (
      partition by account_id, instrument_id order by as_of desc
    ) as newest
  from reported
  window w as (partition by account_id, instrument_id order by as_of)
),
touched as (
  select * from unnest(
    cast(:accounts as uuid[]), cast(:instruments as uuid[]), cast(:times as timestamptz[])
  ) as t (account_id, instrument_id, at)
)
select v.account_id, v.instrument_id, v.from_at, v.to_at,
  v.to_quantity - v.from_quantity as reported_change,
  coalesce((
    select sum(p.quantity) from finance.postings p
    join finance.transactions t on t.id = p.transaction_id
    where p.account_id = v.account_id and p.instrument_id = v.instrument_id
      and t.status <> 'void' and t.occurred_at > v.from_at and t.occurred_at <= v.to_at
  ), 0) as posted_sum,
  i.decimals
from intervals v
join finance.instruments i on i.id = v.instrument_id
where v.from_at is not null and (
  v.newest = 1 or exists (
    select 1 from touched t
    where t.account_id = v.account_id and t.instrument_id = v.instrument_id
      and t.at > v.from_at and t.at <= v.to_at
  )
)
order by v.to_at, v.account_id, v.instrument_id
"""


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
    position may run ahead of its records (design 5.11)."""
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


async def check(
    session: AsyncSession,
    connection_id: UUID,
    touched: Touched,
) -> list[Difference]:
    """The differences among the newest interval of every (account, instrument) of the
    connection and every interval holding one of `touched`'s (account, instrument,
    `occurred_at`)."""
    points = sorted(touched)
    rows = await session.execute(
        text(_SNAPSHOT_INTERVALS),
        {
            "c": connection_id,
            "accounts": [a for a, _, _ in points],
            "instruments": [i for _, i, _ in points],
            "times": [at for _, _, at in points],
        },
    )
    earn = {a for a, key in (await wallets(session, connection_id)).items() if key == _EARN}
    found: list[Difference] = []
    with localcontext(prec=MAX_PREC):
        for row in rows.all():
            reported, posted = Decimal(row.reported_change), Decimal(row.posted_sum)
            ahead = reported - posted
            if ahead.copy_abs() <= tolerance(row.decimals):
                continue
            if row.account_id in earn and 0 < ahead <= await _accruing(
                session, connection_id, row.instrument_id, row.to_at
            ):
                continue
            found.append(
                Difference(
                    row.account_id, row.instrument_id, row.from_at, row.to_at, reported, posted
                )
            )
    return found


# What is stored.


def _owned(connection_id: UUID) -> Select[UUID]:
    a = models.Account
    return select(a.id).where(a.source_connection_id == connection_id)


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
    return opened, closed


async def _set_warnings(run: Run) -> None:
    """The connection's warnings beginning `Binance ` that are not its flags are its open
    differences."""
    session, connection_id = run.session, run.connection.id
    connection = await get_connection(session, connection_id)
    flags = connection.config.get(_FLAGS_KEY)
    flagged = {
        notices.FLAG_PREFIX + flag
        for flag in (flags if isinstance(flags, list) else [])
        if isinstance(flag, str)
    }
    kept = [w for w in connection.warnings if not w.startswith(WARNING_PREFIX) or w in flagged]
    texts = [
        await warning_text(session, connection_id, d)
        for d in await open_differences(session, connection_id)
    ]
    warnings = kept + list(dict.fromkeys(texts))
    if warnings != connection.warnings:
        await set_warnings(run.command, connection_id, warnings)
