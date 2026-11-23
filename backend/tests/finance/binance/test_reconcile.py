"""Reconciling Binance's reported balances against the records posted (design 5.11, plan
Decision 11): the differences stored, warned and told once, closed when the record that
explains them arrives, and the trade sweep after an unexplained change (design 5.2)."""

import itertools
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text

from lararium.core.actors import Actor
from lararium.core.commands import command_context
from lararium.core.connections import create_connection, store_credential
from lararium.db import create_engine, make_sessionmaker
from lararium.modules.finance.binance import sync as sync_module
from lararium.modules.finance.binance.parse import ApiValue
from lararium.modules.finance.binance.reconcile import TOUCHED_KEY
from lararium.modules.finance.binance.sync import SWEPT_KEY

from .fixtures import HOUR_MS, FakeBinance, build_row
from .syncing import (
    API_KEY,
    API_SECRET,
    KEY,
    UID,
    Clock,
    RecordingNotifier,
    binance_connection,
    later_config,
    run_binance_sync,
)
from .test_sync import _delete, _system_accounts

_CHANGED = "changed by something no record explains"
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


def _ms(at: datetime) -> int:
    return (at - _EPOCH) // timedelta(milliseconds=1)


def _spot(*balances: tuple[str, str]) -> list[dict[str, ApiValue]]:
    return [build_row("spot-account", asset=code, free=free, locked="0") for code, free in balances]


def _earn(*assets: tuple[str, str]) -> list[dict[str, ApiValue]]:
    listed: list[ApiValue] = [
        {
            "asset": code,
            "assetName": code,
            "free": free,
            "locked": "0",
            "freeze": "0",
            "withdrawing": "0",
            "btcValuation": "0",
        }
        for code, free in assets
    ]
    return [build_row("wallet-balance", walletName="Earn", balance="1", assetBalances=listed)]


def _pairs(fake: FakeBinance, *pairs: tuple[str, str]) -> None:
    fake.set_rows(
        "exchange-info",
        [build_row("exchange-info", symbol=b + q, baseAsset=b, quoteAsset=q) for b, q in pairs],
    )


def _deposit(seq: int, at_ms: int, amount: str, coin: str = "AAA") -> dict[str, ApiValue]:
    return build_row(
        "deposits",
        seq,
        coin=coin,
        amount=amount,
        status=1,
        walletType=0,
        insertTime=at_ms,
        completeTime=at_ms,
    )


def _trade(seq: int, at_ms: int, symbol: str, *, buyer: bool, qty: str, price: str) -> dict:
    return build_row(
        "my-trades",
        seq,
        symbol=symbol,
        time=at_ms,
        isBuyer=buyer,
        qty=qty,
        price=price,
        quoteQty=str(Decimal(qty) * Decimal(price)),
        commission="0",
        commissionAsset="AAA",
    )


async def _differences(session, connection_id: UUID) -> list[Any]:
    rows = await session.execute(
        text(
            "select a.name as wallet, i.symbol, d.from_at, d.to_at, d.reported_change,"
            " d.posted_sum, d.opened_at, d.closed_at"
            " from finance.binance_reconciliation_differences d"
            " join finance.accounts a on a.id = d.account_id"
            " join finance.instruments i on i.id = d.instrument_id"
            " where a.source_connection_id = :c order by d.from_at, a.name, i.symbol"
        ),
        {"c": connection_id},
    )
    return list(rows)


async def _as_of(session, connection_id: UUID, wallet: str, symbol: str) -> list[datetime]:
    """The `as_of` of each reported snapshot of the wallet's asset, oldest first."""
    found = await session.scalars(
        text(
            "select s.as_of from finance.balance_snapshots s"
            " join finance.accounts a on a.id = s.account_id"
            " join finance.instruments i on i.id = s.instrument_id"
            " where a.source_connection_id = :c and a.name = :w and i.symbol = :s"
            " and s.kind = 'reported' order by s.as_of"
        ),
        {"c": connection_id, "w": f"Binance {wallet}", "s": symbol},
    )
    return list(found)


