"""The opening balance adjustment (design 5.8, plan Decision 12): what the earliest report holds
beyond the records before it, derived afresh after every normalising run once the first sync's
backfill is complete, and the first sync's one message."""

from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

import httpx
from sqlalchemy import text

from lararium.core.commands import command_context
from lararium.core.connections import get_connection, update_config
from lararium.modules.finance.binance import sync as sync_module
from lararium.modules.finance.binance.accounts import wallet_account
from lararium.modules.finance.binance.instruments import asset
from lararium.modules.finance.binance.normalize import Run
from lararium.modules.finance.binance.notices import PENDING_LINES_KEY
from lararium.modules.finance.binance.opening import set_openings
from lararium.modules.finance.snapshots import record_snapshot

from .fixtures import ADDRESS, DAY_MS, HOUR_MS, NOW_MS, FakeBinance, build_row
from .normalizing import ingest, run_normalize
from .syncing import (
    CONNECTOR,
    Clock,
    RecordingNotifier,
    binance_connection,
    complete_cursors,
    context,
    later_config,
    run_binance_sync,
)


def _refuse(request: httpx.Request) -> httpx.Response:
    raise AssertionError("no HTTP in this test")


def _ms(at: datetime) -> int:
    return int(at.timestamp()) * 1000


def _utc(day: str) -> datetime:
    return datetime.fromisoformat(day).replace(tzinfo=UTC)


def _deposit(seq: int, at: datetime, amount: str, coin: str = "AAA") -> dict:
    return build_row(
        "deposits",
        seq,
        coin=coin,
        amount=amount,
        status=1,
        walletType=0,
        insertTime=_ms(at),
        completeTime=_ms(at),
    )


async def _deposits(session, connection_id: UUID, *rows: dict) -> None:
    await ingest(session, connection_id, "deposits", rows)
    await run_normalize(session, connection_id)


async def _openings(session, connection_id: UUID) -> int:
    async with httpx.AsyncClient(transport=httpx.MockTransport(_refuse)) as http:
        ctx = await context(session, connection_id, http)
        async with command_context(session, CONNECTOR, "binance.opening", {}) as cmd:
            return await set_openings(await Run.start(ctx, cmd, ctx.connection))


async def _spot_aaa(session, connection_id: UUID) -> tuple[UUID, UUID]:
    """The Spot account and the AAA instrument the connection's deposits posted to."""
    row = (
        await session.execute(
            text(
                "select p.account_id, p.instrument_id from finance.postings p"
                " join finance.transactions t on t.id = p.transaction_id"
                " join finance.accounts a on a.id = p.account_id"
                " where t.connection_id = :c and a.name = 'Binance Spot' limit 1"
            ),
            {"c": connection_id},
        )
    ).one()
    return row.account_id, row.instrument_id


async def _report(run, account_id: UUID, instrument_id: UUID, quantity: str, at: datetime):
    await run(
        record_snapshot,
        account_id=account_id,
        instrument_id=instrument_id,
        quantity=Decimal(quantity),
        as_of=at,
        as_of_date=at.date(),
        kind="reported",
    )


async def _opening(session, connection_id: UUID) -> Any:
    """The connection's opening transactions: key, status, booking date, time, own quantity."""
    rows = await session.execute(
        text(
            "select t.external_id, t.status, t.booking_date, t.occurred_at, p.quantity"
            " from finance.transactions t"
            " join finance.postings p on p.transaction_id = t.id"
            " join finance.accounts a on a.id = p.account_id"
            " where t.connection_id = :c and t.external_id like 'opening:%'"
            " and a.source_connection_id = :c"
        ),
        {"c": connection_id},
    )
    return list(rows)


async def _balance(session, account_id: UUID, instrument_id: UUID, day: date) -> Decimal | None:
    return await session.scalar(
        text(
            "select quantity from finance.v1_balances_asof(:d)"
            " where account_id = :a and instrument_id = :i"
        ),
        {"d": day, "a": account_id, "i": instrument_id},
    )


