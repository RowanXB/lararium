"""Property tests pinning the Binance ledger's invariants on generated account histories
(strategies.py), each fetched by a run of whole syncs against `FakeBinance` at drawn moments.

The fake is the history at Binance's clock: every request first sets what it lists (the rows
listed by then, in their state then: a deposit credited, a withdrawal failed) and what each
wallet reports (the true balance then). The first sync walks back to the fetch floor and reads
the daily Spot snapshots; the later ones walk forwards from their cursors with the 7-day
overlap. A record the test removes is the history's still (its effects are in every report)
but the fake never lists it.

- Windows and order: two schedules of the same history give the same transactions, postings
  and typed record rows; only an opening dated at its wallet's first report (no record, or
  none until the day after it; design 5.8) is dated whenever that report was.
- Balance: after every sync, every transaction balances.
- Balances: on every drawn day, `v1_balances_asof` is the true balance at the end of that day,
  for every wallet from the day its ledger starts (the opening's date, or its first report).
- Reconciliation: a consistent history leaves no difference open and no difference warning.
- A removed record: a difference in each interval of two reports of the record's account in
  which it changed an instrument, of what it changed there (none where the Earn allowance
  covers it; none where a debt grew, which is a flexible loan's interest; none before the
  account's first report, where the opening holds it): exactly one
  for a record that moves one instrument of one wallet, one per (account, instrument) it moved
  for a record that moves two.
- Regeneration: with the connection's transactions and typed record rows deleted, normalising
  every raw record again (and setting the openings) rebuilds them identically.
- Opening: with the first records hidden, each wallet's opening is what it held before the
  history plus what the hidden records did before its first report.

Each example runs in a transaction of its own on a connection of its own, rolled back at the
end; rows are compared by external id and content, never by UUID.
"""

import itertools
import uuid
from collections.abc import AsyncGenerator, AsyncIterator, Callable, Iterable
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID

import httpx
import pytest
from hypothesis import event, given, note, settings
from hypothesis import strategies as st
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession, async_sessionmaker

from lararium.core.actors import Actor
from lararium.core.commands import CommandContext, command_context
from lararium.core.connections import (
    create_connection,
    get_connection,
    request_sync,
    store_credential,
)
from lararium.core.connectors import SyncContext
from lararium.db import create_engine
from lararium.modules import finance
from lararium.modules.finance.binance import layout
from lararium.modules.finance.binance import sync as sync_module
from lararium.modules.finance.binance.fetch import HISTORY_LABELS, TRADES_LABEL
from lararium.modules.finance.binance.normalize import regenerate
from lararium.modules.finance.binance.parse import ApiValue
from lararium.modules.finance.binance.reconcile import report_times
from lararium.modules.finance.binance.sync import BinanceConnector
from lararium.modules.finance.ledger import find_unbalanced_transactions

from .fixtures import FakeBinance, build_row
from .strategies import (
    ACCOUNT_NAMES,
    ASSETS,
    DEBTS,
    PAIRS,
    SINGLE_SIDED,
    TWO_SIDED,
    WALLET_KEYS,
    History,
    Record,
    Schedule,
    collateral_of,
    histories,
    holdings_of,
    schedule,
)
from .syncing import (
    API_KEY,
    API_SECRET,
    KEY,
    UID,
    Clock,
    RecordingNotifier,
    run_binance_sync,
)

USER = Actor("user", "test")
CONNECTOR = Actor("connector", "binance")
# Columns that differ between two runs of the same input without saying anything about it.
VOLATILE = frozenset({"id", "created_at", "updated_at", "recorded_at", "fetched_at"})
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_CHANGED = "changed by something no record explains"
# The history labels with records of their own (the daily snapshots are a report).
RECORD_LABELS = [label for label in [*HISTORY_LABELS, TRADES_LABEL] if label != "snapshot"]
_ACCRUING_MS = 48 * 3_600_000
# The profile's examples (tests/finance/conftest.py): "ci" by default, "deep" with
# HYPOTHESIS_PROFILE=deep (HYPOTHESIS_MAX_EXAMPLES of them, random).
EXAMPLES = settings(max_examples=settings().max_examples)