async def _balance(session, connection_id: UUID, wallet: str, symbol: str, day) -> Decimal:
    return await session.scalar(
        text(
            "select b.quantity from finance.v1_balances_asof(:day) b"
            " join finance.accounts a on a.id = b.account_id"
            " join finance.instruments i on i.id = b.instrument_id"
            " where a.source_connection_id = :c and a.name = :w and i.symbol = :s"
        ),
        {"day": day, "c": connection_id, "w": f"Binance {wallet}", "s": symbol},
    )


def _shown(at: datetime) -> str:
    return at.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S")


def _warning(wallet: str, symbol: str, from_at: datetime, to_at: datetime) -> str:
    return (
        f"Binance {wallet}: {symbol} {_CHANGED} between {_shown(from_at)} and {_shown(to_at)} (UTC)"
    )


def _told(notifier: RecordingNotifier) -> list[str]:
    return [m for m in notifier.messages if _CHANGED in m]


def _covers(swept: list, from_at: datetime, to_at: datetime) -> None:
    """Each symbol's sweep asked the whole interval, window after window."""
    for symbol in {s.params["symbol"] for s in swept}:
        asked = sorted(
            (int(s.params["startTime"]), int(s.params["endTime"]))
            for s in swept
            if s.params["symbol"] == symbol
        )
        assert asked[0][0] == _ms(from_at) and asked[-1][1] == _ms(to_at)
        assert all(b[0] == a[1] + 1 for a, b in itertools.pairwise(asked))


async def _openings_of(session, connection_id: UUID, symbol: str) -> list[Any]:
    rows = await session.execute(
        text(
            "select t.id from finance.transactions t"
            " join finance.postings p on p.transaction_id = t.id"
            " join finance.instruments i on i.id = p.instrument_id"
            " where t.connection_id = :c and t.external_id like 'opening:%'"
            " and t.status <> 'void' and i.symbol = :s"
        ),
        {"c": connection_id, "s": symbol},
    )
    return list(rows)


async def _later(session, connection, fake: FakeBinance, clock: Clock, notifier=None):
    clock.ms += 4 * HOUR_MS
    after = await run_binance_sync(session, connection.id, fake.handler, notifier=notifier)
    assert after.status == "healthy", after.last_error
    return after


async def test_consistent_history_has_no_difference(
    session, run, fake: FakeBinance, connector, clock: Clock
) -> None:
    _pairs(fake, ("AAA", "BBB"))
    fake.set_rows("spot-account", _spot(("AAA", "10"), ("BBB", "20")))
    connection = await binance_connection(run, config=later_config())
    await run_binance_sync(session, connection.id, fake.handler)
    start = clock.ms
    fake.add("deposits", _deposit(0, start + HOUR_MS, "5"))
    fake.add("my-trades", _trade(0, start + 2 * HOUR_MS, "AAABBB", buyer=True, qty="2", price="3"))
    subscription = build_row(
        "earn-flexible-subscriptions",
        time=start + 3 * HOUR_MS,
        asset="AAA",
        amount="3",
        sourceAccount="SPOT",
        status="SUCCESS",
    )
    fake.add("earn-flexible-subscriptions", subscription)
    fake.set_rows("spot-account", _spot(("AAA", "14"), ("BBB", "14")))
    fake.set_rows("wallet-balance", _earn(("AAA", "3")))
    notifier = RecordingNotifier()

    after = await _later(session, connection, fake, clock, notifier)

    assert len(await _as_of(session, connection.id, "Spot", "AAA")) == 2
    assert await _differences(session, connection.id) == []
    assert not [w for w in after.warnings if _CHANGED in w]
    assert _told(notifier) == []


async def test_missing_record_is_one_difference_once(
    session, run, fake: FakeBinance, connector, clock: Clock
) -> None:
    fake.set_rows("spot-account", _spot(("AAA", "10")))
    connection = await binance_connection(run, config=later_config())
    await run_binance_sync(session, connection.id, fake.handler)
    fake.set_rows("spot-account", _spot(("AAA", "12.5")))
    notifier = RecordingNotifier()

    after = await _later(session, connection, fake, clock, notifier)

    first, second = await _as_of(session, connection.id, "Spot", "AAA")
    [difference] = await _differences(session, connection.id)
    assert (difference.wallet, difference.symbol) == ("Binance Spot", "AAA")
    assert (difference.from_at, difference.to_at) == (first, second)
    assert (difference.reported_change, difference.posted_sum) == (Decimal("2.5"), 0)
    assert difference.closed_at is None
    warning = _warning("Spot", "AAA", first, second)
    assert [w for w in after.warnings if _CHANGED in w] == [warning]
    assert "2.5" not in warning
    [message] = _told(notifier)
    assert warning in message and "2.5" not in message

    # Nothing new: no second message; the difference and its warning stand.
    again = RecordingNotifier()
    after = await _later(session, connection, fake, clock, again)

    assert _told(again) == []
    assert len(await _differences(session, connection.id)) == 1
    assert [w for w in after.warnings if _CHANGED in w] == [warning]


