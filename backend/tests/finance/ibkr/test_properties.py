"""Property tests pinning the IBKR normalizer's invariants on generated account histories
(strategies.py), fetched as overlapping windows in random orders, each run ingested and
normalized as a sync does.

- Windows and order: the ledger's transactions and postings are the same for any windowing
  and any order of fetching it.
- Balance: after every run, find_unbalanced_transactions finds nothing.
- Reconciliation: consistent statements leave no warning once every day is fetched, and a
  run reports a difference only on a day before which some day is not fetched yet.
- Idempotence: normalizing every raw record again writes nothing and emits no event.
- Regeneration: with the connection's ledger rows, reported snapshots, ibkr_* rows and IBKR
  prices and rates deleted, normalizing every raw record as a first sync rebuilds them
  identically.
- Fail closed: proceeds further from quantity times tradePrice than rounding explains fail
  the run.

Balance and reconciliation are checked after every run of every test. Rows are compared by
external id and content, never by UUID. Each example runs in a transaction of its own on a
connection of its own, rolled back at the end. Profiles (tests/finance/conftest.py): "ci"
(the default, derandomized) and "deep" (HYPOTHESIS_PROFILE=deep); each test here runs half
the profile's examples (HALF)."""

import uuid
from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path
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
from lararium.core.connections import create_connection, get_connection
from lararium.core.connectors import SyncContext
from lararium.core.documents import LocalDocumentStore
from lararium.core.json_types import JSONValue
from lararium.db import create_engine
from lararium.modules.finance.ibkr.normalize import NormalizeReport, normalize
from lararium.modules.finance.ibkr.parser import FlexDataError
from lararium.modules.finance.ibkr.sync import ingest_statement
from lararium.modules.finance.ledger import find_unbalanced_transactions

from .strategies import CENT, TICK, History, Window, fetches, histories, one_trade

CONNECTOR = Actor("connector", "ibkr")
USER = Actor("user", "test")
# Columns that differ between two runs of the same input without saying anything about it.
VOLATILE = frozenset({"id", "created_at", "updated_at", "recorded_at", "fetched_at"})


@pytest.fixture
async def connection(backend_url: str, migrated: None) -> AsyncIterator[AsyncConnection]:
    engine = create_engine(backend_url)
    try:
        async with engine.connect() as conn:
            yield conn
    finally:
        await engine.dispose()


class _Notifier:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send(self, text: str) -> None:
        self.sent.append(text)


def _refuse(request: httpx.Request) -> httpx.Response:
    raise AssertionError("no HTTP in these tests")


@dataclass
class Run:
    windows: list[Window]
    report: NormalizeReport
    events: int


