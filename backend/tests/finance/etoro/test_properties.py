"""Property tests pinning the eToro connector's invariants on generated histories
(strategies.py): what the API reports at each of a schedule's syncs, normalized as a sync
does (test_normalize_api's Ledger, which stores the records a sync fetched and sights its
reports at the sync's time), interleaved with statements uploaded as overlapping pieces in
a drawn order (sync.ingest_statement, as the upload route calls it).

- Order: the ledger (every live transaction and its postings, by external id) and the leg
  fees are the same for any two schedules: whichever source arrives first, the API's values
  post a trade (design §5.7), and a statement row two pieces share is one event.
- Balance: after every sync and upload, find_unbalanced_transactions finds nothing.
- Reconciliation: once the statements uploaded cover every event up to the latest sync,
  and at the end, no warning stands (no difference, no "upload a statement" prompt, no
  flag); no review item is ever opened (the sources agree on every trade); nothing is left
  to the owner. Time semantics are pinned on the way: statement times are read as UTC
  (events in the hour before midnight UTC in summer time book on the same London day from
  both sources), and a statement ending mid-day covers that day only up to its end (the
  next piece overlaps it).
- Idempotence: normalizing every raw record again writes nothing and emits no event.
- Regeneration: with everything normalize derives deleted, a replay (a new normalizer
  version) rebuilds it identically; also after a corrected statement disagreeing with the
  API on a close's cash and the owner's taking the statement's value (source_reviews), whose
  decision the replay applies again.

Each example runs in a transaction of its own on a connection of its own, rolled back at the
end. Profiles: tests/finance/conftest.py; each test runs a third of the profile's examples
(each example normalizes a dozen runs on Postgres), with no explain phase."""

import dataclasses
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from hypothesis import HealthCheck, Phase, event, given, note, settings
from hypothesis import strategies as st
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession

from lararium.core.actors import Actor
from lararium.core.commands import command_context
from lararium.core.connections import get_connection
from lararium.core.documents import LocalDocumentStore
from lararium.db import create_engine
from lararium.modules.finance.etoro import sync as etoro_sync
from lararium.modules.finance.etoro.normalize import normalize
from lararium.modules.finance.etoro.reconcile import PROMPT, prompt
from lararium.modules.finance.ledger import find_unbalanced_transactions
from lararium.modules.finance.source_reviews import list_reviews, resolve_review

from .strategies import DAY0, Event, History, Piece, Position, Schedule, histories, schedules
from .test_normalize_api import Ledger, _connection, _delete_derived, _ledger_state
from .test_normalize_statement import _ledger, _leg_fees, _review_links

