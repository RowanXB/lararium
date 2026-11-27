"""Which Monzo reversal cancels which authorisation (card refunds design §4.3): the pure
match, and a property test of the ledger it gives on generated histories."""

from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from hypothesis import event, given, note
from hypothesis import strategies as st
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession

from lararium.core.actors import Actor
from lararium.core.commands import command_context
from lararium.core.connections import update_config
from lararium.db import create_engine
from lararium.modules.finance.ledger import find_unbalanced_transactions
from lararium.modules.finance.monzo.normalize import NORMALIZER_VERSION_KEY
from lararium.modules.finance.monzo.reversals import reversal_pairs
from lararium.modules.finance.system_accounts import system_account_ids

from .fixtures import ACCOUNT_ID, make_card_transaction
from .test_normalize import _connection, _normalize  # pyright: ignore[reportPrivateUsage]


def _card(id: str, amount: int, created: str, **overrides: Any) -> dict[str, Any]:
    return make_card_transaction(id, amount, created, **overrides)


def _reversal(id: str, amount: int, created: str, **overrides: Any) -> dict[str, Any]:
    return make_card_transaction(id, amount, created, reversal=True, **overrides)


def _pairs(*copies: dict[str, Any]) -> dict[str, str]:
    """reversal_pairs of copies all fetched for one Monzo account."""
    return reversal_pairs((tx, ACCOUNT_ID) for tx in copies)


def test_an_exact_reversal_pairs() -> None:
    authorisation = _card("tx_A", -1000, "2026-03-01T12:00:00.000Z")
    reversal = _reversal("tx_R", 1000, "2026-03-01T15:00:00.000Z")

    assert _pairs(reversal, authorisation) == {"tx_R": "tx_A"}


def test_a_shared_lifecycle_without_a_reversal_pairs_nothing() -> None:
    # One authorisation cleared as two settled payments under one lifecycle.
    settled = "2026-03-02T06:00:00.000Z"
    first = _card("tx_A", -700, "2026-03-01T12:00:00.000Z", settled=settled)
    second = _card("tx_B", -360, "2026-03-01T12:00:01.000Z", settled=settled)

    assert _pairs(first, second) == {}


def test_a_partial_reversal_pairs_nothing() -> None:
    authorisation = _card("tx_A", -1000, "2026-03-01T12:00:00.000Z")
    reversal = _reversal("tx_R", 700, "2026-03-01T15:00:00.000Z")

    assert _pairs(authorisation, reversal) == {}


def test_a_settled_authorisation_is_not_reversed() -> None:
    authorisation = _card(
        "tx_A", -1000, "2026-03-01T12:00:00.000Z", settled="2026-03-02T06:00:00.000Z"
    )
    reversal = _reversal("tx_R", 1000, "2026-03-01T15:00:00.000Z")

    assert _pairs(authorisation, reversal) == {}


def test_two_authorisations_of_one_amount_pair_the_latest() -> None:
    earlier = _card("tx_A1", -1000, "2026-03-01T12:00:00.000Z")
    later = _card("tx_A2", -1000, "2026-03-01T13:00:00.000Z")
    reversal = _reversal("tx_R", 1000, "2026-03-01T15:00:00.000Z")

    assert _pairs(later, reversal, earlier) == {"tx_R": "tx_A2"}


def test_a_reversal_on_another_account_pairs_nothing() -> None:
    authorisation = _card("tx_A", -1000, "2026-03-01T12:00:00.000Z")
    reversal = _reversal("tx_R", 1000, "2026-03-01T15:00:00.000Z")

    assert reversal_pairs([(authorisation, "acc_TEST1"), (reversal, "acc_TEST2")]) == {}


def test_a_declined_reversal_pairs_nothing() -> None:
    authorisation = _card("tx_A", -1000, "2026-03-01T12:00:00.000Z")
    reversal = _reversal(
        "tx_R", 1000, "2026-03-01T15:00:00.000Z", decline_reason="INSUFFICIENT_FUNDS"
    )

    assert _pairs(authorisation, reversal) == {}


# A generated history: card authorisations on a few lifecycles, of two amounts, most of
# them reversed later, exactly or in part (a partial reversal), so that a reversal meets
# authorisations of its amount and of another, and two authorisations of one amount meet one
# reversal; with settled card payments and refunds on the same lifecycles.
_START = datetime(2026, 3, 1, 9, tzinfo=UTC)
_LIFECYCLES = st.integers(1, 2)
_AMOUNTS = st.sampled_from([1000, 500])


def _iso(value: datetime) -> str:
    return f"{value:%Y-%m-%dT%H:%M:%S}.000Z"


def _history_item(n: int, kind: str, lifecycle: int, amount: int) -> dict[str, Any]:
    """The nth transaction: an unsettled authorisation, a reversal, a settled payment or a
    settled refund, each a minute after the one before."""
    created = _START + timedelta(minutes=n)
    settled = "" if kind == "authorisation" else _iso(created + timedelta(hours=1))
    return make_card_transaction(
        f"tx_TEST{n:04d}",
        amount if kind in ("reversal", "refund") else -amount,
        _iso(created),
        lifecycle=f"mclifecycle_TEST{lifecycle}",
        reversal=kind == "reversal",
        settled=settled,
    )