async def test_opening_is_the_residual_at_the_first_report(session, run) -> None:
    connection = await binance_connection(run, config=later_config())
    await _deposits(
        session,
        connection.id,
        _deposit(0, _utc("2026-11-01T10:00"), "3"),
        _deposit(1, _utc("2026-11-10T10:00"), "4"),
    )
    account_id, instrument_id = await _spot_aaa(session, connection.id)
    await _report(run, account_id, instrument_id, "10", _utc("2026-11-20T12:00"))

    assert await _openings(session, connection.id) == 1

    [opening] = await _opening(session, connection.id)
    assert opening.external_id == f"opening:spot:{instrument_id}"
    assert opening.status != "void" and opening.quantity == 3
    assert opening.booking_date == date(2026, 10, 31)
    assert opening.occurred_at == _utc("2026-10-31T00:00")
    # Between the opening and the report: the records so far plus the opening.
    assert await _balance(session, account_id, instrument_id, date(2026, 11, 5)) == 6
    assert await _balance(session, account_id, instrument_id, date(2026, 11, 20)) == 10
    # Set afresh, it is the same.
    assert await _openings(session, connection.id) == 1
    assert len(await _opening(session, connection.id)) == 1


async def test_opening_moves_when_an_old_record_arrives(session, run) -> None:
    connection = await binance_connection(run, config=later_config())
    await _deposits(session, connection.id, _deposit(0, _utc("2026-11-10T10:00"), "7"))
    account_id, instrument_id = await _spot_aaa(session, connection.id)
    await _report(run, account_id, instrument_id, "10", _utc("2026-11-20T12:00"))
    await _openings(session, connection.id)

    await _deposits(session, connection.id, _deposit(1, _utc("2026-10-20T10:00"), "2"))
    assert await _openings(session, connection.id) == 1

    [opening] = await _opening(session, connection.id)
    assert (opening.quantity, opening.booking_date) == (1, date(2026, 10, 19))

    # The history now explains all of the report: the opening is voided.
    await _deposits(session, connection.id, _deposit(2, _utc("2026-10-15T10:00"), "1"))
    assert await _openings(session, connection.id) == 0

    [voided] = await _opening(session, connection.id)
    assert voided.status == "void"
    assert await _balance(session, account_id, instrument_id, date(2026, 11, 20)) == 10


async def test_opening_is_never_after_the_earliest_report(session, run) -> None:
    # The account's records all come after its earliest report (a daily snapshot): the opening
    # is the report's own time, or it would fall inside an interval after it.
    connection = await binance_connection(run, config=later_config())
    await _deposits(session, connection.id, _deposit(0, _utc("2026-11-10T10:00"), "4"))
    account_id, instrument_id = await _spot_aaa(session, connection.id)
    reported = _utc("2026-11-01T23:59:59")
    await _report(run, account_id, instrument_id, "10", reported)

    await _openings(session, connection.id)

    [opening] = await _opening(session, connection.id)
    assert (opening.quantity, opening.occurred_at) == (10, reported)


async def test_records_of_an_asset_the_earliest_report_omits_open_at_minus_their_sum(
    session, run
) -> None:
    # The report is complete: BBB is not in it, so the account held none of it then.
    connection = await binance_connection(run, config=later_config())
    await _deposits(
        session,
        connection.id,
        _deposit(0, _utc("2026-11-01T10:00"), "4"),
        _deposit(1, _utc("2026-11-02T10:00"), "5", coin="BBB"),
    )
    account_id, aaa = await _spot_aaa(session, connection.id)
    bbb = await run(asset, connection.id, "BBB")
    await _report(run, account_id, aaa, "4", _utc("2026-11-20T12:00"))

    assert await _openings(session, connection.id) == 1

    [opening] = await _opening(session, connection.id)
    assert opening.external_id == f"opening:spot:{bbb}"
    assert (opening.quantity, opening.booking_date) == (-5, date(2026, 10, 31))
    # With no snapshot of BBB, the hybrid rule sums its postings: 0 from the deposit on.
    assert await _balance(session, account_id, bbb, date(2026, 11, 20)) == 0
    assert await _balance(session, account_id, bbb, date(2026, 11, 2)) == 0
    assert await _balance(session, account_id, bbb, date(2026, 10, 31)) == -5


async def test_an_asset_first_reported_later_is_no_opening(session, run) -> None:
    connection = await binance_connection(run, config=later_config())
    await _deposits(session, connection.id, _deposit(0, _utc("2026-11-01T10:00"), "10"))
    account_id, aaa = await _spot_aaa(session, connection.id)
    bbb = await run(asset, connection.id, "BBB")
    await _report(run, account_id, aaa, "10", _utc("2026-11-20T12:00"))
    await _report(run, account_id, aaa, "10", _utc("2026-11-21T12:00"))
    await _report(run, account_id, bbb, "3", _utc("2026-11-21T12:00"))

    assert await _openings(session, connection.id) == 0
    assert await _opening(session, connection.id) == []