def _ms(at: datetime) -> int:
    return (at - _EPOCH) // timedelta(milliseconds=1)


def _at(ms: int) -> datetime:
    return _EPOCH + timedelta(milliseconds=ms)


def _day_end_ms(day: date) -> int:
    return _ms(datetime.combine(day + timedelta(days=1), datetime.min.time(), UTC)) - 1


@pytest.fixture
async def connection(backend_url: str, migrated: None) -> AsyncIterator[AsyncConnection]:
    engine = create_engine(backend_url)
    try:
        async with engine.connect() as conn:
            yield conn
    finally:
        await engine.dispose()


# The fake as the history at Binance's clock.

# The variant each label's row is listed under: a transfer under its own type, a dust
# conversion under Spot (the only wallet the histories convert dust on).
_ASKED_FOR: dict[str, Callable[[dict[str, ApiValue]], dict[str, str]]] = {
    "universal-transfer": lambda row: {"type": str(row["type"])},
    "dust": lambda row: {"accountType": "SPOT"},
}


class World:
    """Sets, before each request, what the fake lists and reports at that moment."""

    def __init__(self, history: History, hidden: Iterable[Record], clock: Clock) -> None:
        self.history = history
        self.clock = clock
        hidden_ids = {id(record) for record in hidden}
        self.listed = [r for r in history.records if id(r) not in hidden_ids]
        self.fake = FakeBinance(now=clock, secret=API_SECRET)
        self.fake.set_rows(
            "exchange-info",
            [
                build_row("exchange-info", symbol=symbol, baseAsset=base, quoteAsset=quote)
                for symbol, base, quote in PAIRS
            ],
        )
        self.fake.set_rows("snapshot", self._daily(), when={"type": "SPOT"})

    def _daily(self) -> list[dict[str, ApiValue]]:
        """A Spot snapshot at 23:59:59 of every day, from the day before the history to three
        days after it; the fake lists only those up to the request's end."""
        first = _at(self.history.start_ms).date() - timedelta(days=1)
        last = _at(self.history.end_ms).date() + timedelta(days=3)
        rows: list[dict[str, ApiValue]] = []
        day = first
        while day <= last:
            at = _day_end_ms(day) - 999
            held = holdings_of(self.history.balance(at), "spot")
            row = build_row("snapshot", type="spot", updateTime=at)
            data = dict(row["data"])  # pyright: ignore[reportArgumentType,reportCallIssue]
            data["balances"] = [
                {"asset": asset, "free": format(q, "f"), "locked": "0"}
                for asset, q in sorted(held.items())
            ]
            rows.append({**row, "data": data})
            day += timedelta(days=1)
        return rows

    def update(self, now_ms: int) -> None:
        listed: dict[str, list[dict[str, ApiValue]]] = {label: [] for label in RECORD_LABELS}
        for record in self.listed:
            for label, versions in record.rows:
                shown = [row for at, row in versions if at <= now_ms]
                if shown:
                    listed[label].append(dict(shown[-1]))
        for label, rows in listed.items():
            asked = _ASKED_FOR.get(label)
            if asked is None:
                self.fake.set_rows(label, rows)
                continue
            # A row answers only the request for its own variant.
            self.fake.set_rows(label, [])
            for row in rows:
                self.fake.add(label, row, when=asked(row))
        held = self.history.balance(now_ms)
        spot, funding, earn, loans = (holdings_of(held, w) for w in WALLET_KEYS)
        self.fake.set_rows(
            "loan-flexible-ongoing",
            [
                build_row(
                    "loan-flexible-ongoing",
                    loanCoin=a,
                    totalDebt=format(-q, "f"),
                    collateralCoin=collateral_of(a),
                    collateralAmount="1",
                )
                for a, q in sorted(loans.items())
            ],
        )
        self.fake.set_rows(
            "spot-account",
            [
                build_row("spot-account", asset=a, free=format(q, "f"), locked="0")
                for a, q in sorted(spot.items())
            ],
        )
        self.fake.set_rows(
            "funding-asset",
            [
                build_row(
                    "funding-asset",
                    asset=a,
                    free=format(q, "f"),
                    locked="0",
                    freeze="0",
                    withdrawing="0",
                )
                for a, q in sorted(funding.items())
            ],
        )
        earn_listed: list[ApiValue] = [
            {
                "asset": a,
                "assetName": a,
                "free": format(q, "f"),
                "locked": "0",
                "freeze": "0",
                "withdrawing": "0",
                "btcValuation": "0",
            }
            for a, q in sorted(earn.items())
        ]
        self.fake.set_rows(
            "wallet-balance",
            [
                build_row(
                    "wallet-balance", walletName="Spot", balance=_any(spot), assetBalances=[]
                ),
                build_row(
                    "wallet-balance", walletName="Funding", balance=_any(funding), assetBalances=[]
                ),
                build_row(
                    "wallet-balance",
                    walletName="Earn",
                    balance=_any(earn),
                    assetBalances=earn_listed,
                ),
            ],
        )

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.update(self.clock())
        return self.fake.handler(request)