async def test_late_record_closes_its_interval(
    session, run, fake: FakeBinance, connector, clock: Clock
) -> None:
    fake.set_rows("spot-account", _spot(("AAA", "10")))
    connection = await binance_connection(run, config=later_config())
    await run_binance_sync(session, connection.id, fake.handler)
    deposited = clock.ms + HOUR_MS
    fake.set_rows("spot-account", _spot(("AAA", "15")))
    await _later(session, connection, fake, clock)
    [opened] = await _differences(session, connection.id)
    assert opened.closed_at is None

    # Binance lists the deposit only now, dated inside the old interval.
    fake.add("deposits", _deposit(0, deposited, "5"))
    after = await _later(session, connection, fake, clock)

    [closed] = await _differences(session, connection.id)
    assert (closed.from_at, closed.to_at) == (opened.from_at, opened.to_at)
    assert closed.closed_at is not None
    assert not [w for w in after.warnings if _CHANGED in w]
    # The report already held the deposit: it is not counted again.
    newest = (await _as_of(session, connection.id, "Spot", "AAA"))[-1]
    assert await _balance(session, connection.id, "Spot", "AAA", newest.date()) == 15


async def test_changed_old_record_reopens_its_interval(
    session, run, fake: FakeBinance, connector, clock: Clock
) -> None:
    fake.set_rows("spot-account", _spot(("AAA", "10")))
    connection = await binance_connection(run, config=later_config())
    await run_binance_sync(session, connection.id, fake.handler)
    deposited = clock.ms + HOUR_MS
    fake.add("deposits", _deposit(0, deposited, "5"))
    fake.set_rows("spot-account", _spot(("AAA", "15")))
    await _later(session, connection, fake, clock)
    assert await _differences(session, connection.id) == []

    # The deposit's amount is corrected in place, after the report that held it.
    fake.set_rows("deposits", [_deposit(0, deposited, "6")])
    after = await _later(session, connection, fake, clock)

    first, second, _ = await _as_of(session, connection.id, "Spot", "AAA")
    [difference] = await _differences(session, connection.id)
    assert (difference.from_at, difference.to_at) == (first, second)
    assert (difference.reported_change, difference.posted_sum) == (5, 6)
    assert difference.closed_at is None
    assert [w for w in after.warnings if _CHANGED in w] == [_warning("Spot", "AAA", first, second)]


@pytest.mark.parametrize(
    ("held", "differs"),
    [("100.8", False), ("100.5", False), ("101.1", True), ("100.4", True)],
)
async def test_earn_allowance(
    session, run, fake: FakeBinance, connector, clock: Clock, held: str, differs: bool
) -> None:
    fake.set_rows("wallet-balance", _earn(("STB", "100")))
    connection = await binance_connection(run, config=later_config())
    await run_binance_sync(session, connection.id, fake.handler)
    reward = build_row(
        "earn-flexible-rewards",
        asset="STB",
        rewards="0.5",
        type="REALTIME",
        time=clock.ms + HOUR_MS,
    )
    fake.add("earn-flexible-rewards", reward)
    # The position runs ahead of its records by the reward still accruing: 0.3 is less than
    # the last REALTIME reward (0.5), 0.6 is more; behind is never the reward.
    fake.set_rows("wallet-balance", _earn(("STB", held)))

    await _later(session, connection, fake, clock)

    found = await _differences(session, connection.id)
    assert bool(found) is differs
    if differs:
        [difference] = found
        assert difference.wallet == "Binance Earn"
        assert difference.posted_sum == Decimal("0.5")


