"""Binance's reported balances (design 5.6, plan Decisions 8 and 9): each read's own cutoff, the
reads a movement falls into made again, each wallet's balance from its source, the typed report
tables, the daily snapshots of the first sync and the wallet checks."""

import json
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID

import httpx
import pytest
from sqlalchemy import text

from lararium.core.json_types import JSONValue
from lararium.modules.finance.binance import balances
from lararium.modules.finance.binance.balances import (
    Read,
    ambiguous,
    backfill_daily_snapshots,
    read_balances,
    report_balances,
    settle_reads,
    wallet_flags,
)
from lararium.modules.finance.binance.fetch import to_json, walk
from lararium.modules.finance.binance.parse import ApiValue, BinanceDataError
from lararium.modules.finance.binance.reconcile import open_differences

from .fixtures import HOUR_MS, NOW_MS, FakeBinance, build_response, build_row
from .syncing import (
    Clock,
    binance_connection,
    client_for,
    context,
    later_config,
    run_binance_sync,
)

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


def _at(ms: int) -> datetime:
    return _EPOCH + timedelta(milliseconds=ms)


def _ms(moment: datetime) -> int:
    return (moment - _EPOCH) // timedelta(milliseconds=1)


def _spot(*balances: tuple[str, str, str]) -> list[dict[str, ApiValue]]:
    return [
        build_row("spot-account", asset=code, free=free, locked=locked)
        for code, free, locked in balances
    ]


def _funding(code: str, free: str, locked: str, freeze: str, withdrawing: str) -> dict:
    return build_row(
        "funding-asset",
        asset=code,
        free=free,
        locked=locked,
        freeze=freeze,
        withdrawing=withdrawing,
    )


def _asset(code: str, free: str) -> dict[str, ApiValue]:
    return {
        "asset": code,
        "assetName": code,
        "free": free,
        "locked": "0",
        "freeze": "0",
        "withdrawing": "0",
        "btcValuation": "0",
    }


def _wallet(name: str, *assets: tuple[str, str], balance: str | None = None) -> dict:
    listed = [_asset(code, free) for code, free in assets]
    shown = balance if balance is not None else ("1" if assets else "0")
    return build_row("wallet-balance", walletName=name, balance=shown, assetBalances=listed)


def _read(label: str, rows: list, at_ms: int = NOW_MS, span_ms: int = 200) -> Read:
    body: JSONValue = to_json(build_response(label, rows))
    return Read(label, body, _at(at_ms), _at(at_ms + span_ms))


async def _report(run, connection, reads: dict[str, Read | None], flags: list[str] | None = None):
    sink = flags if flags is not None else []
    return await run(report_balances, connection, reads, flag=sink.append)


async def _snapshots(session, connection_id: UUID) -> list[tuple[str, str, Decimal, datetime]]:
    rows = await session.execute(
        text(
            "select a.name, i.symbol, s.quantity, s.as_of, s.as_of_date"
            " from finance.balance_snapshots s"
            " join finance.accounts a on a.id = s.account_id"
            " join finance.instruments i on i.id = s.instrument_id"
            " where a.source_connection_id = :c and s.kind = 'reported'"
            " order by s.as_of, a.name, i.symbol"
        ),
        {"c": connection_id},
    )
    found = []
    for row in rows:
        assert row.as_of_date == row.as_of.astimezone(UTC).date()
        found.append((row.name, row.symbol, row.quantity, row.as_of))
    return found


async def _balance(session, connection_id: UUID, wallet: str, symbol: str, day: date) -> Decimal:
    return await session.scalar(
        text(
            "select b.quantity from finance.v1_balances_asof(:day) b"
            " join finance.accounts a on a.id = b.account_id"
            " join finance.instruments i on i.id = b.instrument_id"
            " where a.source_connection_id = :c and a.name = :w and i.symbol = :s"
        ),
        {"day": day, "c": connection_id, "w": f"Binance {wallet}", "s": symbol},
    )


def _deposit(seq: int, at_ms: int, amount: str) -> dict:
    return build_row(
        "deposits",
        seq,
        coin="AAA",
        amount=amount,
        status=1,
        walletType=0,
        insertTime=at_ms,
        completeTime=at_ms,
    )


# One read's cutoff, and the reads a movement falls into.


@pytest.mark.parametrize("included", [True, False])
async def test_movement_during_balance_read_is_not_counted_twice(
    session, run, fake: FakeBinance, connector, clock: Clock, included: bool
) -> None:
    fake.set_rows("spot-account", _spot(("AAA", "10", "0")))
    deposited: list[int] = []

    def credit(asked: int) -> None:
        if asked == 0:
            # Credited while Binance answers the Spot read, which may or may not include it.
            deposited.append(clock.ms)
            fake.add("deposits", _deposit(0, clock.ms, "5"))
            if included:
                fake.set_rows("spot-account", _spot(("AAA", "15", "0")))
            clock.ms += 10_000
        elif asked == 1 and not included:
            fake.set_rows("spot-account", _spot(("AAA", "15", "0")))

    fake.when_asked("spot-account", credit)
    connection = await binance_connection(run, config=later_config())

    after = await run_binance_sync(session, connection.id, fake.handler)

    assert after.status == "healthy", after.last_error
    assert len(fake.calls("spot-account")) == 2
    [(wallet, symbol, quantity, as_of)] = await _snapshots(session, connection.id)
    assert (wallet, symbol, quantity) == ("Binance Spot", "AAA", Decimal(15))
    assert as_of > _at(deposited[0])
    assert await _balance(session, connection.id, "Spot", "AAA", as_of.date()) == 15
    assert await open_differences(session, connection.id) == []