def _any(held: dict[str, Decimal]) -> str:
    return "1" if held else "0"


# One connection run through a schedule, in a transaction of its own.


class Env:
    def __init__(self, session: AsyncSession, world: World, clock: Clock) -> None:
        self.session = session
        self.world = world
        self.clock = clock
        self.history = world.history
        self.connection_id: UUID = uuid.uuid4()

    async def run(self, fn: Callable[..., Any], *args: Any) -> Any:
        async with command_context(self.session, USER, "test", {}) as ctx:
            return await fn(ctx, *args)

    async def setup(self) -> None:
        async def make(ctx: CommandContext) -> UUID:
            connection = await create_connection(
                ctx,
                connector="binance",
                display_name="Binance",
                config={"key_type": "hmac", "uid": UID},
            )
            await store_credential(ctx, KEY, connection.id, "api_key", API_KEY, None)
            await store_credential(ctx, KEY, connection.id, "api_secret", API_SECRET, None)
            await ctx.session.execute(
                text("update core.connections set status = 'healthy' where id = :id"),
                {"id": connection.id},
            )
            return connection.id

        self.connection_id = await self.run(make)

    async def sync(self, at_ms: int) -> None:
        self.clock.ms = max(self.clock.ms, at_ms)
        await self.run(request_sync, self.connection_id)
        after = await run_binance_sync(
            self.session, self.connection_id, self.world.handler, notifier=RecordingNotifier()
        )
        assert after.status == "healthy", after.last_error
        assert await find_unbalanced_transactions(self.session) == []

    async def play(self, plan: Schedule) -> None:
        for at in plan.syncs:
            await self.sync(at)

    async def context(self) -> SyncContext:
        return SyncContext(
            command=CommandContext(self.session, CONNECTOR, uuid.uuid4()),
            connection=await get_connection(self.session, self.connection_id),
            sync_id=uuid.uuid4(),
            master_key=KEY,
            http=httpx.AsyncClient(transport=httpx.MockTransport(_refuse)),
            notifier=RecordingNotifier(),
            session_factory=async_sessionmaker(bind=self.session.bind),
        )

    # What is read back.

    async def accounts(self) -> dict[str, UUID]:
        rows = await self.session.execute(
            text("select id, name from finance.accounts where source_connection_id = :c"),
            {"c": self.connection_id},
        )
        return {row.name: row.id for row in rows}

    async def _labels(self) -> dict[UUID, str]:
        labels: dict[UUID, str] = {self.connection_id: "connection"}
        queries = {
            "account": "select id, name from finance.accounts",
            "instrument": "select id, symbol from finance.instruments",
            "identifier": "select id, kind || ':' || value from finance.account_identifiers",
            "entity": "select id, kind || ':' || name from core.entities",
        }
        for kind, query in queries.items():
            for row in await self.session.execute(text(query)):
                labels[row[0]] = f"{kind} {row[1]}"
        for row in await self.session.execute(
            text("select id, external_id from finance.transactions where connection_id = :c"),
            {"c": self.connection_id},
        ):
            labels[row[0]] = f"transaction {_external_id(row[1], labels)}"
        return labels

    async def _rows(self, query: str, labels: dict[UUID, str]) -> list[str]:
        rows = await self.session.execute(text(query), {"c": self.connection_id})
        return sorted(
            repr(
                sorted(
                    (column, _label(value, labels))
                    for column, value in row._mapping.items()  # pyright: ignore[reportPrivateUsage]
                    if column not in VOLATILE
                )
            )
            for row in rows
        )

    async def ledger(self, *, by_first_report: bool = False) -> dict[str, Any]:
        """The connection's transactions with their postings, by external id; an opening a
        later run voided holds nothing and is left out.

        `by_first_report`: an opening is dated the day before its wallet's earliest record, or
        at its first report when that is earlier or there is no record (design 5.8); the first
        report depends on when the syncs ran, so the date is checked to be that and an opening
        dated at the first report is shown as "first report"."""
        labels = await self._labels()
        dates = await self._opening_dates() if by_first_report else {}
        found: dict[str, Any] = {}
        transactions = await self.session.execute(
            text(
                "select * from finance.transactions where connection_id = :c"
                " and not (external_id like 'opening:%' and status = 'void')"
            ),
            {"c": self.connection_id},
        )
        for t in transactions:
            mapping = dict(t._mapping)  # pyright: ignore[reportPrivateUsage]
            postings = await self._rows(
                f"select * from finance.postings where transaction_id = '{mapping['id']}'",
                labels,
            )
            key = _external_id(mapping["external_id"], labels)
            wallet = key.split(":")[1] if key and key.startswith("opening:") else None
            if wallet in dates:
                at, first = dates[wallet]
                assert mapping["occurred_at"] == at, key
                assert mapping["booking_date"] == at.astimezone(UTC).date(), key
                if at == first:
                    mapping |= {"occurred_at": "first report", "booking_date": "first report"}
            found[key] = (
                sorted(
                    (column, _label(value, labels))
                    for column, value in {**mapping, "external_id": key}.items()
                    if column not in VOLATILE
                ),
                postings,
            )
        return found

    async def _opening_dates(self) -> dict[str, tuple[datetime, datetime]]:
        """Each reported wallet's opening date as design 5.8 has it: the day before its earliest
        record (00:00 UTC), or its first report when that is earlier or it has no record; and
        that first report."""
        accounts = await self.accounts()
        found: dict[str, tuple[datetime, datetime]] = {}
        for wallet, name in ACCOUNT_NAMES.items():
            if name not in accounts:
                continue
            times = await report_times(self.session, accounts[name])
            if not times:
                continue
            day = await self.session.scalar(
                text(
                    "select min(t.booking_date) from finance.postings p"
                    " join finance.transactions t on t.id = p.transaction_id"
                    " where p.account_id = :a and t.status <> 'void'"
                    " and t.external_id not like 'opening:%'"
                ),
                {"a": accounts[name]},
            )
            first = times[0]
            if day is None:
                found[wallet] = (first, first)
            else:
                before = datetime.combine(day - timedelta(days=1), datetime.min.time(), UTC)
                found[wallet] = (min(before, first), first)
        return found

    async def typed(self) -> dict[str, list[str]]:
        """Every typed record row of the connection, per table."""
        labels = await self._labels()
        tables: dict[str, list[str]] = {}
        for label in RECORD_LABELS:
            table = layout.layout(label)[0].name
            tables[table] = await self._rows(
                f"select * from finance.{table} where connection_id = :c", labels
            )
        return tables

    async def open_differences(self) -> list[tuple[Any, ...]]:
        rows = await self.session.execute(
            text(
                "select a.name, i.symbol, d.from_at, d.to_at, d.reported_change, d.posted_sum"
                " from finance.binance_reconciliation_differences d"
                " join finance.accounts a on a.id = d.account_id"
                " join finance.instruments i on i.id = d.instrument_id"
                " where a.source_connection_id = :c and d.closed_at is null"
                " order by d.from_at, a.name, i.symbol"
            ),
            {"c": self.connection_id},
        )
        return [
            (
                r.name,
                r.symbol,
                r.from_at,
                r.to_at,
                r.reported_change.normalize(),
                r.posted_sum.normalize(),
            )
            for r in rows
        ]

    async def openings(self) -> dict[tuple[str, str], Decimal]:
        """Each live opening's quantity on its wallet, by (wallet key, asset)."""
        rows = await self.session.execute(
            text(
                "select a.name, i.symbol, p.quantity from finance.transactions t"
                " join finance.postings p on p.transaction_id = t.id"
                " join finance.accounts a on a.id = p.account_id"
                " join finance.instruments i on i.id = p.instrument_id"
                " where t.connection_id = :c and t.external_id like 'opening:%'"
                " and t.status <> 'void' and a.source_connection_id = :c"
            ),
            {"c": self.connection_id},
        )
        names = {name: key for key, name in ACCOUNT_NAMES.items()}
        return {(names[r.name], r.symbol): r.quantity for r in rows}

    async def reports(self) -> dict[str, list[datetime]]:
        """Each wallet's report times, by wallet key."""
        accounts = await self.accounts()
        found: dict[str, list[datetime]] = {}
        for wallet, name in ACCOUNT_NAMES.items():
            if name in accounts:
                found[wallet] = await report_times(self.session, accounts[name])
        return found

    async def balances(self, day: date) -> dict[tuple[str, str], Decimal]:
        rows = await self.session.execute(
            text(
                "select a.name, i.symbol, b.quantity from finance.v1_balances_asof(:d) b"
                " join finance.accounts a on a.id = b.account_id"
                " join finance.instruments i on i.id = b.instrument_id"
                " where a.source_connection_id = :c"
            ),
            {"d": day, "c": self.connection_id},
        )
        return {(r.name, r.symbol): r.quantity for r in rows if r.quantity != 0}

    async def starts(self) -> dict[str, date]:
        """The day each wallet's ledger starts: its opening's date or earliest record's
        booking date, or its first report's date."""
        accounts = await self.accounts()
        starts: dict[str, date] = {}
        for wallet, name in ACCOUNT_NAMES.items():
            if name not in accounts:
                continue
            booked = await self.session.scalar(
                text(
                    "select min(t.booking_date) from finance.transactions t"
                    " join finance.postings p on p.transaction_id = t.id"
                    " where p.account_id = :a and t.status <> 'void'"
                ),
                {"a": accounts[name]},
            )
            times = await report_times(self.session, accounts[name])
            candidates = [d for d in (booked, times[0].date() if times else None) if d]
            if candidates:
                starts[wallet] = min(candidates)
        return starts

    async def assert_balances_are_the_truth(self) -> None:
        """After a sync later than every record: each wallet's balance is what Binance reports
        it holds (the history's balance at its end)."""
        found = await self.balances(_at(self.clock.ms).date() + timedelta(days=1))
        true = self.history.balance(self.history.end_ms)
        for wallet, name in ACCOUNT_NAMES.items():
            shown = {a: q for (n, a), q in found.items() if n == name}
            assert shown == holdings_of(true, wallet), wallet

    async def reads_made_again(self) -> bool:
        """Whether a sync stored a balance read a snapshot comes from more than once: a
        movement fell into it and it was made again."""
        again = await self.session.scalar(
            text(
                "select exists (select 1 from core.raw_records where connection_id = :c"
                " and record_type in ('binance.spot_account', 'binance.funding_asset',"
                " 'binance.wallet_balance') group by sync_id, record_type having count(*) > 1)"
            ),
            {"c": self.connection_id},
        )
        return again is True

    async def delete_derived(self) -> None:
        """What normalising writes from the raw records: the transactions and the typed
        record rows."""
        c = {"c": self.connection_id}
        for label in RECORD_LABELS:
            table = layout.layout(label)[0].name
            await self.session.execute(
                text(f"delete from finance.{table} where connection_id = :c"), c
            )
        await self.session.execute(
            text("delete from finance.transactions where connection_id = :c"), c
        )