async def test_round_trip_through_unseen_asset_found_by_sweep(
    session, run, fake: FakeBinance, connector, clock: Clock
) -> None:
    _pairs(fake, ("ZZZ", "STB"), ("AAA", "BBB"), ("AAA", "STB"))
    fake.set_rows("spot-account", _spot(("STB", "100")))
    connection = await binance_connection(run, config=later_config())
    await run_binance_sync(session, connection.id, fake.handler)
    start = clock.ms
    # STB falls and rises through ZZZ, which no balance ever shows.
    fake.add(
        "my-trades",
        _trade(0, start + HOUR_MS, "ZZZSTB", buyer=True, qty="5", price="2"),
        _trade(1, start + 2 * HOUR_MS, "ZZZSTB", buyer=False, qty="5", price="2.4"),
    )
    fake.set_rows("spot-account", _spot(("STB", "102")))
    fake.requests.clear()
    notifier = RecordingNotifier()

    after = await _later(session, connection, fake, clock, notifier)

    [first, second] = await _as_of(session, connection.id, "Spot", "STB")
    swept = [s for s in fake.calls("my-trades") if "startTime" in s.params]
    assert sorted({s.params["symbol"] for s in swept}) == ["AAASTB", "ZZZSTB"]
    _covers(swept, first, second)
    assert await _differences(session, connection.id) == []
    assert _told(notifier) == []
    assert not [w for w in after.warnings if _CHANGED in w]
    assert after.config[SWEPT_KEY] == [["STB", _ms(first)]]

    fake.requests.clear()
    await _later(session, connection, fake, clock)

    assert [s for s in fake.calls("my-trades") if "startTime" in s.params] == []
    assert await _differences(session, connection.id) == []


async def test_a_difference_the_sweep_does_not_explain_is_told_once(
    session, run, fake: FakeBinance, connector, clock: Clock
) -> None:
    _pairs(fake, ("ZZZ", "STB"))
    fake.set_rows("spot-account", _spot(("STB", "100")))
    connection = await binance_connection(run, config=later_config())
    await run_binance_sync(session, connection.id, fake.handler)
    fake.set_rows("spot-account", _spot(("STB", "102")))
    fake.requests.clear()
    notifier = RecordingNotifier()

    await _later(session, connection, fake, clock, notifier)

    first, second = await _as_of(session, connection.id, "Spot", "STB")
    swept = [s for s in fake.calls("my-trades") if "startTime" in s.params]
    _covers(swept, first, second)
    [difference] = await _differences(session, connection.id)
    assert (difference.from_at, difference.to_at, difference.reported_change) == (first, second, 2)
    assert len(_told(notifier)) == 1

    fake.requests.clear()
    again = RecordingNotifier()
    await _later(session, connection, fake, clock, again)

    assert [s for s in fake.calls("my-trades") if "startTime" in s.params] == []
    [still] = await _differences(session, connection.id)
    assert still.closed_at is None
    assert _told(again) == []


async def test_a_new_asset_with_no_record_is_a_difference_not_an_opening(
    session, run, fake: FakeBinance, connector, clock: Clock
) -> None:
    fake.set_rows("spot-account", _spot(("AAA", "10")))
    connection = await binance_connection(run, config=later_config())
    await run_binance_sync(session, connection.id, fake.handler)
    fake.set_rows("spot-account", _spot(("AAA", "10"), ("BBB", "3")))
    notifier = RecordingNotifier()

    await _later(session, connection, fake, clock, notifier)

    first, second = await _as_of(session, connection.id, "Spot", "AAA")
    [difference] = await _differences(session, connection.id)
    assert (difference.symbol, difference.from_at, difference.to_at) == ("BBB", first, second)
    assert (difference.reported_change, difference.posted_sum) == (3, 0)
    assert await _openings_of(session, connection.id, "BBB") == []
    assert len(_told(notifier)) == 1