async def test_transfer_between_two_wallet_reads(
    session, run, fake: FakeBinance, connector, clock: Clock
) -> None:
    # Spot is read before the wallet list that holds Earn. Phase A lists no Spot -> Funding
    # transfer, so the movement between the two reads is a flexible Earn subscription from Spot.
    fake.set_rows("spot-account", _spot(("AAA", "10", "0")))
    fake.set_rows("wallet-balance", [_wallet("Earn", ("AAA", "3"))])
    moved: list[int] = []

    def subscribe(asked: int) -> None:
        if asked == 0:
            moved.append(clock.ms + 5_000)
            row = build_row(
                "earn-flexible-subscriptions",
                time=moved[0],
                asset="AAA",
                amount="3",
                sourceAccount="SPOT",
                status="SUCCESS",
            )
            fake.add("earn-flexible-subscriptions", row)

    def later(asked: int) -> None:
        if asked == 0:
            clock.ms += 10_000

    fake.when_asked("spot-account", subscribe)
    fake.when_asked("user-asset", later)
    connection = await binance_connection(run, config=later_config())

    after = await run_binance_sync(session, connection.id, fake.handler)

    assert after.status == "healthy", after.last_error
    assert len(fake.calls("spot-account")) == len(fake.calls("wallet-balance")) == 1
    assert fake.calls("wallet-balance")[0].params["needBalanceDetail"] == "true"
    for label in ("funding-asset", "user-asset"):
        assert fake.calls(label)[0].params["needBtcValuation"] == "true"
    found = {(w, s): (q, at) for w, s, q, at in await _snapshots(session, connection.id)}
    spot_quantity, spot_as_of = found[("Binance Spot", "AAA")]
    earn_quantity, earn_as_of = found[("Binance Earn", "AAA")]
    assert (spot_quantity, earn_quantity) == (10, 3)
    assert spot_as_of < _at(moved[0]) < earn_as_of
    # The subscription is after Spot's cutoff, so it is added to Spot's snapshot; it is before
    # Earn's, so Earn's snapshot already holds it.
    day = earn_as_of.date()
    assert await _balance(session, connection.id, "Spot", "AAA", day) == 7
    assert await _balance(session, connection.id, "Earn", "AAA", day) == 3
    assert await open_differences(session, connection.id) == []


async def test_read_still_ambiguous_after_three_rounds_writes_no_snapshot(
    session, run, fake: FakeBinance, connector, clock: Clock
) -> None:
    fake.set_rows("spot-account", _spot(("AAA", "10", "0")))
    fake.set_rows("funding-asset", [_funding("BBB", "1", "0", "0", "0")])
    connection = await binance_connection(run, config=later_config())
    await run_binance_sync(session, connection.id, fake.handler)
    [first] = [s for s in await _snapshots(session, connection.id) if s[0] == "Binance Spot"]

    def busy(asked: int) -> None:
        # A deposit inside every Spot read, and none near the other reads (each is read 5 s
        # or more away from it).
        clock.ms += 5_000
        fake.add("deposits", _deposit(100 + asked, clock.ms, "1"))
        fake.set_rows("spot-account", _spot(("AAA", str(11 + asked), "0")))
        clock.ms += 5_000

    fake.when_asked("spot-account", busy)
    fake.requests.clear()
    clock.ms += 4 * HOUR_MS
    await run_binance_sync(session, connection.id, fake.handler)

    assert len(fake.calls("spot-account")) == 4
    # The unclear read's report is still stored typed; only its snapshot is held back.
    reported = await session.scalar(
        text("select max(snapshot_at) from finance.binance_spot_account where connection_id = :c"),
        {"c": connection.id},
    )
    assert reported > first[3]
    snapshots = await _snapshots(session, connection.id)
    assert [s for s in snapshots if s[0] == "Binance Spot"] == [first]
    # The other wallets are cut as usual.
    assert len([s for s in snapshots if s[0] == "Binance Funding"]) == 2
    # The previous snapshot plus the four deposits after it: what Spot last said it held.
    assert await _balance(session, connection.id, "Spot", "AAA", _at(clock.ms).date()) == 14

    fake.when_asked("spot-account", None)
    clock.ms += 4 * HOUR_MS
    await run_binance_sync(session, connection.id, fake.handler)

    spot = [s for s in await _snapshots(session, connection.id) if s[0] == "Binance Spot"]
    assert len(spot) == 2
    assert spot[-1][3] > first[3]


async def test_second_precision_times_use_the_margin(
    session, run, fake: FakeBinance, clock: Clock
) -> None:
    connection = await binance_connection(run, config=later_config())
    clock.ms += 500  # a read that starts half a second into its second
    async with fake.http() as http:
        ctx = await context(session, connection.id, http)
        client = client_for(clock, http)
        await client.sync_clock()
        reads = await read_balances(client, ["spot-account"])
        read = reads["spot-account"]
        before = read.read_from.replace(microsecond=0) - timedelta(seconds=1)
        shown = before.strftime("%Y-%m-%d %H:%M:%S")
        fake.add("withdrawals", build_row("withdrawals", applyTime=shown, completeTime=shown))
        await walk(ctx, client, "withdrawals")
        # The read made again starts more than 2 s after the withdrawal: one round settles it.
        clock.ms += 1_000

        settled = await settle_reads(ctx, client, reads)

    # The withdrawal is 1.5 s before the read, inside the 2 s margin.
    assert read.read_from - before == timedelta(milliseconds=1500)
    assert ambiguous(read, [before])
    assert len(fake.calls("spot-account")) == 2
    again = settled["spot-account"]
    assert again is not None and again.read_from > read.read_from and not again.unclear