def _refuse(request: httpx.Request) -> httpx.Response:
    raise AssertionError("no HTTP here")


def _external_id(external_id: str | None, labels: dict[UUID, str]) -> str | None:
    """An opening's key names its instrument by UUID: by its label instead."""
    if external_id is not None and external_id.startswith("opening:"):
        prefix, _, instrument = external_id.rpartition(":")
        return f"{prefix}:{labels[UUID(instrument)]}"
    return external_id


def _label(value: Any, labels: dict[UUID, str]) -> Any:
    if isinstance(value, UUID):
        return labels.get(value, "?unknown")
    if isinstance(value, Decimal):
        return value.normalize()
    return value


@asynccontextmanager
async def isolated(
    conn: AsyncConnection, history: History, hidden: Iterable[Record] = ()
) -> AsyncGenerator[Env]:
    clock = Clock(history.start_ms)
    installed = BinanceConnector(clock=clock, sleep=clock.sleep)
    before = finance.MODULE.connectors.get("binance")
    finance.MODULE.connectors["binance"] = installed  # pyright: ignore[reportArgumentType]
    outer = await conn.begin()
    session = AsyncSession(bind=conn, join_transaction_mode="create_savepoint")
    try:
        env = Env(session, World(history, hidden, clock), clock)
        await env.setup()
        yield env
    finally:
        await session.close()
        await outer.rollback()
        if before is not None:
            finance.MODULE.connectors["binance"] = before