USER = Actor("user", "test")
TRADING = "eToro Trading"
SUMMER_TIME_ENDS = date(2026, 10, 25)
# A history is a dozen events and a schedule of a few syncs and statements at the least,
# which is what a smallest example of eToro's is (large_base_example).
THIRD = settings(
    max_examples=max(1, settings().max_examples // 3),
    phases=[p for p in Phase if p is not Phase.explain],
    suppress_health_check=[*settings().suppress_health_check, HealthCheck.large_base_example],
)


@pytest.fixture
async def connection(backend_url: str, migrated: None) -> AsyncIterator[AsyncConnection]:
    engine = create_engine(backend_url)
    try:
        async with engine.connect() as conn:
            yield conn
    finally:
        await engine.dispose()


class Books(Ledger):
    """One eToro connection fed a history by a schedule, in one session."""

    def __init__(self, session: AsyncSession, connection: Any, history: History, root: Path):
        super().__init__(session, connection)
        self.history = history
        self.documents = LocalDocumentStore(root)
        self.synced: datetime | None = None
        self.flags_in_full: set[str] = set()
        self.prompted: set[bool] = set()  # prompts seen, by whether the latest ended mid-day
        self.uploaded: list[Piece] = []

    async def sync(self, at: datetime) -> None:
        report = await self.normalize(*self.history.api(at), now=at)
        assert not report.flagged, report.flagged
        # Found in full every run, so they clear once their cause is gone: a position opened
        # and closed between two syncs has no currency until a statement names it.
        self.flags_in_full = report.flags_in_full
        self.synced = at

    async def upload(self, piece: Piece, corrected: int | None = None) -> None:
        ctx = dataclasses.replace(await self.context(), documents=self.documents)
        body = self.history.statement(piece, corrected)
        ingested = await etoro_sync.ingest_statement(ctx, body, "statement.xlsx")
        assert ingested.flagged == frozenset()
        self.uploaded.append(piece)

    def covered(self) -> bool:
        """Whether the statements uploaded hold every event up to the latest sync."""
        assert self.synced is not None
        return all(
            any(datetime.combine(p.start, time(), UTC) <= e.at <= p.end for p in self.uploaded)
            for e in self.history.upto(self.synced)
        )

    async def warnings(self) -> list[str]:
        return (await get_connection(self.session, self.connection.id)).warnings

    async def reviews(self) -> list[Any]:
        return await list_reviews(self.session, self.connection.id)

    async def follow(self, schedule: Schedule) -> None:
        """Every step in turn, checking after each what holds after every step."""
        for kind, what in schedule.steps():
            if kind == "sync":
                await self.sync(what)
            else:
                await self.upload(what)
            assert await find_unbalanced_transactions(self.session) == []
            warnings = await self.warnings()
            if self.covered():
                assert warnings == [], kind
            await self.check_prompt(warnings)
            reviews = await self.reviews()
            assert not reviews, [
                (r.event_key, r.field, r.api_value, r.statement_value) for r in reviews
            ]
        assert self.covered()
        assert await self.warnings() == []
        if schedule.steps()[-1][0] == "sync":
            assert not self.flags_in_full, self.flags_in_full

    async def check_prompt(self, warnings: list[str]) -> None:
        """An "upload a statement" prompt, once a statement is in, names the first day the
        latest one does not cover in full: its end's day when it ends mid-day (the rest of
        that day is not covered), else the next day (design §5.11, ruling R34)."""
        prompts = [w for w in warnings if PROMPT in w]
        if not prompts or not self.uploaded:
            return
        latest = max(self.uploaded, key=lambda p: (p.end, p.start))
        mid_day = latest.end.time() != time(23, 59, 59)
        expected = latest.end.date() if mid_day else latest.end.date() + timedelta(days=1)
        assert prompts == [prompt(TRADING, expected)], (prompts, latest)
        self.prompted.add(mid_day)

    async def raw_record_ids(self) -> list[Any]:
        rows = await self.session.scalars(
            text(
                "select id from core.raw_records where connection_id = :c order by fetched_at, id"
            ),
            {"c": self.connection.id},
        )
        return list(rows)

    async def ledger(self) -> dict[str, Any]:
        return {"ledger": await _ledger(self.session, self), "fees": await _leg_fees(self.session)}

    async def everything(self) -> dict[str, Any]:
        state = await _ledger_state(self.session, self.connection)
        state["fees"] = await _leg_fees(self.session)
        state["reviews"] = await _review_links(self.session, self)
        return state


@asynccontextmanager
async def isolated(conn: AsyncConnection, history: History, root: Path) -> AsyncIterator[Books]:
    outer = await conn.begin()
    session = AsyncSession(bind=conn, join_transaction_mode="create_savepoint")

    async def run(fn: Any, /, *args: Any, **kwargs: Any) -> Any:
        async with command_context(session, USER, "test", {}) as ctx:
            return await fn(ctx, *args, **kwargs)

    try:
        books = Books(session, await _connection(run), history, root)
        books.run = run  # type: ignore[attr-defined]
        yield books
    finally:
        await session.close()
        await outer.rollback()


async def _follow(
    conn: AsyncConnection, history: History, schedule: Schedule, root: Path
) -> dict[str, Any]:
    async with isolated(conn, history, root) as books:
        await books.follow(schedule)
        return await books.ledger()


def _events(history: History, schedule: Schedule) -> None:
    kinds = {e.kind for e in history.events}
    event(f"partial close: {'partial' in kinds}")
    event(f"withdraw cancelled: {'cancel' in kinds}")
    event(f"a fee row: {any(e.fee is not None for e in history.events)}")
    event(f"statements: {min(len(schedule.uploads), 4)}")
    mid_day = any(p.end.time() != time(23, 59, 59) for p, _ in schedule.uploads)
    event(f"a piece ending mid-day: {mid_day}")
    late = any(e.at.hour == 23 and e.at.date() < SUMMER_TIME_ENDS for e in history.events)
    event(f"an event after 23:00 UTC in summer time: {late}")
    first_upload = next(i for i, step in enumerate(schedule.steps()) if step[0] == "upload")
    event(f"a statement before the last sync: {first_upload < len(schedule.steps()) - 1}")


def _expected_keys(history: History) -> set[str]:
    found: set[str] = set()
    for e in history.events:
        if e.position is not None and e.kind == "open":
            found.add(f"open:{e.position.position_id}")
        elif e.position is not None and e.kind in ("close", "partial"):
            found.add(f"close:{e.position.position_id}")
            if e.remainder is not None:
                found.add(f"open:{e.remainder.position_id}")
        if e.money:
            found.add(f"cash:8000{e.n:04d}")
    return found


@THIRD
@given(history=histories(), data=st.data())
async def test_ledger_independent_of_order(
    connection: AsyncConnection, tmp_path: Path, history: History, data: st.DataObject
) -> None:
    note(history.describe())
    first = data.draw(schedules(history), label="first")
    second = data.draw(schedules(history), label="second")
    note(f"first:\n{first.describe()}\nsecond:\n{second.describe()}")
    _events(history, first)
    one = await _follow(connection, history, first, tmp_path)
    two = await _follow(connection, history, second, tmp_path)
    assert one == two
    # Every trade and eToro Money row is posted; the rest are the statement's activity rows
    # and eToro Money's opening.
    keys = set(one["ledger"])
    assert _expected_keys(history) <= keys
    others = keys - _expected_keys(history)
    assert all(k.startswith(("activity:", "opening:")) for k in others), others


@THIRD
@given(history=histories(), data=st.data())
async def test_every_transaction_balances(
    connection: AsyncConnection, tmp_path: Path, history: History, data: st.DataObject
) -> None:
    note(history.describe())
    schedule = data.draw(schedules(history), label="schedule")
    note(schedule.describe())
    _events(history, schedule)
    async with isolated(connection, history, tmp_path) as books:
        await books.follow(schedule)  # checks it after every step
        assert await find_unbalanced_transactions(books.session) == []


@THIRD
@given(history=histories(), data=st.data())
async def test_no_reconciliation_difference(
    connection: AsyncConnection, tmp_path: Path, history: History, data: st.DataObject
) -> None:
    note(history.describe())
    schedule = data.draw(schedules(history), label="schedule")
    note(schedule.describe())
    _events(history, schedule)
    async with isolated(connection, history, tmp_path) as books:
        await books.follow(schedule)  # checks it after every step once covered, and at the end
        event(f"a prompt after a mid-day end: {True in books.prompted}")
        assert await books.reviews() == []
        assert not [m for m in books.notifier.messages if "disagree" in m]


@THIRD
@given(history=histories(), data=st.data())
async def test_renormalize_writes_nothing(
    connection: AsyncConnection, tmp_path: Path, history: History, data: st.DataObject
) -> None:
    note(history.describe())
    schedule = data.draw(schedules(history), label="schedule")
    note(schedule.describe())
    _events(history, schedule)
    async with isolated(connection, history, tmp_path) as books:
        await books.follow(schedule)
        before = await books.everything()
        ctx = await books.context()
        report = await normalize(ctx, await books.raw_record_ids())
        assert (report.created, report.updated) == (0, 0)
        assert (report.accounts_created, report.accounts_updated) == (0, 0)
        assert not report.flagged
        assert len(ctx.command._events) == 0  # pyright: ignore[reportPrivateUsage]
        assert await books.everything() == before
        assert await books.warnings() == []


@THIRD
@given(history=histories(), data=st.data(), decided=st.booleans())
async def test_regenerate_from_raw(
    connection: AsyncConnection,
    tmp_path: Path,
    history: History,
    data: st.DataObject,
    decided: bool,
) -> None:
    note(history.describe())
    schedule = data.draw(schedules(history), label="schedule")
    note(schedule.describe())
    _events(history, schedule)
    closes = [e for e in history.events if e.kind in ("close", "partial")]
    event(f"a resolved review: {decided and bool(closes)}")
    async with isolated(connection, history, tmp_path) as books:
        await books.follow(schedule)
        if decided and closes:
            # A re-issued statement of the whole history says the first close brought a
            # dollar more; the owner takes the statement's value, which the next run posts.
            corrected = closes[0]
            whole = Piece(DAY0, history.last + timedelta(seconds=1))
            await books.upload(whole, corrected.n)
            [review] = await books.reviews()
            assert corrected.position is not None
            assert (review.event_key, review.field) == (
                f"close:{corrected.position.position_id}",
                "cash",
            )
            await books.run(resolve_review, review.id, "statement")  # type: ignore[attr-defined]
            await books.normalize(*history.api(schedule.syncs[-1]), now=schedule.syncs[-1])
            [posted] = [
                p for p in (await books.ledger())["ledger"][review.event_key][5] if p[1] == "USD"
            ]
            assert posted[2] == corrected.cash + 1
        before = await books.everything()
        await _delete_derived(books.session, books.connection)
        await books.replay()
        after = await books.everything()
        for name in before:
            assert after[name] == before[name], name


def _at(day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 10, day, hour, minute, tzinfo=UTC)


def _fixed() -> tuple[History, Schedule, Schedule]:
    """A history every CI run checks, whatever Hypothesis draws: a partial close that a
    statement ends before while the API already has it (R39), and a position a statement
    opens before any sync has seen it (R40), opened late on a summer evening."""
    first = Position(3000000101, 3000000601, Decimal(2), Decimal("200.00"), _at(22, 11))
    rest = Position(
        3000000102, 3000000601, Decimal(1), Decimal("100.00"), _at(22, 11), first.position_id
    )
    late = Position(3000000103, 3000000603, Decimal(3), Decimal("300.00"), _at(24, 23, 30))
    history = History(
        (
            Event(0, "in", _at(22, 10), Decimal("1260.00"), Decimal("-1000.00"), Decimal("6.30")),
            Event(1, "open", _at(22, 11), Decimal("-200.00"), position=first),
            Event(
                2,
                "partial",
                _at(23, 12),
                Decimal("103.33"),
                position=first,
                units=Decimal(1),
                profit=Decimal("3.33"),
                remainder=rest,
            ),
            Event(3, "open", _at(24, 23, 30), Decimal("-300.00"), position=late),
            Event(4, "close", _at(26, 10), Decimal("100.00"), position=rest, units=Decimal(1)),
        )
    )
    behind = Piece(DAY0, datetime.combine(DAY0, time(23, 59, 59), UTC))
    opening = Piece(date(2026, 10, 23), _at(25, 1))
    rest_piece = Piece(date(2026, 10, 25), _at(27, 0))
    syncs = (_at(22, 10, 30), _at(23, 13), _at(27, 12))
    schedule = Schedule(syncs, ((behind, 2), (opening, 2), (rest_piece, 3)))
    later = Schedule((syncs[0], syncs[2]), ((behind, 2), (opening, 2), (rest_piece, 2)))
    return history, schedule, later


async def test_a_partial_close_and_a_statement_first_open(
    connection: AsyncConnection, tmp_path: Path
) -> None:
    """Every property above on _fixed's history."""
    history, schedule, later = _fixed()
    assert await _follow(connection, history, schedule, tmp_path) == await _follow(
        connection, history, later, tmp_path
    )
    async with isolated(connection, history, tmp_path) as books:
        await books.follow(schedule)  # balance, warnings, reviews after every step
        before = await books.everything()
        ctx = await books.context()
        report = await normalize(ctx, await books.raw_record_ids())
        assert (report.created, report.updated) == (0, 0)
        assert await books.everything() == before
        await _delete_derived(books.session, books.connection)
        await books.replay()
        assert await books.everything() == before