class Connector:
    """One IBKR connection fed a history's statements, in one session."""

    def __init__(
        self, session: AsyncSession, http: httpx.AsyncClient, history: History, documents: Path
    ) -> None:
        self.session = session
        self.http = http
        self.history = history
        self.documents = LocalDocumentStore(documents)
        self.notifier = _Notifier()
        self.fetched: set[int] = set()  # the days of every window ingested so far
        self.sections: set[tuple[str, str]] = set()
        self.flagged: set[str] = set()
        self.gaps = False  # whether a run came before days it needed

    async def setup(self) -> None:
        async with command_context(self.session, USER, "test", {}) as ctx:
            record = await create_connection(ctx, connector="ibkr", display_name="IBKR", config={})
        self.connection_id = record.id

    async def context(self) -> SyncContext:
        return SyncContext(
            command=CommandContext(self.session, CONNECTOR, uuid.uuid4()),
            connection=await get_connection(self.session, self.connection_id),
            sync_id=uuid.uuid4(),
            master_key=bytes(32),
            http=self.http,
            notifier=self.notifier,
            session_factory=async_sessionmaker(bind=self.session.bind),
            documents=self.documents,
        )

    async def fetch(self, windows: list[Window]) -> Run:
        """Ingest the windows' statements and normalize them in one run, as a sync does;
        the earliest window it holds sets the opening (design 5.7)."""
        ids: list[UUID] = []
        flagged: set[str] = set()
        sections: set[tuple[str, str]] = set()
        for window in windows:
            request: dict[str, JSONValue] = {
                "query_id": "1000001",
                "fd": self.history.date_of(window.first).isoformat(),
                "td": self.history.date_of(window.last).isoformat(),
            }
            ingested = await ingest_statement(
                await self.context(), self.history.statement(window), request
            )
            ids += ingested.raw_record_ids
            flagged |= ingested.flagged
            sections |= ingested.sections
            self.fetched.update(window.days())
        ctx = await self.context()
        report = await normalize(ctx, ids, frozenset(flagged), frozenset(sections))
        events = len(ctx.command._events)  # pyright: ignore[reportPrivateUsage]
        self.sections |= sections
        self.flagged |= flagged
        return Run(windows, report, events)

    def complete_through(self, day: int) -> bool:
        """Whether every day from the first through `day` has been fetched."""
        return all(d in self.fetched for d in range(day + 1))

    async def raw_record_ids(self) -> list[UUID]:
        rows = await self.session.scalars(
            text(
                "select id from core.raw_records where connection_id = :c order by fetched_at, id"
            ),
            {"c": self.connection_id},
        )
        return list(rows)

    async def renormalize(self) -> Run:
        """Every raw record the connection holds, normalized again in one run."""
        ctx = await self.context()
        report = await normalize(
            ctx,
            await self.raw_record_ids(),
            frozenset(self.flagged),
            frozenset(self.sections),
        )
        return Run([], report, len(ctx.command._events))  # pyright: ignore[reportPrivateUsage]

    async def warnings(self) -> list[str]:
        return (await get_connection(self.session, self.connection_id)).warnings

    # What is compared ---------------------------------------------------------------------

    async def _labels(self) -> dict[UUID, str]:
        """A stable name for every row a UUID may point at."""
        labels: dict[UUID, str] = {}
        queries = {
            "account": "select id, name from finance.accounts",
            "instrument": (
                "select i.id, i.symbol || coalesce(':' || q.symbol, '')"
                " from finance.instruments i"
                " left join finance.instruments q on q.id = i.quote_instrument_id"
            ),
            "identifier": "select id, kind || ':' || value from finance.account_identifiers",
        }
        for kind, query in queries.items():
            for row in await self.session.execute(text(query)):
                labels[row[0]] = f"{kind} {row[1]}"
        for row in await self.session.execute(
            text("select id, external_id from finance.transactions where connection_id = :c"),
            {"c": self.connection_id},
        ):
            labels[row[0]] = f"transaction {self._external_id(row[1], labels)}"
        for row in await self.session.execute(
            text("select id, account_id, instrument_id, as_of from finance.balance_snapshots")
        ):
            labels[row[0]] = f"snapshot {labels[row[1]]} {labels[row[2]]} {row[3].isoformat()}"
        return labels

    @staticmethod
    def _external_id(external_id: str, labels: dict[UUID, str]) -> str:
        """An opening's key names its instrument by UUID: by its label instead."""
        if external_id.startswith("opening:"):
            prefix, _, instrument = external_id.rpartition(":")
            return f"{prefix}:{labels[UUID(instrument)]}"
        return external_id

    async def ledger(self) -> dict[str, tuple[Any, ...]]:
        """The connection's transactions and their postings, by external id."""
        labels = await self._labels()
        transactions = await self.session.execute(
            text(
                "select id, external_id, status, occurred_at, occurred_tz, booking_date,"
                " value_date, description, bank_transfer, counterparty_identifier_id"
                " from finance.transactions where connection_id = :c"
                # An opening a later run voided (history reached further back) holds nothing.
                " and not (external_id like 'opening:%' and status = 'void')"
            ),
            {"c": self.connection_id},
        )
        result: dict[str, tuple[Any, ...]] = {}
        for t in transactions:
            postings = await self.session.execute(
                text(
                    "select position, account_id, instrument_id, quantity, cost_per_unit,"
                    " cost_instrument_id, price_per_unit, price_instrument_id, is_fee, memo"
                    " from finance.postings where transaction_id = :t order by position"
                ),
                {"t": t.id},
            )
            key = self._external_id(t.external_id, labels)
            result[key] = (
                tuple(_label(v, labels) for v in tuple(t)[2:]),
                tuple(tuple(_label(v, labels) for v in p) for p in postings),
            )
        return result

    async def everything(self) -> dict[str, Any]:
        """The ledger, reported snapshots, IBKR's prices and rates, and every ibkr_* table."""
        labels = await self._labels()
        tables = await self.session.scalars(
            text(
                "select table_name from information_schema.tables"
                " where table_schema = 'finance' and table_name like 'ibkr\\_%'"
                " order by table_name"
            )
        )
        queries = {name: f"select * from finance.{name}" for name in tables}
        queries |= {
            "balance_snapshots": "select * from finance.balance_snapshots",
            "prices": "select * from finance.prices where source = 'ibkr'",
            "fx_rates": "select * from finance.fx_rates where source = 'ibkr'",
        }
        state: dict[str, Any] = {"ledger": await self.ledger()}
        for name, query in queries.items():
            rows = await self.session.execute(text(query))
            state[name] = sorted(
                repr(
                    sorted(
                        (column, _label(value, labels))
                        for column, value in row._mapping.items()  # pyright: ignore[reportPrivateUsage]
                        if column not in VOLATILE
                    )
                )
                for row in rows
            )
        return state

    async def delete_derived(self) -> None:
        """Everything normalize writes for the connection, leaving its raw records and
        accounts; and the cursor as before the first sync."""
        c = {"c": self.connection_id}
        accounts = "select id from finance.accounts where source_connection_id = :c"
        await self.session.execute(
            text("delete from finance.transactions where connection_id = :c"), c
        )
        await self.session.execute(
            text(f"delete from finance.balance_snapshots where account_id in ({accounts})"), c
        )
        for name in (
            "ibkr_position_lots",
            "ibkr_positions",
            "ibkr_cash_reports",
            "ibkr_nav",
            "ibkr_trade_details",
            "ibkr_instruments",
            "ibkr_accounts",
        ):
            await self.session.execute(text(f"delete from finance.{name}"))
        await self.session.execute(text("delete from finance.prices where source = 'ibkr'"))
        await self.session.execute(text("delete from finance.fx_rates where source = 'ibkr'"))