def _notes(history: History, *plans: Schedule) -> None:
    note(history.describe())
    for plan in plans:
        note(f"syncs: {plan.describe()}")


# The properties.


@EXAMPLES
@given(history=histories(), data=st.data())
async def test_ledger_is_independent_of_windows_and_order(
    connection: AsyncConnection, history: History, data: st.DataObject
) -> None:
    first = data.draw(schedule(history), label="first")
    second = data.draw(schedule(history), label="second")
    _notes(history, first, second)
    event(f"syncs: {min(len(first.syncs), 5)}")
    results = []
    for plan in (first, second):
        async with isolated(connection, history) as env:
            await env.play(plan)
            results.append((await env.ledger(by_first_report=True), await env.typed()))
    (ledger_one, typed_one), (ledger_two, typed_two) = results
    assert sorted(ledger_one.keys() - ledger_two.keys()) == []
    assert sorted(ledger_two.keys() - ledger_one.keys()) == []
    assert ledger_one == ledger_two
    assert typed_one == typed_two


@EXAMPLES
@given(history=histories(), data=st.data())
async def test_every_transaction_balances(
    connection: AsyncConnection, history: History, data: st.DataObject
) -> None:
    plan = data.draw(schedule(history), label="schedule")
    _notes(history, plan)
    async with isolated(connection, history) as env:
        await env.play(plan)  # checks it after every sync
        assert await find_unbalanced_transactions(env.session) == []
        event(f"reads made again: {await env.reads_made_again()}")