@st.composite
def _histories(draw: st.DrawFn) -> list[dict[str, Any]]:
    events: list[tuple[str, int, int]] = []
    later: list[tuple[int, int]] = []  # each reversal's index in events, and its authorisation's
    for lifecycle, amount, reversed_by in draw(
        st.lists(
            st.tuples(_LIFECYCLES, _AMOUNTS, st.sampled_from([None, "exact", "exact", "part"])),
            min_size=1,
            max_size=4,
        )
    ):
        events.append(("authorisation", lifecycle, amount))
        if reversed_by is not None:
            events.append(("reversal", lifecycle, amount if reversed_by == "exact" else 300))
            later.append((len(events) - 1, len(events) - 2))
    others = st.tuples(st.sampled_from(["payment", "refund"]), _LIFECYCLES, _AMOUNTS)
    events += draw(st.lists(others, max_size=3))
    positions = draw(st.permutations(range(len(events))))
    # A reversal comes after the authorisation it was made for.
    for reversal, authorisation in later:
        if positions[reversal] < positions[authorisation]:
            positions[reversal], positions[authorisation] = (
                positions[authorisation],
                positions[reversal],
            )
    by_position = sorted(range(len(events)), key=lambda i: positions[i])
    return [_history_item(n, *events[i]) for n, i in enumerate(by_position)]


@pytest.fixture
async def connection(backend_url: str, migrated: None) -> AsyncIterator[AsyncConnection]:
    engine = create_engine(backend_url)
    try:
        async with engine.connect() as conn:
            yield conn
    finally:
        await engine.dispose()


@asynccontextmanager
async def _isolated(conn: AsyncConnection) -> AsyncGenerator[tuple[AsyncSession, Any]]:
    """A session in a transaction of its own, rolled back at the end, with a `run`."""
    outer = await conn.begin()
    session = AsyncSession(bind=conn, join_transaction_mode="create_savepoint")

    async def run(fn: Any, /, *args: Any, **kwargs: Any) -> Any:
        async with command_context(session, Actor("user", "test"), "test", {}) as ctx:
            return await fn(ctx, *args, **kwargs)

    try:
        yield session, run
    finally:
        await session.close()
        await outer.rollback()


async def _ledger(session: AsyncSession) -> set[tuple[Any, ...]]:
    """Every transaction: (external id, status, contra account, refund, cancels external id),
    the contra named by its system account key."""
    names = {account_id: key for key, account_id in (await system_account_ids(session)).items()}
    rows = await session.execute(
        text(
            "select t.external_id, t.status, p.account_id, t.refund, c.external_id as cancels"
            " from finance.transactions t"
            " join finance.postings p on p.transaction_id = t.id and p.position = 1"
            " left join finance.transactions c on c.id = t.cancels_transaction_id"
        )
    )
    return {(r.external_id, r.status, names.get(r.account_id), r.refund, r.cancels) for r in rows}


async def _check(session: AsyncSession) -> None:
    """Every transaction balances, and every cancelled pair sums to zero on In transit."""
    assert await find_unbalanced_transactions(session) == []
    in_transit = (await system_account_ids(session)).get("in_transit")
    unbalanced = await session.execute(
        text(
            "select c.external_id, sum(p.quantity) from finance.transactions c"
            " join finance.transactions d on d.id = c.cancels_transaction_id"
            " join finance.postings p on p.transaction_id in (c.id, d.id)"
            " where c.status <> 'void' and d.status <> 'void' and p.account_id = :i"
            " group by c.external_id having sum(p.quantity) <> 0"
        ),
        {"i": in_transit},
    )
    assert unbalanced.all() == []


async def _sync_by_sync(conn: AsyncConnection, syncs: list[list[dict[str, Any]]]) -> set[Any]:
    async with _isolated(conn) as (session, run):
        connection = await _connection(run)
        for pages in syncs:
            await _normalize(session, connection, transactions=pages)
            await _check(session)
        return await _ledger(session)


async def _at_once_then_replayed(conn: AsyncConnection, history: list[dict[str, Any]]) -> set[Any]:
    async with _isolated(conn) as (session, run):
        connection = await _connection(run)
        await _normalize(session, connection, transactions=history)
        await _check(session)
        once = await _ledger(session)
        await run(update_config, connection.id, {NORMALIZER_VERSION_KEY: "monzo-norm-5"})
        await _normalize(session, connection)
        await _check(session)
        replayed = await _ledger(session)
        assert replayed == once
        return replayed


@given(history=_histories(), data=st.data())
async def test_any_arrival_order_gives_one_ledger(
    connection: AsyncConnection, history: list[dict[str, Any]], data: st.DataObject
) -> None:
    order = data.draw(st.permutations(history), label="order")
    groups = data.draw(
        st.lists(st.integers(0, 3), min_size=len(order), max_size=len(order)), label="syncs"
    )
    syncs = [[tx for tx, g in zip(order, groups, strict=True) if g == n] for n in range(4)]
    pairs = _pairs(*history)
    note(f"pairs: {pairs}")
    event(f"pairs: {min(len(pairs), 2)}")
    sync_of = {tx["id"]: g for tx, g in zip(order, groups, strict=True)}
    early = any(sync_of[r] < sync_of[a] for r, a in pairs.items())
    event(f"a reversal in a sync before its authorisation's: {early}")
    event(f"a pair split across syncs: {any(sync_of[r] != sync_of[a] for r, a in pairs.items())}")

    by_sync = await _sync_by_sync(connection, syncs)
    at_once = await _at_once_then_replayed(connection, history)

    assert by_sync == at_once