async def test_a_record_a_round_finds_inside_another_read_has_it_read_again(
    session, run, fake: FakeBinance, clock: Clock
) -> None:
    connection = await binance_connection(run, config=later_config())
    async with fake.http() as http:
        ctx = await context(session, connection.id, http)
        client = client_for(clock, http)
        await client.sync_clock()
        start = clock.ms
        spot = _read("spot-account", _spot(("AAA", "1", "0")), start + 1_000)
        funding = _read("funding-asset", [_funding("BBB", "1", "0", "0", "0")], start + 10_000)
        # Inside the Spot read only: Spot is made again, Funding is not, at first.
        fake.add("deposits", _deposit(0, start + 1_100, "1"))
        clock.ms = start + 20_000
        await walk(ctx, client, "deposits")

        def credited(asked: int) -> None:
            if asked == 0:
                # Found by the round's walk, inside the original Funding read.
                fake.add("deposits", _deposit(1, start + 10_100, "1"))

        fake.when_asked("spot-account", credited)
        settled = await settle_reads(ctx, client, {"spot-account": spot, "funding-asset": funding})

    assert len(fake.calls("spot-account")) == 1
    assert len(fake.calls("funding-asset")) == 1
    again = settled["funding-asset"]
    assert again is not None and again.read_from > funding.read_to and not again.unclear


def test_ambiguous_is_the_bracket_widened_by_two_seconds() -> None:
    read = Read("spot-account", {}, _at(NOW_MS), _at(NOW_MS + 300))
    assert ambiguous(read, [_at(NOW_MS - 2_000)])
    assert ambiguous(read, [_at(NOW_MS + 2_300)])
    assert not ambiguous(read, [_at(NOW_MS - 2_001), _at(NOW_MS + 2_301)])
    assert not ambiguous(read, [])


# Each wallet's balance from its source.


async def test_ld_assets_never_counted(session, run) -> None:
    connection = await binance_connection(run)
    reads: dict[str, Read | None] = {
        "spot-account": _read("spot-account", _spot(("AAA", "1", "0"), ("LDSTB", "2", "0"))),
        "wallet-balance": _read("wallet-balance", [_wallet("Earn", ("STB", "2"))]),
    }

    await _report(run, connection, reads)

    found = [(w, s, q) for w, s, q, _ in await _snapshots(session, connection.id)]
    assert sorted(found) == [("Binance Earn", "STB", 2), ("Binance Spot", "AAA", 1)]
    assert not await session.scalar(
        text("select exists (select 1 from finance.instruments where symbol = 'LDSTB')")
    )


async def test_spot_is_free_plus_locked(session, run) -> None:
    connection = await binance_connection(run)
    read = _read("spot-account", _spot(("AAA", "1.25", "0.5")))

    written = await _report(run, connection, {"spot-account": read})

    [(wallet, symbol, quantity, as_of)] = await _snapshots(session, connection.id)
    assert (wallet, symbol, quantity) == ("Binance Spot", "AAA", Decimal("1.75"))
    assert as_of == _at(NOW_MS)
    assert list(written.values()) == [Decimal("1.75")]
    assert [wallet for wallet, _ in written] == ["spot"]


async def test_funding_sums_its_four_fields(session, run) -> None:
    connection = await binance_connection(run)
    read = _read("funding-asset", [_funding("AAA", "1", "2", "3", "4")])

    await _report(run, connection, {"funding-asset": read})

    [(wallet, symbol, quantity, _)] = await _snapshots(session, connection.id)
    assert (wallet, symbol, quantity) == ("Binance Funding", "AAA", 10)


async def test_asset_that_disappears_gets_a_zero_snapshot(session, run) -> None:
    connection = await binance_connection(run)
    both = _spot(("AAA", "1", "0"), ("BBB", "2", "0"))
    await _report(run, connection, {"spot-account": _read("spot-account", both, NOW_MS)})
    only = _spot(("AAA", "1", "0"))
    second = NOW_MS + HOUR_MS
    await _report(run, connection, {"spot-account": _read("spot-account", only, second)})
    third = second + HOUR_MS
    await _report(run, connection, {"spot-account": _read("spot-account", only, third)})

    bbb = [(q, at) for _, s, q, at in await _snapshots(session, connection.id) if s == "BBB"]
    assert bbb == [(2, _at(NOW_MS)), (0, _at(second))]


async def test_zero_balances_are_not_written(session, run) -> None:
    connection = await binance_connection(run)
    read = _read("spot-account", _spot(("AAA", "0", "0"), ("BBB", "1", "0")))
    wallets = _read("wallet-balance", [_wallet("Cross Margin"), _wallet("Earn", ("STB", "0"))])

    await _report(run, connection, {"spot-account": read, "wallet-balance": wallets})

    found = [(w, s, q) for w, s, q, _ in await _snapshots(session, connection.id)]
    assert found == [("Binance Spot", "BBB", 1)]
    names = await session.scalars(
        text("select name from finance.accounts where source_connection_id = :c"),
        {"c": connection.id},
    )
    assert list(names) == ["Binance Spot"]


async def test_other_wallets_come_from_the_wallet_list_until_they_have_their_own(
    session, run
) -> None:
    connection = await binance_connection(run)
    listed = [
        _wallet("Spot", ("AAA", "9")),
        _wallet("Funding", ("AAA", "9")),
        _wallet("Cross Margin", ("BBB", "2")),
    ]
    reads: dict[str, Read | None] = {
        "spot-account": _read("spot-account", _spot(("AAA", "1", "0"))),
        "funding-asset": _read("funding-asset", [_funding("AAA", "2", "0", "0", "0")]),
        "wallet-balance": _read("wallet-balance", listed),
    }

    await _report(run, connection, reads)

    found = sorted((w, s, q) for w, s, q, _ in await _snapshots(session, connection.id))
    assert found == [
        ("Binance Cross Margin", "BBB", 2),
        ("Binance Funding", "AAA", 2),
        ("Binance Spot", "AAA", 1),
    ]