@EXAMPLES
@given(history=histories(), data=st.data())
async def test_balance_is_the_latest_report_plus_later_records(
    connection: AsyncConnection, history: History, data: st.DataObject
) -> None:
    plan = data.draw(schedule(history), label="schedule")
    _notes(history, plan)
    first_day = _at(history.start_ms).date()
    last_day = _at(plan.syncs[-1]).date()
    span = (last_day - first_day).days
    offsets = data.draw(st.lists(st.integers(0, span), min_size=1, max_size=6, unique=True))
    days = sorted(first_day + timedelta(days=n) for n in offsets)
    note(f"days: {', '.join(d.isoformat() for d in days)}")
    async with isolated(connection, history) as env:
        await env.play(plan)
        starts = await env.starts()
        for day in days:
            true = history.balance(_day_end_ms(day))
            found = await env.balances(day)
            for wallet, name in ACCOUNT_NAMES.items():
                start = starts.get(wallet)
                expected = {a: q for (w, a), q in true.items() if w == wallet}
                if start is None:
                    # No account: the wallet never held anything the syncs could see.
                    assert expected == {}, (day, wallet)
                    continue
                if day < start:
                    continue
                shown = {a: q for (n, a), q in found.items() if n == name}
                assert shown == expected, (day, wallet)


