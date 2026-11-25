"""Property tests pinning the Trading 212 connector's invariants on generated account
histories (strategies.py), each reaching Lararium through drawn schedules: syncs at cuts of
the history, every history endpoint walked newest first in drawn page splits whose pages
may repeat the last rows of the one before, through the real sync (Trading212Connector,
against test_sync's FakeTrading212) and normalizer; and optionally CSV exports of the
deposits and withdrawals, uploaded between syncs (export.ingest_export).

- Order: the ledger (every transaction and its postings, by external id) is the same for
  any two schedules, and the same with no export at all: the export stays optional, and
  where it overlaps the API it changes no posting.
- Balance: after every sync and upload, find_unbalanced_transactions finds nothing.
- Reconciliation: every sync's report matches the ledger (the API side is complete up to
  each cut, so there is never a difference), no warning or flag is ever published, the
  owner is told nothing, and the history leaves no opening adjustment.
- Idempotence: normalizing every raw record again writes nothing and emits no event.
- Regeneration: with everything normalize derives deleted, a replay (a new normalizer
  version) rebuilds it identically; also after an export that disagrees with the API on a
  deposit's amount and the owner's taking the export's value (source_reviews).

Each example runs in a transaction of its own on a connection of its own, rolled back at the
end. Profiles: tests/finance/conftest.py; each test runs half the profile's examples."""

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, tzinfo
from decimal import Decimal
from functools import partial
from itertools import pairwise
from typing import Any

import httpx
import pytest
from hypothesis import Phase, event, given, note, settings
from hypothesis import strategies as st
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession, async_sessionmaker

from lararium.core.actors import Actor
from lararium.core.commands import CommandContext, command_context
from lararium.core.connections import get_connection, update_config
from lararium.core.connectors import SyncContext
from lararium.db import create_engine
from lararium.modules.finance.ledger import find_unbalanced_transactions
from lararium.modules.finance.source_reviews import list_reviews, resolve_review
from lararium.modules.finance.trading212 import sync as t212_sync
from lararium.modules.finance.trading212.client import Trading212Client
from lararium.modules.finance.trading212.export import ingest_export
from lararium.modules.finance.trading212.normalize import normalize
from lararium.modules.finance.trading212.sync import Trading212Connector

from .fixtures import instrument_json
from .strategies import START, Event, Export, History, Schedule, histories, schedules
from .test_export import _delete_derived, export_csv, row
from .test_normalize import _ledger_state, _reports
from .test_sync import KEY, FakeTrading212, _set_cursor
from .test_sync import _connection as _sync_connection