async def test_a_wallet_whose_read_is_unclear_is_skipped(session, run) -> None:
    connection = await binance_connection(run)
    reads: dict[str, Read | None] = {
        "spot-account": None,
        "funding-asset": _read("funding-asset", [_funding("AAA", "2", "0", "0", "0")]),
    }

    written = await _report(run, connection, reads)

    assert [w for w, _ in written] == ["funding"]
    assert [w for w, *_ in await _snapshots(session, connection.id)] == ["Binance Funding"]


async def test_unknown_wallet_is_flagged_and_counted(session, run) -> None:
    connection = await binance_connection(run)
    flags: list[str] = []
    wallets = _read("wallet-balance", [_wallet("Lucky Box", ("AAA", "4"))])

    await _report(run, connection, {"wallet-balance": wallets}, flags)

    assert flags == ["unknown wallet Lucky Box"]
    [(wallet, symbol, quantity, _)] = await _snapshots(session, connection.id)
    assert (wallet, symbol, quantity) == ("Binance Lucky Box", "AAA", 4)


async def test_wallet_disagreement_is_flagged(session, run) -> None:
    connection = await binance_connection(run)
    flags: list[str] = []
    reads: dict[str, Read | None] = {
        "funding-asset": _read("funding-asset", []),
        "wallet-balance": _read("wallet-balance", [_wallet("Funding", ("AAA", "1"))]),
    }

    await _report(run, connection, reads, flags)

    assert flags == ["wallet Funding disagrees with its own report"]
    # Funding's balance is its own report's, not the wallet list's.
    assert await _snapshots(session, connection.id) == []


def test_wallet_flags() -> None:
    listed = [
        _wallet("Spot", ("AAA", "1")),
        _wallet("Funding"),
        _wallet("Earn", ("AAA", "1")),
        _wallet("Lucky Box"),
    ]
    assert wallet_flags(listed, {"spot": True, "funding": False}) == ["unknown wallet Lucky Box"]
    assert wallet_flags(listed, {"spot": False, "funding": True}) == [
        "wallet Spot disagrees with its own report",
        "wallet Funding disagrees with its own report",
        "unknown wallet Lucky Box",
    ]
    # A valuation of 0 with a quantity listed is not zero.
    dust = [_wallet("Spot", ("AAA", "0.00000001"), balance="0")]
    assert wallet_flags(dust, {"spot": True}) == []
    # A wallet counted from the list whose balance is not zero must list what it holds.
    assert wallet_flags([_wallet("Earn", balance="2")], {}) == [
        "wallet Earn has a balance but lists no asset"
    ]
    assert wallet_flags([_wallet("Earn", ("AAA", "1"), balance="2")], {}) == []


async def test_a_wallet_with_a_balance_and_no_assets_listed_is_flagged(session, run) -> None:
    connection = await binance_connection(run)
    flags: list[str] = []
    listed = [_wallet("Trading Bots", balance="0.5"), _wallet("Copy Trading", balance="0")]

    await _report(run, connection, {"wallet-balance": _read("wallet-balance", listed)}, flags)

    assert flags == ["wallet Trading Bots has a balance but lists no asset"]
    assert await _snapshots(session, connection.id) == []


# The daily snapshots of the first sync.


def _daily(kind: str, at_ms: int, *balances: tuple[str, str]) -> dict[str, Any]:
    row = build_row("snapshot", type=kind, updateTime=at_ms)
    data = dict(row["data"])  # pyright: ignore[reportArgumentType,reportCallIssue]
    data["balances"] = [{"asset": code, "free": free, "locked": "0"} for code, free in balances]
    return {**row, "data": data}


async def test_daily_snapshots_backfilled_at_their_own_time(session, run) -> None:
    connection = await binance_connection(run)
    day_one = NOW_MS - 2 * 24 * HOUR_MS - 1_000  # 23:59:59 UTC
    day_two = day_one + 24 * HOUR_MS
    rows = [
        _daily("spot", day_two, ("AAA", "3")),
        _daily("spot", day_one, ("AAA", "2"), ("BBB", "1"), ("LDAAA", "5")),
        _daily("margin", day_two, ("CCC", "7")),
    ]

    written = await run(backfill_daily_snapshots, connection, rows, flag=lambda _: None)

    found = [(w, s, q, at) for w, s, q, at in await _snapshots(session, connection.id)]
    assert found == [
        ("Binance Spot", "AAA", 2, _at(day_one)),
        ("Binance Spot", "BBB", 1, _at(day_one)),
        ("Binance Spot", "AAA", 3, _at(day_two)),
        ("Binance Spot", "BBB", 0, _at(day_two)),
    ]
    assert written == 4
    # Once written, the same rows write nothing.
    assert await run(backfill_daily_snapshots, connection, rows, flag=lambda _: None) == 0


async def _spot_reports(session, connection_id: UUID) -> list[datetime]:
    found = await session.scalars(
        text(
            "select r.as_of from finance.binance_account_reports r"
            " join finance.accounts a on a.id = r.account_id"
            " where r.connection_id = :c and a.name = 'Binance Spot' order by r.as_of"
        ),
        {"c": connection_id},
    )
    return list(found)