def _label(value: Any, labels: dict[UUID, str]) -> Any:
    if isinstance(value, UUID):
        return labels.get(value, "?unknown")
    if isinstance(value, Decimal):
        return value.normalize()
    return value


@asynccontextmanager
async def isolated(
    conn: AsyncConnection, history: History, documents: Path
) -> AsyncGenerator[Connector]:
    outer = await conn.begin()
    session = AsyncSession(bind=conn, join_transaction_mode="create_savepoint")
    try:
        async with httpx.AsyncClient(transport=httpx.MockTransport(_refuse)) as http:
            connector = Connector(session, http, history, documents)
            await connector.setup()
            yield connector
    finally:
        await session.close()
        await outer.rollback()


def _describe(runs: list[list[Window]]) -> str:
    return " | ".join(
        ", ".join(f"{w.first}-{w.last}{'' if w.by_day else ' whole'}" for w in run) for run in runs
    )


async def _fetch_all(connector: Connector, runs: list[list[Window]]) -> None:
    """Every run in turn, checking after each what holds after every run: the ledger
    balances, nothing is flagged, and a difference is reported only on a day some day
    before it is not fetched yet (a run's own days, or the latest day fetched, which a run
    of older windows checks again); and at the end, no warning."""
    for windows in runs:
        run = await connector.fetch(windows)
        assert await find_unbalanced_transactions(connector.session) == []
        assert not run.report.flagged
        for mismatch in run.report.mismatches:
            day = date.fromisoformat(mismatch.split(" on ")[1].split(" ")[0])
            assert not connector.complete_through((day - connector.history.start).days)
        if not connector.complete_through(max(connector.fetched)):
            connector.gaps = True
    assert await connector.warnings() == []


async def _replay(
    conn: AsyncConnection, history: History, runs: list[list[Window]], documents: Path
) -> dict[str, tuple[Any, ...]]:
    async with isolated(conn, history, documents) as connector:
        await _fetch_all(connector, runs)
        return await connector.ledger()