USER = Actor("user", "test")
CONNECTOR = Actor("connector", "trading212")
# Half the profile's examples per test: each runs several syncs on Postgres. No explain
# phase: on a failure it would replay hundreds of variants of the example on Postgres.
HALF = settings(
    max_examples=max(1, settings().max_examples // 2),
    phases=[p for p in Phase if p is not Phase.explain],
)


@pytest.fixture
async def connection(backend_url: str, migrated: None) -> AsyncIterator[AsyncConnection]:
    engine = create_engine(backend_url)
    try:
        async with engine.connect() as conn:
            yield conn
    finally:
        await engine.dispose()


class _Clock:
    """The sync's wall clock (when each report arrived, and the re-walk's and metadata's
    due dates): a history's events must happen between the syncs that see them, so each
    sync runs at its cut's time (History.sync_time), not at the test's."""

    now = START


class _SyncTime(datetime):
    @classmethod
    def now(cls, tz: tzinfo | None = None) -> "_SyncTime":  # type: ignore[override]
        at = _Clock.now
        return cls.fromtimestamp(at.timestamp(), tz or UTC)


@pytest.fixture
def api(monkeypatch: pytest.MonkeyPatch) -> FakeTrading212:
    """The fake API, its pacing on a fake clock, and the sync's clock set by each sync;
    each sync sets what the API serves."""
    fake = FakeTrading212()
    monkeypatch.setattr(
        t212_sync,
        "Trading212Client",
        partial(Trading212Client, sleep=fake.sleep, clock=fake.clock, wall_clock=fake.clock),
    )
    monkeypatch.setattr(t212_sync, "datetime", _SyncTime)
    return fake


class _Notifier:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send(self, text: str) -> None:
        self.sent.append(text)


def _refuse(request: httpx.Request) -> httpx.Response:
    raise AssertionError("no HTTP outside a sync")


def _export_rows(history: History, export: Export, disagree: bool = False) -> bytes:
    """The export's rows: the window's deposits and withdrawals, as the audited columns
    print them (a deposit's card Transaction ID in Notes). `disagree`: its first row's
    Total one pound more than the API's amount."""
    rows: list[dict[str, str]] = []
    for e in history.events:
        if not (export.first <= e.n <= export.last) or e.kind not in ("deposit", "withdrawal"):
            continue
        total = e.cash + (Decimal(1) if disagree and not rows else Decimal(0))
        rows.append(
            row(
                reference=e.reference,
                action="Deposit" if e.kind == "deposit" else "Withdrawal",
                total=str(total),
                time=e.at.strftime("%Y-%m-%d %H:%M:%S+00:00"),
                notes=f"Transaction ID: card-{e.n:04d}" if e.kind == "deposit" else "",
            )
        )
    return export_csv(*rows)


class Account:
    """One Trading 212 connection fed a history by a schedule, in one session."""

    def __init__(self, session: AsyncSession, api: FakeTrading212, history: History) -> None:
        self.session = session
        self.api = api
        self.history = history
        self.notifier = _Notifier()
        self.uploads = 0

    async def run(self, fn: Any, /, *args: Any, **kwargs: Any) -> Any:
        async with command_context(self.session, USER, "test", {}) as ctx:
            return await fn(ctx, *args, **kwargs)

    async def setup(self) -> None:
        self.connection = await _sync_connection(self.run)

    async def context(self, http: httpx.AsyncClient) -> SyncContext:
        return SyncContext(
            command=CommandContext(self.session, CONNECTOR, uuid.uuid4()),
            connection=await get_connection(self.session, self.connection.id),
            sync_id=uuid.uuid4(),
            master_key=KEY,
            http=http,
            notifier=self.notifier,
            session_factory=async_sessionmaker(bind=self.session.bind),
        )

    def _serve(self, cut: int, pages: dict[str, list[tuple[int, int]]]) -> None:
        api, history = self.api, self.history
        api.summary = history.summary(cut)
        api.positions = history.positions(cut)
        api.pending = []
        api.instruments = [instrument_json()]
        api.pies = []
        api.requests = []
        for endpoint, splits in pages.items():
            rows = [e.item() for e in history.rows(endpoint, cut)]
            api.history[endpoint] = [rows[start : start + size] for start, size in splits]

    async def sync(self, cut: int, pages: dict[str, list[tuple[int, int]]]) -> None:
        """One sync of the history up to the cut, as the worker runs it, at the cut's time."""
        self._serve(cut, pages)
        _Clock.now = self.history.sync_time(cut)
        async with httpx.AsyncClient(transport=httpx.MockTransport(self.api.handler)) as http:
            cursor = await Trading212Connector().sync(await self.context(http))
        await _set_cursor(self.session, self.connection, cursor)

    async def upload(self, body: bytes) -> None:
        self.uploads += 1
        async with httpx.AsyncClient(transport=httpx.MockTransport(_refuse)) as http:
            await ingest_export(await self.context(http), body, f"export-{self.uploads}.csv")

    async def warnings(self) -> list[str]:
        return (await get_connection(self.session, self.connection.id)).warnings

    async def checked(self) -> None:
        """What holds after every sync and upload: the ledger balances, and nothing is
        warned of, flagged or told."""
        assert await find_unbalanced_transactions(self.session) == []
        assert await self.warnings() == []
        assert self.notifier.sent == []

    async def follow(self, schedule: Schedule) -> None:
        for n, sync in enumerate(schedule.syncs, start=1):
            await self.sync(sync.cut, sync.pages)
            await self.checked()
            for export in schedule.exports:
                if export.after == n:
                    await self.upload(_export_rows(self.history, export))
                    await self.checked()

    async def raw_record_ids(self) -> list[uuid.UUID]:
        rows = await self.session.scalars(
            text(
                "select id from core.raw_records where connection_id = :c order by fetched_at, id"
            ),
            {"c": self.connection.id},
        )
        return list(rows)

    async def own_quantity(self, external_id: str) -> Decimal:
        """The live transaction's posting on the connection's own account."""
        quantity = await self.session.scalar(
            text(
                "select p.quantity from finance.postings p"
                " join finance.transactions t on t.id = p.transaction_id"
                " join finance.accounts a on a.id = p.account_id"
                " where t.connection_id = :c and t.external_id = :e and t.status <> 'void'"
                " and a.source_connection_id = :c"
            ),
            {"c": self.connection.id, "e": external_id},
        )
        assert isinstance(quantity, Decimal)
        return quantity

    async def ledger(self) -> list[tuple[Any, ...]]:
        """Every transaction of the connection and its postings, by external id, without
        UUIDs."""
        return (await _ledger_state(self.session, self.connection))["transactions"]

    async def everything(self) -> dict[str, Any]:
        """The ledger, typed rows, reports, prices, export rows and review items."""
        state = await _ledger_state(self.session, self.connection)
        state |= await _reports(self.session, self.connection)
        for table in ("trading212_export_rows", "trading212_order_taxes"):
            rows = await self.session.execute(
                text(
                    f"select to_jsonb(r) - 'transaction_id' as row, t.external_id"
                    f" from finance.{table} r"
                    " left join finance.transactions t"
                    " on t.id = (to_jsonb(r) ->> 'transaction_id')::uuid"
                    " where r.connection_id = :c"
                ),
                {"c": self.connection.id},
            )
            state[table] = sorted((str(sorted(r.row.items())), r.external_id) for r in rows)
        state["reviews"] = sorted(
            (r.event_key, r.field, r.api_value, r.statement_value, r.resolution)
            for r in await list_reviews(self.session, self.connection.id)
        )
        return state

    async def renormalize(self) -> tuple[Any, int]:
        """Every raw record normalized again in one run, as a sync of its own."""
        async with httpx.AsyncClient(transport=httpx.MockTransport(_refuse)) as http:
            ctx = await self.context(http)
            report = await normalize(ctx, await self.raw_record_ids())
        return report, len(ctx.command._events)  # pyright: ignore[reportPrivateUsage]

    async def replay(self) -> None:
        """A new normalizer version: every stored record replayed, nothing new fetched."""
        await self.run(
            update_config, self.connection.id, {"normalizer_version": "trading212-norm-0"}
        )
        async with httpx.AsyncClient(transport=httpx.MockTransport(_refuse)) as http:
            await normalize(await self.context(http), [])


@asynccontextmanager
async def isolated(
    conn: AsyncConnection, api: FakeTrading212, history: History
) -> AsyncIterator[Account]:
    outer = await conn.begin()
    session = AsyncSession(bind=conn, join_transaction_mode="create_savepoint")
    try:
        account = Account(session, api, history)
        await account.setup()
        yield account
    finally:
        await session.close()
        await outer.rollback()


async def _follow(
    conn: AsyncConnection, api: FakeTrading212, history: History, schedule: Schedule
) -> list[tuple[Any, ...]]:
    async with isolated(conn, api, history) as account:
        await account.follow(schedule)
        return await account.ledger()


def _expected_keys(history: History) -> set[str]:
    """Every event's key (Decision 9): all of them post, but a cancelled order, which is a
    typed row only."""
    found: set[str] = set()
    for e in history.events:
        if e.kind == "cancelled":
            continue
        if e.endpoint == "orders":
            found.add(f"order:{700000 + e.n}")
        else:
            found.add(f"{'dividend' if e.kind == 'dividend' else 'transaction'}:{e.reference}")
    return found


def _posted(ledger: list[tuple[Any, ...]]) -> set[str]:
    return {t[0] for t in ledger if t[1] != "void"}


def _events(history: History, schedule: Schedule) -> None:
    kinds = {e.kind for e in history.events}
    event(f"syncs: {len(schedule.syncs)}")
    event(f"exports: {len(schedule.exports)}")
    event(f"sold: {'sell' in kinds}")
    event(f"overlapping pages: {_overlaps(schedule)}")


def _overlaps(schedule: Schedule) -> bool:
    for sync in schedule.syncs:
        for splits in sync.pages.values():
            for (a, n), (b, _) in pairwise(splits):
                if b < a + n:
                    return True
    return False


@HALF
@given(history=histories(), data=st.data())
async def test_ledger_independent_of_order(
    connection: AsyncConnection, api: FakeTrading212, history: History, data: st.DataObject
) -> None:
    note(history.describe())
    first = data.draw(schedules(history), label="first schedule")
    second = data.draw(schedules(history), label="second schedule")
    plain = data.draw(schedules(history, exports=False), label="without an export")
    note(f"first:\n{first.describe()}\nsecond:\n{second.describe()}\nplain:\n{plain.describe()}")
    _events(history, first)
    one = await _follow(connection, api, history, first)
    assert await _follow(connection, api, history, second) == one
    assert await _follow(connection, api, history, plain) == one
    # Every event is in the ledger, but the cancelled orders, and no opening.
    assert {t[0] for t in one} == _posted(one) == _expected_keys(history)


@HALF
@given(history=histories(), data=st.data())
async def test_every_transaction_balances(
    connection: AsyncConnection, api: FakeTrading212, history: History, data: st.DataObject
) -> None:
    note(history.describe())
    schedule = data.draw(schedules(history), label="schedule")
    note(schedule.describe())
    _events(history, schedule)
    async with isolated(connection, api, history) as account:
        await account.follow(schedule)  # checks it after every sync and upload
        assert await find_unbalanced_transactions(account.session) == []


@HALF
@given(history=histories(), data=st.data())
async def test_no_reconciliation_difference(
    connection: AsyncConnection, api: FakeTrading212, history: History, data: st.DataObject
) -> None:
    note(history.describe())
    schedule = data.draw(schedules(history), label="schedule")
    note(schedule.describe())
    _events(history, schedule)
    async with isolated(connection, api, history) as account:
        await account.follow(schedule)  # no warning, flag or message after every step
        openings = await account.session.scalar(
            text(
                "select count(*) from finance.transactions"
                " where connection_id = :c and external_id like 'opening:%'"
            ),
            {"c": account.connection.id},
        )
        assert openings == 0
        cursor = (await get_connection(account.session, account.connection.id)).cursor
        assert isinstance(cursor, dict) and not any(cursor.get("flags", {}).values())


@HALF
@given(history=histories(), data=st.data())
async def test_renormalize_writes_nothing(
    connection: AsyncConnection, api: FakeTrading212, history: History, data: st.DataObject
) -> None:
    note(history.describe())
    schedule = data.draw(schedules(history), label="schedule")
    note(schedule.describe())
    _events(history, schedule)
    async with isolated(connection, api, history) as account:
        await account.follow(schedule)
        before = await account.everything()
        report, events = await account.renormalize()
        assert (report.created, report.updated) == (0, 0)
        assert report.unchanged == len(_posted(await account.ledger()))
        assert (report.accounts_created, report.accounts_updated) == (0, 0)
        assert not report.flagged
        assert events == 0
        assert await account.everything() == before


async def _disagreeing_export(account: Account, history: History) -> None:
    """Upload an export listing every deposit and withdrawal, its first a pound more than
    the API says; check that it opens one review, on that amount; and take the export's
    amount, which the next run posts."""
    movements = [e for e in history.events if e.kind in ("deposit", "withdrawal")]
    export = Export(len(history.events), 0, len(history.events) - 1)
    await account.upload(_export_rows(history, export, disagree=True))
    reviews = await list_reviews(account.session, account.connection.id)
    assert {(r.event_key, r.field) for r in reviews} == {
        (f"transaction:{movements[0].reference}", "amount")
    }
    for review in reviews:
        await account.run(resolve_review, review.id, "statement")
    await account.replay()  # the decision posts at the next run


@HALF
@given(history=histories(), data=st.data(), decided=st.booleans())
async def test_regenerate_from_raw(
    connection: AsyncConnection,
    api: FakeTrading212,
    history: History,
    data: st.DataObject,
    decided: bool,
) -> None:
    note(history.describe())
    schedule = data.draw(schedules(history), label="schedule")
    note(schedule.describe())
    _events(history, schedule)
    event(f"a resolved review: {decided}")
    async with isolated(connection, api, history) as account:
        await account.follow(schedule)
        if decided:
            await _disagreeing_export(account, history)
            first: Event = next(e for e in history.events if e.kind == "deposit")
            posted = await account.own_quantity(f"transaction:{first.reference}")
            assert posted == first.cash + 1  # the export's amount posts
        before = await account.everything()
        await _delete_derived(account.session, account.connection)
        await account.replay()
        after = await account.everything()
        for name in before:
            assert after[name] == before[name], name