async def test_a_day_is_reported_only_when_its_snapshots_are_written_in_full(
    session, run, monkeypatch: pytest.MonkeyPatch
) -> None:
    connection = await binance_connection(run)
    day = 24 * HOUR_MS
    one, two, three = (NOW_MS - n * day - 1_000 for n in (3, 2, 1))
    rows = [
        # An LD mirror and a zero balance do not keep a day from being written.
        _daily("spot", one, ("AAA", "2"), ("LDAAA", "5"), ("CCC", "0")),
        # BBB cannot be resolved: nothing of the day is written, not even AAA.
        _daily("spot", two, ("AAA", "3"), ("BBB", "1")),
        _daily("spot", three, ("AAA", "4")),
    ]
    real = balances.asset

    async def unresolved(ctx, connection_id, code, *, flag=None):
        return None if code == "BBB" else await real(ctx, connection_id, code, flag=flag)

    monkeypatch.setattr(balances, "asset", unresolved)
    flags: list[str] = []

    await run(backfill_daily_snapshots, connection, rows, flag=flags.append)

    written = {at for _, _, _, at in await _snapshots(session, connection.id)}
    assert written == {_at(one), _at(three)}
    assert await _spot_reports(session, connection.id) == [_at(one), _at(three)]
    assert [(s, q) for _, s, q, at in await _snapshots(session, connection.id)] == [
        ("AAA", 2),
        ("AAA", 4),
    ]
    assert flags == ["a daily Spot snapshot was not written: BBB has no instrument"]


async def test_days_holding_nothing_are_reported_once_the_records_make_the_account(
    session, run, fake: FakeBinance, connector, clock: Clock
) -> None:
    day = 24 * HOUR_MS
    one, two = (NOW_MS - n * day - 1_000 for n in (2, 1))
    fake.add("snapshot", _daily("spot", one, ("AAA", "0")), _daily("spot", two, ("BBB", "0")))
    fake.add("deposits", _deposit(0, NOW_MS - HOUR_MS, "2"))
    fake.set_rows("spot-account", _spot(("AAA", "2", "0")))
    config = later_config()
    del config["first_sync_done"]
    config["cursors"].pop("snapshot:SPOT")  # pyright: ignore[reportAttributeAccessIssue,reportUnknownMemberType]
    connection = await binance_connection(run, config=config)

    after = await run_binance_sync(session, connection.id, fake.handler)

    assert after.status == "healthy", after.last_error
    reports = await _spot_reports(session, connection.id)
    assert reports[:2] == [_at(one), _at(two)] and len(reports) == 3
    assert [at for _, _, _, at in await _snapshots(session, connection.id)] == [reports[2]]


async def test_first_sync_writes_the_daily_snapshots(
    session, run, fake: FakeBinance, connector
) -> None:
    day = NOW_MS - 24 * HOUR_MS - 1_000
    fake.add("snapshot", _daily("spot", day, ("AAA", "2")))
    fake.set_rows("spot-account", _spot(("AAA", "2", "0")))
    config = later_config()
    del config["first_sync_done"]
    config["cursors"].pop("snapshot:SPOT")  # pyright: ignore[reportAttributeAccessIssue,reportUnknownMemberType]
    connection = await binance_connection(run, config=config)

    after = await run_binance_sync(session, connection.id, fake.handler)

    assert after.status == "healthy", after.last_error
    [daily, current] = [(s, q, at) for _, s, q, at in await _snapshots(session, connection.id)]
    assert daily == ("AAA", 2, _at(day))
    assert current[:2] == ("AAA", 2) and current[2] >= _at(NOW_MS)


# The typed report tables.


async def _count(session, table: str, connection_id: UUID) -> int:
    return await session.scalar(
        text(f"select count(*) from finance.{table} where connection_id = :c"),
        {"c": connection_id},
    )


_TABLES = (
    "binance_spot_account",
    "binance_spot_account_balances",
    "binance_wallet_balance",
    "binance_earn_flexible_position",
    "binance_earn_flexible_position_responses",
)


def _reads(at_ms: int) -> dict[str, Read | None]:
    position = build_row("earn-flexible-position", asset="STB", totalAmount="2")
    return {
        "spot-account": _read("spot-account", _spot(("AAA", "1", "0")), at_ms),
        "wallet-balance": _read("wallet-balance", [_wallet("Earn", ("STB", "2"))], at_ms + 300),
        "earn-flexible-position": _read("earn-flexible-position", [position], at_ms + 600),
    }


async def test_identical_report_writes_nothing_new_but_a_snapshot(session, run) -> None:
    connection = await binance_connection(run)
    await _report(run, connection, _reads(NOW_MS))
    counts = [await _count(session, table, connection.id) for table in _TABLES]
    assert counts == [1, 1, 1, 1, 1]

    later = NOW_MS + HOUR_MS
    await _report(run, connection, _reads(later))

    assert [await _count(session, table, connection.id) for table in _TABLES] == counts
    spot = [at for w, _, _, at in await _snapshots(session, connection.id) if w == "Binance Spot"]
    assert spot == [_at(NOW_MS), _at(later)]


async def test_a_changed_report_is_stored_with_its_read(session, run) -> None:
    connection = await binance_connection(run)
    await _report(run, connection, _reads(NOW_MS))
    changed = _reads(NOW_MS + HOUR_MS)
    changed["spot-account"] = _read("spot-account", _spot(("AAA", "2", "0")), NOW_MS + HOUR_MS)

    await _report(run, connection, changed)

    rows = (
        await session.execute(
            text(
                "select snapshot_at, read_to from finance.binance_spot_account"
                " where connection_id = :c order by snapshot_at"
            ),
            {"c": connection.id},
        )
    ).all()
    assert [(r.snapshot_at, r.read_to) for r in rows] == [
        (_at(NOW_MS), _at(NOW_MS + 200)),
        (_at(NOW_MS + HOUR_MS), _at(NOW_MS + HOUR_MS + 200)),
    ]
    # The position's answer is unchanged: its rows and its response stay as they were.
    assert await _count(session, "binance_earn_flexible_position_responses", connection.id) == 1
    response = await session.scalar(
        text("select total from finance.binance_earn_flexible_position_responses")
    )
    assert response == 1