async def test_wallet_with_no_record_starts_at_its_report(session, run) -> None:
    connection = await binance_connection(run, config=later_config())
    account_id = await run(wallet_account, connection, "funding")
    instrument_id = await run(asset, connection.id, "BBB")
    reported = _utc("2026-11-20T12:00")
    await _report(run, account_id, instrument_id, "5", reported)

    assert await _openings(session, connection.id) == 1

    [opening] = await _opening(session, connection.id)
    assert opening.external_id == f"opening:funding:{instrument_id}"
    assert (opening.quantity, opening.occurred_at, opening.booking_date) == (
        5,
        reported,
        reported.date(),
    )
    assert await _balance(session, account_id, instrument_id, date(2026, 11, 19)) is None
    assert await _balance(session, account_id, instrument_id, date(2026, 11, 20)) == 5


async def test_no_opening_before_the_first_sync_finishes(session, run) -> None:
    cursors = complete_cursors(NOW_MS - HOUR_MS)
    cursors["withdrawals"] = {"fetched_to": NOW_MS, "backfilled_to": NOW_MS - DAY_MS}
    connection = await binance_connection(run, config={"cursors": cursors})
    await _deposits(session, connection.id, _deposit(0, _utc("2026-11-10T10:00"), "7"))
    account_id, instrument_id = await _spot_aaa(session, connection.id)
    await _report(run, account_id, instrument_id, "10", _utc("2026-11-20T12:00"))

    async def step() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(_refuse)) as http:
            await sync_module.opening(await context(session, connection.id, http))

    # The withdrawals' backfill is not complete: no opening, the first sync is not done.
    await step()

    assert await _opening(session, connection.id) == []
    config = (await get_connection(session, connection.id)).config
    assert "first_sync_done" not in config and PENDING_LINES_KEY not in config

    cursors["withdrawals"] = {**cursors["withdrawals"], "complete": True}
    await run(update_config, connection.id, {"cursors": cursors})
    await step()

    [opening] = await _opening(session, connection.id)
    assert opening.quantity == 3
    config = (await get_connection(session, connection.id)).config
    assert config["first_sync_done"] is True
    assert config[PENDING_LINES_KEY] == ["first sync finished: deposits 1; 1 opening adjustment"]


async def test_first_sync_notifies_once_with_counts(
    session, run, fake: FakeBinance, connector, clock: Clock
) -> None:
    fake.add("deposits", _deposit(0, _utc("2026-11-21T10:00"), "3"))
    fake.add("deposits", _deposit(1, _utc("2026-11-21T11:00"), "4", coin="BBB"))
    # Both assets have a EUR market, so none is flagged as unpriced.
    for coin in ("AAA", "BBB"):
        pair = f"{coin}EUR"
        fake.add(
            "exchange-info",
            build_row("exchange-info", symbol=pair, baseAsset=coin, quoteAsset="EUR"),
        )
        fake.add("klines", build_row("klines", **{"0": NOW_MS - DAY_MS}), when={"symbol": pair})
    fake.set_rows(
        "spot-account",
        [
            build_row("spot-account", asset="AAA", free="10", locked="0"),
            build_row("spot-account", asset="BBB", free="4", locked="0"),
        ],
    )
    config = later_config()
    del config["first_sync_done"]
    connection = await binance_connection(run, config=config)
    notifier = RecordingNotifier()

    after = await run_binance_sync(session, connection.id, fake.handler, notifier=notifier)

    assert after.status == "healthy", after.last_error
    assert after.config["first_sync_done"] is True
    [message] = [m for m in notifier.messages if "first sync" in m]
    assert message == "Binance: first sync finished: deposits 2; 1 opening adjustment"
    for value in ("AAA", "BBB", "10", "3", ADDRESS):
        assert value not in message.removeprefix("Binance: ")
    [opening] = await _opening(session, connection.id)
    assert opening.quantity == 7

    again = RecordingNotifier()
    clock.ms += 4 * HOUR_MS
    after = await run_binance_sync(session, connection.id, fake.handler, notifier=again)

    assert after.status == "healthy", after.last_error
    assert [m for m in again.messages if "first sync" in m] == []