async def test_a_trade_of_assets_first_seen_in_its_sync_is_fetched_by_it(
    session, run, fake: FakeBinance, connector, clock: Clock
) -> None:
    # The Spot account is first reported by the sync that first sees AAA and BBB: its read and
    # its deposit record name them, so that sync walks their symbol too.
    _pairs(fake, ("AAA", "BBB"))
    connection = await binance_connection(run, config=later_config())
    await run_binance_sync(session, connection.id, fake.handler)
    fake.add("deposits", _deposit(0, clock.ms + HOUR_MS, "10", coin="BBB"))
    fake.add(
        "my-trades", _trade(0, clock.ms + 2 * HOUR_MS, "AAABBB", buyer=True, qty="3", price="2")
    )
    fake.set_rows("spot-account", _spot(("AAA", "3"), ("BBB", "4")))

    await _later(session, connection, fake, clock)

    assert await _openings_of(session, connection.id, "AAA") == []
    assert await _openings_of(session, connection.id, "BBB") == []
    assert await _differences(session, connection.id) == []


async def test_a_new_asset_bought_is_neither_a_difference_nor_an_opening(
    session, run, fake: FakeBinance, connector, clock: Clock
) -> None:
    _pairs(fake, ("BBB", "AAA"))
    fake.set_rows("spot-account", _spot(("AAA", "10")))
    connection = await binance_connection(run, config=later_config())
    await run_binance_sync(session, connection.id, fake.handler)
    fake.add("my-trades", _trade(0, clock.ms + HOUR_MS, "BBBAAA", buyer=True, qty="3", price="2"))
    fake.set_rows("spot-account", _spot(("AAA", "4"), ("BBB", "3")))

    await _later(session, connection, fake, clock)

    assert await _differences(session, connection.id) == []
    assert await _openings_of(session, connection.id, "BBB") == []


async def test_first_sync_checks_every_daily_interval(
    session, run, fake: FakeBinance, connector, clock: Clock
) -> None:
    def daily(at_ms: int, free: str) -> dict[str, Any]:
        row = build_row("snapshot", type="SPOT", updateTime=at_ms)
        data = dict(row["data"])  # pyright: ignore[reportArgumentType,reportCallIssue]
        data["balances"] = [{"asset": "AAA", "free": free, "locked": "0"}]
        return {**row, "data": data}

    day = 24 * HOUR_MS
    days = [clock.ms - n * day - 1_000 for n in (3, 2, 1)]
    fake.add("snapshot", daily(days[0], "2"), daily(days[1], "5"), daily(days[2], "5"))
    fake.set_rows("spot-account", _spot(("AAA", "5")))
    config = later_config()
    del config["first_sync_done"]
    config["cursors"].pop("snapshot:SPOT")  # pyright: ignore[reportAttributeAccessIssue,reportUnknownMemberType]
    connection = await binance_connection(run, config=config)

    after = await run_binance_sync(session, connection.id, fake.handler)

    assert after.status == "healthy", after.last_error
    reported = await _as_of(session, connection.id, "Spot", "AAA")
    assert len(reported) == 4
    # A step between two daily snapshots, neither the newest interval nor holding a record.
    [difference] = await _differences(session, connection.id)
    assert (difference.from_at, difference.to_at) == (reported[0], reported[1])
    assert (difference.reported_change, difference.posted_sum) == (3, 0)


@asynccontextmanager
async def _committed(
    backend_url: str, owner_url: str, fake: FakeBinance, clock: Clock
) -> AsyncIterator[tuple[Any, UUID, Callable[[], Awaitable[Any]]]]:
    """A connection whose syncs commit for real (each step commits on its own, a failed
    sync's transaction rolls back), and a sync 4 hours after the last; deleted afterwards."""
    engine = create_engine(backend_url)
    sessions = make_sessionmaker(engine)
    connection_id: UUID | None = None
    system_before = await _system_accounts(owner_url)
    try:
        async with (
            sessions() as setup,
            command_context(setup, Actor("user", "dev"), "test.setup", {}) as ctx,
        ):
            connection = await create_connection(
                ctx,
                connector="binance",
                display_name=f"Binance {uuid4()}",
                config={"key_type": "hmac", "uid": UID, **later_config()},
            )
            connection_id = connection.id
            await store_credential(ctx, KEY, connection_id, "api_key", API_KEY, None)
            await store_credential(ctx, KEY, connection_id, "api_secret", API_SECRET, None)
            await setup.execute(
                text("update core.connections set status = 'healthy' where id = :id"),
                {"id": connection_id},
            )
        found = connection_id

        async def sync() -> Any:
            clock.ms += 4 * HOUR_MS
            async with sessions() as session:
                return await run_binance_sync(
                    session, found, fake.handler, session_factory=sessions
                )

        yield sessions, found, sync
    finally:
        if connection_id is not None:
            await _delete(engine, owner_url, connection_id, system_before)
        await engine.dispose()


