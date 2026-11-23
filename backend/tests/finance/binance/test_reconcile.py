"""Reconciling Binance's reported balances against the records posted (design 5.11, plan
Decision 11): the differences stored, warned and told once, closed when the record that
explains them arrives, and the trade sweep after an unexplained change (design 5.2)."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import text

from lararium.modules.finance.binance.parse import ApiValue
from lararium.modules.finance.binance.sync import SWEPT_KEY

from .fixtures import HOUR_MS, FakeBinance, build_row
from .syncing import (
    Clock,
    RecordingNotifier,
    binance_connection,
    later_config,
    run_binance_sync,
)

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

    [first, _] = await _as_of(session, connection.id, "Spot", "STB")
    swept = [s for s in fake.calls("my-trades") if "startTime" in s.params]
    assert sorted({s.params["symbol"] for s in swept}) == ["AAASTB", "ZZZSTB"]
    assert all(int(s.params["startTime"]) <= start for s in swept[:1])
    assert await _differences(session, connection.id) == []
    assert _told(notifier) == []
    assert not [w for w in after.warnings if _CHANGED in w]
    assert after.config[SWEPT_KEY] == [["STB", _ms(first)]]

    fake.requests.clear()
    await _later(session, connection, fake, clock)

    assert [s for s in fake.calls("my-trades") if "startTime" in s.params] == []
    assert await _differences(session, connection.id) == []