@EXAMPLES
@given(history=histories(), data=st.data())
async def test_consistent_history_has_no_difference(
    connection: AsyncConnection, history: History, data: st.DataObject
) -> None:
    plan = data.draw(schedule(history), label="schedule")
    _notes(history, plan)
    async with isolated(connection, history) as env:
        await env.play(plan)
        assert await env.open_differences() == []
        after = await get_connection(env.session, env.connection_id)
        assert [w for w in after.warnings if _CHANGED in w] == []


async def _expected_differences(env: Env, removed: Record) -> list[tuple[Any, ...]]:
    """A difference in each interval of two consecutive reports of each account the removed
    record changed, of what it changed there; none where Earn's allowance covers it, nor where
    the debt on Crypto Loans grew (a flexible loan's interest absorbs it)."""
    history = env.history
    reports = await env.reports()
    expected: list[tuple[Any, ...]] = []
    for wallet, asset in sorted({(e.wallet, e.asset) for e in removed.effects}):
        times = reports.get(wallet, [])
        for from_at, to_at in itertools.pairwise(times):
            low, high = _ms(from_at), _ms(to_at)
            delta = sum(
                (
                    e.delta
                    for e in removed.effects
                    if (e.wallet, e.asset) == (wallet, asset) and low < e.at_ms <= high
                ),
                Decimal(0),
            )
            if delta == 0:
                continue
            if wallet == "earn" and abs(delta) <= _accruing(history, removed, asset, high):
                continue
            if wallet == DEBTS and delta < 0:
                # A debt that grew with no record is a flexible loan's interest (design 5.7).
                continue
            zero = Decimal(0)
            change = history.balance(high).get((wallet, asset), zero) - history.balance(low).get(
                (wallet, asset), zero
            )
            expected.append(
                (
                    ACCOUNT_NAMES[wallet],
                    asset,
                    from_at,
                    to_at,
                    change.normalize(),
                    (change - delta).normalize(),
                )
            )
    return sorted(expected, key=lambda d: (d[2], d[0], d[1]))


def _accruing(history: History, removed: Record, asset: str, at_ms: int) -> Decimal:
    """The largest REALTIME reward row of the asset the fake lists in the 48 hours before."""
    largest = Decimal(0)
    for record in history.records:
        if record is removed or record.kind != "reward":
            continue
        [effect] = record.effects
        copies = len(record.rows)
        if effect.asset == asset and at_ms - _ACCRUING_MS < effect.at_ms <= at_ms:
            largest = max(largest, effect.delta / copies)
    return largest


async def _remove_one(
    connection: AsyncConnection, data: st.DataObject, kinds: frozenset[str]
) -> tuple[Record, list[tuple[Any, ...]], list[tuple[Any, ...]], dict[str, list[datetime]]]:
    history = data.draw(histories(), label="history")
    candidates = [r for r in history.records if r.kind in kinds and r.net() and not r.failing]
    if not candidates:
        candidates = [r for r in history.records if r.net() and not r.failing]
    removed = data.draw(st.sampled_from(candidates), label="removed") if candidates else None
    if removed is None:
        return _nothing(history)
    plan = data.draw(schedule(history, avoid=removed.moments()), label="schedule")
    _notes(history, plan)
    note(f"removed: {removed.describe()}")
    async with isolated(connection, history, hidden=[removed]) as env:
        await env.play(plan)
        # The last sync is after everything: every wallet's balance is its last report, which
        # holds the removed record's effect although no record posted it.
        await env.assert_balances_are_the_truth()
        found = await env.open_differences()
        expected = await _expected_differences(env, removed)
        return removed, found, expected, await env.reports()