async def test_a_sync_stores_the_reference_reports_once(
    session, run, fake: FakeBinance, connector, clock: Clock
) -> None:
    fake.add("coin-config", build_row("coin-config", coin="EUR", isLegalMoney=True))
    fake.set_rows("spot-account", _spot(("EUR", "5", "0")))
    connection = await binance_connection(run, config=later_config())

    await run_binance_sync(session, connection.id, fake.handler)
    clock.ms += 4 * HOUR_MS
    await run_binance_sync(session, connection.id, fake.handler)

    assert len(fake.calls("coin-config")) == len(fake.calls("exchange-info")) == 2
    assert len(fake.calls("asset-detail")) == 2
    assert await _count(session, "binance_coin_config", connection.id) == 1
    assert await _count(session, "binance_exchange_info", connection.id) == 1
    assert await _count(session, "binance_asset_detail", connection.id) == 1
    # coin-config was stored before the asset was resolved: EUR is the currency.
    kinds = await session.scalars(
        text(
            "select i.kind from finance.balance_snapshots s"
            " join finance.instruments i on i.id = s.instrument_id"
            " join finance.accounts a on a.id = s.account_id where a.source_connection_id = :c"
        ),
        {"c": connection.id},
    )
    assert set(kinds) == {"fiat"}


async def test_an_unclear_read_stores_its_report_but_no_snapshot(session, run) -> None:
    connection = await binance_connection(run)
    read = _read("spot-account", _spot(("AAA", "1", "0")))
    unclear = Read(read.label, read.body, read.read_from, read.read_to, unclear=True)

    assert await _report(run, connection, {"spot-account": unclear}) == {}

    assert await _snapshots(session, connection.id) == []
    assert await _count(session, "binance_spot_account_balances", connection.id) == 1


# Unreadable data fails closed, naming the field and never the value.


async def _settle_after(session, run, fake: FakeBinance, clock: Clock, label: str, row: dict):
    connection = await binance_connection(run, config=later_config())
    async with fake.http() as http:
        ctx = await context(session, connection.id, http)
        client = client_for(clock, http)
        reads = await read_balances(client, ["spot-account"])
        fake.add(label, row)
        await walk(ctx, client, label)
        return await settle_reads(ctx, client, reads)


@pytest.mark.parametrize(
    ("label", "field", "overrides"),
    [
        ("withdrawals", "[].completeTime", {"completeTime": "23/11/2026 10:00"}),
        ("deposits", "[].completeTime", {"completeTime": 10**20}),
    ],
)
async def test_an_unreadable_record_time_fails_naming_the_field(
    session, run, fake: FakeBinance, clock: Clock, label: str, field: str, overrides: dict
) -> None:
    at = NOW_MS - HOUR_MS
    times = {"applyTime": _text_time(at)} if label == "withdrawals" else {"insertTime": at}
    row = build_row(label, 0, **times, **overrides)

    with pytest.raises(BinanceDataError) as caught:
        await _settle_after(session, run, fake, clock, label, row)

    assert (caught.value.label, caught.value.field) == (label, field)
    assert str(next(iter(overrides.values()))) not in str(caught.value)


async def test_an_absent_optional_time_is_no_time(session, run, fake: FakeBinance, clock) -> None:
    row = build_row("deposits", 0, insertTime=NOW_MS - HOUR_MS, completeTime=None)

    settled = await _settle_after(session, run, fake, clock, "deposits", row)

    assert settled["spot-account"] is not None and not settled["spot-account"].unclear


def _text_time(ms: int) -> str:
    return _at(ms).strftime("%Y-%m-%d %H:%M:%S")


async def test_a_spot_amount_that_is_no_number_fails(run) -> None:
    connection = await binance_connection(run)
    read = _read("spot-account", [build_row("spot-account", asset="AAA", free=True, locked="0")])

    with pytest.raises(BinanceDataError) as caught:
        await _report(run, connection, {"spot-account": read})

    assert caught.value.label == "spot-account"


async def test_an_unknown_wallet_name_is_stripped(session, run) -> None:
    connection = await binance_connection(run)
    flags: list[str] = []
    wallets = _read("wallet-balance", [_wallet("  Lucky Box  ", ("AAA", "4"))])

    await _report(run, connection, {"wallet-balance": wallets}, flags)

    assert flags == ["unknown wallet Lucky Box"]
    assert [w for w, *_ in await _snapshots(session, connection.id)] == ["Binance Lucky Box"]


@pytest.mark.parametrize("name", ["x" * 65, "Lucky\nBox", "Lucky\x00Box", "   "])
async def test_a_wallet_name_that_cannot_name_an_account_fails(session, run, name: str) -> None:
    connection = await binance_connection(run)
    wallets = _read("wallet-balance", [_wallet(name, ("AAA", "4"))])

    with pytest.raises(BinanceDataError) as caught:
        await _report(run, connection, {"wallet-balance": wallets})

    assert (caught.value.label, caught.value.field) == ("wallet-balance", "[].walletName")
    assert not name.strip() or name.strip() not in str(caught.value)


# An answer that does not state a wallet's holdings is never read as an empty report.