# Every pass checks balance and reconciliation (_fetch_all), so each property is checked on
# every example of every test; each test runs half the profile's examples, which keeps CI's
# time near the transfer engine's property tests.
HALF = settings(max_examples=max(1, settings().max_examples // 2))


@HALF
@given(history=histories(), data=st.data())
async def test_ledger_independent_of_windows_and_order(
    connection: AsyncConnection, tmp_path: Path, history: History, data: st.DataObject
) -> None:
    note(history.describe())
    first = data.draw(fetches(history.days), label="first fetches")
    second = data.draw(fetches(history.days), label="second fetches")
    note(f"first: {_describe(first)}\nsecond: {_describe(second)}")
    event(f"opening: {history.has_opening}")
    one = await _replay(connection, history, first, tmp_path)
    two = await _replay(connection, history, second, tmp_path)
    assert one.keys() == two.keys()
    assert one == two
    # Every event of the history is in the ledger.
    assert len([k for k in one if not k.startswith("opening:")]) == len(history.events)
    memos = {posting[-1] for _, postings in one.values() for posting in postings}
    event(f"rounding posted: {'rounding' in memos}")
    event(f"split: {any(k.startswith('ca:') for k in one)}")
    event(f"whole-window statement: {any(not w.by_day for run in first for w in run)}")


@HALF
@given(history=histories(), data=st.data())
async def test_every_transaction_balances(
    connection: AsyncConnection, tmp_path: Path, history: History, data: st.DataObject
) -> None:
    note(history.describe())
    runs = data.draw(fetches(history.days), label="fetches")
    note(_describe(runs))
    async with isolated(connection, history, tmp_path) as connector:
        await _fetch_all(connector, runs)  # checks it after every run
        assert await find_unbalanced_transactions(connector.session) == []


@HALF
@given(history=histories(), data=st.data())
async def test_no_reconciliation_difference(
    connection: AsyncConnection, tmp_path: Path, history: History, data: st.DataObject
) -> None:
    note(history.describe())
    runs = data.draw(fetches(history.days), label="fetches")
    note(_describe(runs))
    event(f"runs: {min(len(runs), 4)}")
    async with isolated(connection, history, tmp_path) as connector:
        await _fetch_all(connector, runs)  # checks it after every run
        assert await connector.warnings() == []
        # The owner is told of a difference only when a run came before days it needed.
        if not connector.gaps:
            assert connector.notifier.sent == []


@HALF
@given(history=histories(), data=st.data())
async def test_renormalize_writes_nothing(
    connection: AsyncConnection, tmp_path: Path, history: History, data: st.DataObject
) -> None:
    note(history.describe())
    runs = data.draw(fetches(history.days), label="fetches")
    note(_describe(runs))
    async with isolated(connection, history, tmp_path) as connector:
        await _fetch_all(connector, runs)
        before = await connector.everything()
        again = await connector.renormalize()
        report = again.report
        assert (report.created, report.updated) == (0, 0)
        openings = await connector.session.scalar(
            text(
                "select count(*) from finance.transactions"
                " where external_id like 'opening:%' and status <> 'void'"
            )
        )
        assert report.unchanged == len(history.events) + openings
        assert (report.accounts_created, report.accounts_updated) == (0, 0)
        assert again.events == 0
        assert await connector.everything() == before


@HALF
@given(history=histories(), data=st.data())
async def test_regenerate_from_raw(
    connection: AsyncConnection, tmp_path: Path, history: History, data: st.DataObject
) -> None:
    note(history.describe())
    runs = data.draw(fetches(history.days), label="fetches")
    note(_describe(runs))
    event(f"opening: {history.has_opening}")
    async with isolated(connection, history, tmp_path) as connector:
        await _fetch_all(connector, runs)
        before = await connector.everything()
        await connector.delete_derived()
        await connector.renormalize()
        after = await connector.everything()
        for name in before:
            assert after[name] == before[name], name


@HALF
@given(
    quantity=st.integers(1, 2_000).map(lambda n: Decimal(n) / 10),
    price=st.integers(10_000, 2_000_000).map(lambda n: (Decimal(n) / 10_000).quantize(TICK)),
    beyond=st.integers(2, 100_000).map(lambda n: Decimal(n) / 100),
    sign=st.sampled_from((1, -1)),
)
async def test_proceeds_beyond_rounding_fail_closed(
    connection: AsyncConnection,
    tmp_path: Path,
    quantity: Decimal,
    price: Decimal,
    beyond: Decimal,
    sign: int,
) -> None:
    """Proceeds further from quantity times tradePrice than rounding explains (half a unit
    of tradePrice's last place per share, plus half a cent) fail the run, whatever else
    the statement holds."""
    bound = quantity * Decimal("0.00005") + Decimal("0.005")
    proceeds = -(quantity * price).quantize(CENT) + sign * (bound + beyond)
    history = one_trade(quantity, price, proceeds.quantize(CENT))
    note(f"{quantity} at {price}, proceeds {proceeds}")
    async with isolated(connection, history, tmp_path) as connector:
        with pytest.raises(FlexDataError, match="by more than rounding"):
            await connector.fetch([Window(0, 0)])