def _nothing(history: History) -> Any:
    note(history.describe())
    event("nothing to remove")
    return None, [], [], {}


@EXAMPLES
@given(data=st.data())
async def test_removing_one_record_gives_exactly_one_difference(
    connection: AsyncConnection, data: st.DataObject
) -> None:
    removed, found, expected, reports = await _remove_one(connection, data, SINGLE_SIDED)
    assert found == expected
    if removed is None or removed.kind not in SINGLE_SIDED:
        return
    [effect] = removed.effects
    times = reports.get(effect.wallet, [])
    reported_before = bool(times) and _ms(times[0]) < effect.at_ms
    event(f"after its account's first report: {reported_before}")
    if not reported_before:
        # Before the account's first report: the opening holds it.
        assert found == []
    elif effect.wallet == "earn":
        # A reward no larger than the last two days' others is Earn's accruing allowance.
        assert len(found) <= 1
    else:
        [difference] = found
        assert difference[:2] == (ACCOUNT_NAMES[effect.wallet], effect.asset)
        assert difference[4] - difference[5] == effect.delta


@EXAMPLES
@given(data=st.data())
async def test_removing_a_two_sided_record_gives_one_difference_per_leg(
    connection: AsyncConnection, data: st.DataObject
) -> None:
    removed, found, expected, reports = await _remove_one(connection, data, TWO_SIDED)
    assert found == expected
    if removed is None or removed.kind not in TWO_SIDED:
        return
    legs = removed.net()
    reported = all(reports.get(w) and _ms(reports[w][0]) < removed.listed_ms for w, _ in legs)
    event(f"every leg after its account's first report: {reported}")
    if reported:
        got = {(d[0], d[1]): d[4] - d[5] for d in found}
        wanted = {
            (ACCOUNT_NAMES[w], a): q
            for (w, a), q in legs.items()
            if not (w == "earn" and 0 < q) and not (w == DEBTS and q < 0)
        }
        for holding, quantity in wanted.items():
            assert got.get(holding) == quantity, holding


@EXAMPLES
@given(history=histories(), data=st.data())
async def test_regenerate_is_identical(
    connection: AsyncConnection, history: History, data: st.DataObject
) -> None:
    plan = data.draw(schedule(history), label="schedule")
    _notes(history, plan)
    async with isolated(connection, history) as env:
        await env.play(plan)
        before = (await env.ledger(), await env.typed())
        await env.delete_derived()
        ctx = await env.context()
        await regenerate(ctx)
        await sync_module.opening(ctx)
        after = (await env.ledger(), await env.typed())
        assert after[0] == before[0]
        assert after[1] == before[1]


@EXAMPLES
@given(history=histories(min_records=2), data=st.data())
async def test_opening_plus_records_equal_the_first_report(
    connection: AsyncConnection, history: History, data: st.DataObject
) -> None:
    k = data.draw(st.integers(1, len(history.records)), label="hidden")
    hidden = history.records[:k]
    avoid = sorted({at for record in hidden for at in record.moments()})
    plan = data.draw(schedule(history, avoid=avoid), label="schedule")
    _notes(history, plan)
    note(f"hidden: the first {k}")
    async with isolated(connection, history, hidden=hidden) as env:
        await env.play(plan)
        reports = await env.reports()
        expected: dict[tuple[str, str], Decimal] = {}
        for wallet, times in reports.items():
            if not times:
                continue
            earliest = _ms(times[0])
            for asset in ASSETS:
                quantity = history.initial.get((wallet, asset), Decimal(0))
                quantity += sum(
                    (
                        e.delta
                        for record in hidden
                        for e in record.effects
                        if (e.wallet, e.asset) == (wallet, asset) and e.at_ms <= earliest
                    ),
                    Decimal(0),
                )
                if quantity != 0:
                    expected[(wallet, asset)] = quantity
        event(f"openings: {min(len(expected), 3)}")
        assert await env.openings() == expected