def _without(body: Any, field: str, missing: str) -> Any:
    """`body` with `field` left out (`missing == "absent"`) or null."""
    changed = dict(body)
    if missing == "absent":
        del changed[field]
    else:
        changed[field] = None
    return changed


async def _reports_of(session, connection_id: UUID, wallet: str) -> list[datetime]:
    found = await session.scalars(
        text(
            "select r.as_of from finance.binance_account_reports r"
            " join finance.accounts a on a.id = r.account_id"
            " where r.connection_id = :c and a.name = :w order by r.as_of"
        ),
        {"c": connection_id, "w": f"Binance {wallet}"},
    )
    return list(found)


@pytest.mark.parametrize("missing", ["absent", "null"])
async def test_a_spot_answer_without_its_balances_fails_the_sync(
    session, run, fake: FakeBinance, connector, clock: Clock, missing: str
) -> None:
    fake.set_rows("spot-account", _spot(("AAA", "10", "0")))
    connection = await binance_connection(run, config=later_config())
    await run_binance_sync(session, connection.id, fake.handler)
    before = await _snapshots(session, connection.id)
    reports = await _reports_of(session, connection.id, "Spot")
    typed = await _count(session, "binance_spot_account", connection.id)

    def handler(request):
        answer = fake.handler(request)
        if request.url.path != "/api/v3/account":
            return answer
        body = _without(json.loads(answer.content), "balances", missing)
        return httpx.Response(200, content=json.dumps(body).encode())

    clock.ms += 4 * HOUR_MS
    after = await run_binance_sync(session, connection.id, handler)

    assert after.status == "failing"
    assert "spot-account" in (after.last_error or "") and "balances" in (after.last_error or "")
    assert await _snapshots(session, connection.id) == before
    assert await _reports_of(session, connection.id, "Spot") == reports
    assert await _count(session, "binance_spot_account", connection.id) == typed
    day = _at(clock.ms).date()
    assert await _balance(session, connection.id, "Spot", "AAA", day) == 10


async def test_an_empty_spot_list_is_an_empty_wallet(session, run) -> None:
    connection = await binance_connection(run)
    await _report(
        run, connection, {"spot-account": _read("spot-account", _spot(("AAA", "1", "0")))}
    )
    later = NOW_MS + HOUR_MS

    await _report(run, connection, {"spot-account": _read("spot-account", [], later)})

    assert [(s, q, at) for _, s, q, at in await _snapshots(session, connection.id)] == [
        ("AAA", 1, _at(NOW_MS)),
        ("AAA", 0, _at(later)),
    ]


@pytest.mark.parametrize("missing", ["absent", "null"])
async def test_a_funding_or_wallet_list_answer_that_is_no_list_fails(
    session, run, missing: str
) -> None:
    connection = await binance_connection(run)
    for label in ("funding-asset", "wallet-balance"):
        read = Read(label, None if missing == "null" else {}, _at(NOW_MS), _at(NOW_MS + 200))

        with pytest.raises(BinanceDataError) as caught:
            await _report(run, connection, {label: read})

        assert caught.value.label == label
    assert await _snapshots(session, connection.id) == []


@pytest.mark.parametrize("missing", ["absent", "null", "empty"])
async def test_a_listed_wallet_with_a_balance_and_no_detail_keeps_its_balance(
    session, run, missing: str
) -> None:
    connection = await binance_connection(run)
    first = _read("wallet-balance", [_wallet("Earn", ("AAA", "3"))])
    await _report(run, connection, {"wallet-balance": first})
    reports = await _reports_of(session, connection.id, "Earn")
    row = _wallet("Earn", balance="2")
    if missing != "empty":
        row = _without(row, "assetBalances", missing)
    later = _read("wallet-balance", [row], NOW_MS + HOUR_MS)
    flags: list[str] = []

    await _report(run, connection, {"wallet-balance": later}, flags)

    # A balance with no detail, or no detail at all: left out (the list is read again next
    # sync) and flagged; its earlier balance stands and no report time is written.
    if missing == "empty":
        assert flags == ["wallet Earn has a balance but lists no asset"]
    else:
        assert flags == ["wallet Earn lists no asset detail"]
    assert [(s, q) for _, s, q, _ in await _snapshots(session, connection.id)] == [("AAA", 3)]
    assert await _reports_of(session, connection.id, "Earn") == reports


async def test_a_listed_asset_without_its_quantity_fails_and_writes_nothing(session, run) -> None:
    connection = await binance_connection(run)
    row = {
        **build_row("wallet-balance", walletName="Earn", balance="5"),
        "assetBalances": [{"asset": "AAA"}, {"asset": "BBB", "free": "1"}],
    }

    with pytest.raises(BinanceDataError) as caught:
        await _report(run, connection, {"wallet-balance": _read("wallet-balance", [row])})

    assert (caught.value.label, caught.value.field) == (
        "wallet-balance",
        "[].assetBalances[].free",
    )
    assert await _snapshots(session, connection.id) == []
    assert await _reports_of(session, connection.id, "Earn") == []
    assert await _count(session, "binance_wallet_balance", connection.id) == 0


@pytest.mark.parametrize("name", ["free", "locked", "freeze", "withdrawing"])
async def test_a_listed_asset_must_state_every_quantity_it_sums(session, run, name: str) -> None:
    connection = await binance_connection(run)
    entry = _asset("AAA", "1")
    del entry[name]
    row = build_row("wallet-balance", walletName="Earn", balance="5", assetBalances=[entry])

    with pytest.raises(BinanceDataError) as caught:
        await _report(run, connection, {"wallet-balance": _read("wallet-balance", [row])})

    assert caught.value.field == f"[].assetBalances[].{name}"
    assert await _snapshots(session, connection.id) == []