async def _broken(*_: Any, **__: Any) -> Any:
    raise RuntimeError("a later step failed")


async def test_a_report_whose_sync_failed_later_is_checked(
    backend_url: str,
    owner_url: str,
    migrated: None,
    fake: FakeBinance,
    connector,
    clock: Clock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with _committed(backend_url, owner_url, fake, clock) as (sessions, connection_id, sync):
        fake.set_rows("spot-account", _spot(("AAA", "10")))
        assert (await sync()).status == "healthy"
        fake.set_rows("spot-account", _spot(("AAA", "12")))
        with monkeypatch.context() as patched:
            patched.setattr(sync_module, "opening", _broken)
            assert (await sync()).status == "failing"

        after = await sync()

        assert after.status == "healthy", after.last_error
        async with sessions() as session:
            first, second, third = await _as_of(session, connection_id, "Spot", "AAA")
            [difference] = await _differences(session, connection_id)
        assert (difference.from_at, difference.to_at) == (first, second)
        assert third > second


async def test_a_changed_old_record_reopens_its_interval_after_a_sync_that_failed_later(
    backend_url: str,
    owner_url: str,
    migrated: None,
    fake: FakeBinance,
    connector,
    clock: Clock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with _committed(backend_url, owner_url, fake, clock) as (sessions, connection_id, sync):
        fake.set_rows("spot-account", _spot(("AAA", "10")))
        assert (await sync()).status == "healthy"
        deposited = clock.ms + HOUR_MS
        fake.add("deposits", _deposit(0, deposited, "5"))
        fake.set_rows("spot-account", _spot(("AAA", "15")))
        assert (await sync()).status == "healthy"
        # The deposit is corrected in place; the sync that posts the change fails after it
        # committed and before it reconciled.
        fake.set_rows("deposits", [_deposit(0, deposited, "6")])
        with monkeypatch.context() as patched:
            patched.setattr(sync_module, "opening", _broken)
            failed = await sync()
            assert failed.status == "failing"
        assert failed.config[TOUCHED_KEY]

        # Nothing new: the old interval is checked again all the same.
        after = await sync()

        assert after.status == "healthy", after.last_error
        assert TOUCHED_KEY not in after.config
        async with sessions() as session:
            first, second, *_ = await _as_of(session, connection_id, "Spot", "AAA")
            [difference] = await _differences(session, connection_id)
        assert (difference.from_at, difference.to_at) == (first, second)
        assert (difference.reported_change, difference.posted_sum) == (5, 6)
        assert difference.closed_at is None


async def test_an_account_the_records_make_keeps_its_report_time_when_the_sync_dies_after(
    backend_url: str,
    owner_url: str,
    migrated: None,
    fake: FakeBinance,
    connector,
    clock: Clock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real = sync_module.normalize

    async def dies(ctx: Any, **kwargs: Any) -> Any:
        await real(ctx, **kwargs)
        raise RuntimeError("the process died after the records were committed")

    def earn(*assets: tuple[str, str]) -> list[dict[str, ApiValue]]:
        listed = _earn(*assets)[0]["assetBalances"] if assets else []
        shown = "1" if assets else "0"
        return [build_row("wallet-balance", walletName="Earn", balance=shown, assetBalances=listed)]

    async with _committed(backend_url, owner_url, fake, clock) as (sessions, connection_id, sync):
        # Earn took 3 and gave them back (records) and is read empty: its account is made by
        # the records, after the read.
        start = clock.ms
        fake.set_rows("spot-account", _spot(("AAA", "10")))
        fake.set_rows("wallet-balance", earn())
        subscription = build_row(
            "earn-flexible-subscriptions",
            time=start + HOUR_MS,
            asset="AAA",
            amount="3",
            sourceAccount="SPOT",
            status="SUCCESS",
        )
        redemption = build_row(
            "earn-flexible-redemptions",
            time=start + 2 * HOUR_MS,
            asset="AAA",
            amount="3",
            destAccount="SPOT",
            status="PAID",
        )
        fake.add("earn-flexible-subscriptions", subscription)
        fake.add("earn-flexible-redemptions", redemption)
        with monkeypatch.context() as patched:
            patched.setattr(sync_module, "normalize", dies)
            assert (await sync()).status == "failing"

        async with sessions() as session:
            assert len(await _reports(session, connection_id, "Earn")) == 1
        fake.set_rows("wallet-balance", earn(("BBB", "3")))

        after = await sync()

        assert after.status == "healthy", after.last_error
        async with sessions() as session:
            first, second = await _reports(session, connection_id, "Earn")
            [difference] = await _differences(session, connection_id)
            assert await _openings_of(session, connection_id, "BBB") == []
        assert (difference.wallet, difference.symbol) == ("Binance Earn", "BBB")
        assert (difference.from_at, difference.to_at) == (first, second)


# Each account's report times, an empty report included.


def _funding(code: str, free: str) -> dict[str, ApiValue]:
    return build_row(
        "funding-asset", asset=code, free=free, locked="0", freeze="0", withdrawing="0"
    )


def _withdrawal(seq: int, at_ms: int, amount: str) -> dict[str, ApiValue]:
    shown = datetime.fromtimestamp(at_ms // 1000, UTC).strftime("%Y-%m-%d %H:%M:%S")
    return build_row(
        "withdrawals",
        seq,
        coin="AAA",
        amount=amount,
        transactionFee="0",
        walletType=1,
        transferType=0,
        status=6,
        applyTime=shown,
        completeTime=shown,
    )


def _to_funding(seq: int, at_ms: int, amount: str) -> dict[str, ApiValue]:
    return {**_deposit(seq, at_ms, amount), "walletType": 1}


async def _reports(session, connection_id: UUID, wallet: str) -> list[datetime]:
    found = await session.scalars(
        text(
            "select r.as_of from finance.binance_account_reports r"
            " join finance.accounts a on a.id = r.account_id"
            " where r.connection_id = :c and a.name = :w order by r.as_of"
        ),
        {"c": connection_id, "w": f"Binance {wallet}"},
    )
    return list(found)


async def _emptied_funding(session, run, fake: FakeBinance, clock: Clock, *, first: bool = False):
    """A connection whose Funding received 5 and sent 5 (records), and is read empty by a sync
    whose own records make the Funding account (they are normalised after the read)."""
    fake.add("deposits", _to_funding(0, clock.ms - 3 * HOUR_MS, "5"))
    fake.add("withdrawals", _withdrawal(0, clock.ms - 2 * HOUR_MS, "5"))
    config = later_config()
    if first:
        del config["first_sync_done"]
    connection = await binance_connection(run, config=config)
    after = await run_binance_sync(session, connection.id, fake.handler)
    assert after.status == "healthy", after.last_error
    assert after.config["first_sync_done"] is True
    # The clear empty read is the new account's first report.
    assert len(await _reports(session, connection.id, "Funding")) == 1
    assert await _as_of(session, connection.id, "Funding", "AAA") == []
    return connection


async def test_an_unrecorded_asset_after_an_empty_report_is_a_difference_not_an_opening(
    session, run, fake: FakeBinance, connector, clock: Clock
) -> None:
    connection = await _emptied_funding(session, run, fake, clock)
    fake.set_rows("funding-asset", [_funding("BBB", "3")])

    await _later(session, connection, fake, clock)

    first, second = await _reports(session, connection.id, "Funding")
    [difference] = await _differences(session, connection.id)
    assert (difference.wallet, difference.symbol) == ("Binance Funding", "BBB")
    assert (difference.from_at, difference.to_at) == (first, second)
    assert (difference.reported_change, difference.posted_sum) == (3, 0)
    assert await _openings_of(session, connection.id, "BBB") == []
    assert await _openings_of(session, connection.id, "AAA") == []


async def test_records_between_two_empty_reports_are_a_difference(
    session, run, fake: FakeBinance, connector, clock: Clock
) -> None:
    connection = await _emptied_funding(session, run, fake, clock)
    fake.add("deposits", _to_funding(1, clock.ms + HOUR_MS, "2"))

    await _later(session, connection, fake, clock)

    first, second = await _reports(session, connection.id, "Funding")
    [difference] = await _differences(session, connection.id)
    assert (difference.wallet, difference.symbol) == ("Binance Funding", "AAA")
    assert (difference.from_at, difference.to_at) == (first, second)
    assert (difference.reported_change, difference.posted_sum) == (0, 2)


async def test_an_unclear_read_writes_no_report_time(
    session, run, fake: FakeBinance, connector, clock: Clock
) -> None:
    fake.set_rows("spot-account", _spot(("AAA", "10")))
    fake.set_rows("funding-asset", [_funding("BBB", "1")])
    connection = await binance_connection(run, config=later_config())
    await run_binance_sync(session, connection.id, fake.handler)

    def busy(asked: int) -> None:
        # A deposit inside every Spot read, and none near the other reads.
        fake.add("deposits", _deposit(100 + asked, clock.ms, "1"))
        fake.set_rows("spot-account", _spot(("AAA", str(11 + asked))))
        clock.ms += 10_000

    fake.when_asked("spot-account", busy)
    await _later(session, connection, fake, clock)

    assert len(await _reports(session, connection.id, "Spot")) == 1
    assert len(await _reports(session, connection.id, "Funding")) == 2
    assert await _differences(session, connection.id) == []


async def test_an_identical_sync_adds_one_report_time_per_account(
    session, run, fake: FakeBinance, connector, clock: Clock
) -> None:
    fake.set_rows("spot-account", _spot(("AAA", "10")))
    fake.set_rows("funding-asset", [_funding("BBB", "1")])
    fake.set_rows("wallet-balance", _earn(("STB", "4")))
    connection = await binance_connection(run, config=later_config())
    await run_binance_sync(session, connection.id, fake.handler)

    await _later(session, connection, fake, clock)

    for wallet in ("Spot", "Funding", "Earn"):
        reports = await _reports(session, connection.id, wallet)
        assert len(reports) == 2, wallet
        symbol = {"Spot": "AAA", "Funding": "BBB", "Earn": "STB"}[wallet]
        assert await _as_of(session, connection.id, wallet, symbol) == reports
    assert await _differences(session, connection.id) == []


async def test_a_first_sync_creating_an_account_from_records_reports_it(
    session, run, fake: FakeBinance, connector, clock: Clock
) -> None:
    connection = await _emptied_funding(session, run, fake, clock, first=True)
    # The records sum to 0 at the account's earliest report, which listed nothing.
    assert await _openings_of(session, connection.id, "AAA") == []
    fake.set_rows("funding-asset", [_funding("BBB", "3")])

    await _later(session, connection, fake, clock)

    first, second = await _reports(session, connection.id, "Funding")
    [difference] = await _differences(session, connection.id)
    assert (difference.wallet, difference.symbol) == ("Binance Funding", "BBB")
    assert (difference.from_at, difference.to_at, difference.reported_change) == (first, second, 3)
    assert await _openings_of(session, connection.id, "BBB") == []
    assert await _openings_of(session, connection.id, "AAA") == []


async def test_an_unclear_read_of_an_account_its_sync_creates_writes_no_report_time(
    session, run, fake: FakeBinance, connector, clock: Clock
) -> None:
    connection = await binance_connection(run, config=later_config())

    def busy(asked: int) -> None:
        # A Funding deposit inside every Funding read, and none near the other reads.
        fake.add("deposits", _to_funding(100 + asked, clock.ms, "1"))
        clock.ms += 10_000

    fake.when_asked("funding-asset", busy)
    after = await run_binance_sync(session, connection.id, fake.handler)

    assert after.status == "healthy", after.last_error
    assert len(fake.calls("funding-asset")) == 4
    assert await _as_of(session, connection.id, "Funding", "AAA") == []
    funding = await session.scalar(
        text("select count(*) from finance.accounts where source_connection_id = :c and name = :n"),
        {"c": connection.id, "n": "Binance Funding"},
    )
    assert funding == 1
    assert await _reports(session, connection.id, "Funding") == []