@pytest.mark.parametrize(
    ("missing", "field"),
    [
        ("absent", "snapshotVos[].data.balances"),
        ("null", "snapshotVos[].data.balances"),
        ("no data", "snapshotVos[].data.balances"),
    ],
)
async def test_a_daily_snapshot_that_does_not_state_its_balances_is_left_out(
    session, run, missing: str, field: str
) -> None:
    # The daily snapshots are fetched once: a day that does not state what Spot held is left
    # out and flagged, never read as an empty day nor failing every later sync.
    connection = await binance_connection(run)
    one = NOW_MS - 24 * HOUR_MS - 1_000
    two = NOW_MS - 1_000
    row = _daily("spot", two, ("AAA", "1"))
    if missing == "no data":
        del row["data"]
    else:
        row["data"] = _without(row["data"], "balances", missing)
    good = _daily("spot", one, ("AAA", "2"))
    flags: list[str] = []

    await run(backfill_daily_snapshots, connection, [good, row], flag=flags.append)

    assert flags == [f"a daily Spot snapshot was not written: snapshot.{field} is unreadable"]
    assert [(s, q, at) for _, s, q, at in await _snapshots(session, connection.id)] == [
        ("AAA", 2, _at(one))
    ]
    assert await _reports_of(session, connection.id, "Spot") == [_at(one)]


async def test_a_first_sync_with_a_daily_row_without_balances_completes(
    session, run, fake: FakeBinance, connector, clock: Clock
) -> None:
    # Days that type correctly but do not state what Spot held: one without its list, one
    # without `data` at all.
    one, two, three = (NOW_MS - n * 24 * HOUR_MS - 1_000 for n in (3, 2, 1))
    broken = _daily("spot", two, ("AAA", "2"))
    del broken["data"]["balances"]
    no_data = _daily("spot", three, ("AAA", "2"))
    del no_data["data"]
    fake.add("snapshot", _daily("spot", one, ("AAA", "2")), broken, no_data)
    fake.set_rows("spot-account", _spot(("AAA", "2", "0")))
    config = later_config()
    del config["first_sync_done"]
    config["cursors"].pop("snapshot:SPOT")  # pyright: ignore[reportAttributeAccessIssue,reportUnknownMemberType]
    connection = await binance_connection(run, config=config)

    after = await run_binance_sync(session, connection.id, fake.handler)

    assert after.status == "healthy", after.last_error
    assert after.config.get("first_sync_done") is True
    flag = (
        "a daily Spot snapshot was not written: snapshot.snapshotVos[].data.balances is unreadable"
    )
    assert flag in after.config["flags"]  # pyright: ignore[reportOperatorIssue]
    reports = await _reports_of(session, connection.id, "Spot")
    assert _at(one) in reports and _at(two) not in reports and _at(three) not in reports

    clock.ms += 4 * HOUR_MS
    again = await run_binance_sync(session, connection.id, fake.handler)

    assert again.status == "healthy", again.last_error
    assert again.config["flags"] == after.config["flags"]


async def test_a_daily_snapshot_with_an_empty_list_is_an_empty_day(session, run) -> None:
    connection = await binance_connection(run)
    one = NOW_MS - 2 * 24 * HOUR_MS - 1_000
    two = one + 24 * HOUR_MS
    rows = [_daily("spot", one, ("AAA", "1")), _daily("spot", two)]

    await run(backfill_daily_snapshots, connection, rows, flag=lambda _: None)

    assert [(s, q, at) for _, s, q, at in await _snapshots(session, connection.id)] == [
        ("AAA", 1, _at(one)),
        ("AAA", 0, _at(two)),
    ]


@pytest.mark.parametrize("name", ["freeze", "withdrawing"])
async def test_a_summed_quantity_left_out_of_a_funding_entry_fails(session, run, name: str) -> None:
    # Every quantity summed for a wallet must be stated, whatever the catalogue's flag.
    connection = await binance_connection(run)
    entry = {"asset": "AAA", "free": "0", "locked": "0", "freeze": "5", "withdrawing": "0"}
    del entry[name]
    read = Read("funding-asset", [entry], _at(NOW_MS), _at(NOW_MS + 200))

    with pytest.raises(BinanceDataError) as caught:
        await _report(run, connection, {"funding-asset": read})

    assert (caught.value.label, caught.value.field) == ("funding-asset", f"[].{name}")
    assert await _snapshots(session, connection.id) == []
    assert await _count(session, "binance_funding_asset", connection.id) == 0


async def test_a_wallet_holding_only_a_currency_lararium_lacks_is_not_misread(session, run) -> None:
    from lararium.modules.finance.binance.parse import parse
    from lararium.modules.finance.binance.tables import records, store

    connection = await binance_connection(run)
    row = build_row("coin-config", coin="XFA", isLegalMoney=True)
    parsed = parse("coin-config", build_response("coin-config", [row]))
    key = {"connection_id": connection.id, "snapshot_at": _at(NOW_MS)}
    for record in records("coin-config", parsed):
        await run(store, "coin-config", key, record, {"read_to": _at(NOW_MS)})
    flags: list[str] = []
    reads: dict[str, Read | None] = {
        "spot-account": _read("spot-account", _spot(("XFA", "5", "0"))),
        "wallet-balance": _read(
            "wallet-balance", [_wallet("Spot", ("XFA", "5")), _wallet("Earn", ("XFA", "3"))]
        ),
    }

    assert await _report(run, connection, reads, flags) == {}

    # Held but not counted: no snapshot, and neither "disagrees" nor "lists no asset".
    assert flags == ["XFA is a currency Lararium does not have: it is not counted"]
    assert await _snapshots(session, connection.id) == []
