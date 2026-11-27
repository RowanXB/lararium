"""The futures wallets in the ledger (design 5.5, 5.6, 5.7, 5.11): USDⓈ-M and COIN-M are
reported from each account's `assets[]` as wallet balance plus unrealised profit, and reconciled
(and given their openings) on the wallet balance alone; every income row but a transfer is
income against Unassigned, a commission, funding fee or insurance clearance paid is a fee, a
transfer is a movement the universal transfer history may list first (rule 2), and trades and
positions are typed only. Each host's own exchange information sets its request limit."""

from decimal import Decimal

import httpx
import pytest
from sqlalchemy import text

from lararium.core.commands import command_context
from lararium.modules.finance.binance.balances import (
    FUTURES_SOURCES,
    OWN_SOURCES,
    Read,
    backfill_daily_snapshots,
    report_balances,
)
from lararium.modules.finance.binance.catalogue import ENDPOINTS
from lararium.modules.finance.binance.fetch import (
    BALANCE_LABELS,
    CURSORS_KEY,
    FUTURES_INFO,
    FUTURES_TRADES,
    HISTORY_LABELS,
    PHASE_B_BUILT,
    TRADE_LABELS,
    to_json,
)
from lararium.modules.finance.binance.normalize import HANDLERS, MOVEMENT_LISTINGS, Run
from lararium.modules.finance.binance.opening import set_openings
from lararium.modules.finance.binance.parse import BinanceDataError
from lararium.modules.finance.binance.reconcile import check
from lararium.modules.finance.binance.records.futures import (
    INCOME_FEES,
    TRANSFER,
    income_types,
)
from lararium.modules.finance.binance.sync import limits_unread_flag, read_limits

from .fixtures import BASE_TIME_MS, DAY_MS, HOUR_MS, FakeBinance, build_response, build_row
from .normalizing import (
    assert_balanced_and_quiet,
    at,
    holdings,
    ingest,
    keys,
    normalize_rows,
    plain,
    postings,
    typed,
)
from .syncing import (
    CONNECTOR,
    Clock,
    RecordingNotifier,
    binance_connection,
    client_for,
    context,
    later_config,
    run_binance_sync,
)

T = BASE_TIME_MS
UM = "um-income"
CM = "cm-income"
UM_ACCOUNT = "um-account"
CM_ACCOUNT = "cm-account"
UM_NAME = "Binance USDⓈ-M Futures"
CM_NAME = "Binance COIN-M Futures"
SPOT = "Binance Spot"
UNIVERSAL = "universal-transfer"
_CHANGED = "changed by something no record explains"
_RECORD = {UM: "um_income", CM: "cm_income"}
_ACCOUNT_OF = {UM: UM_ACCOUNT, CM: CM_ACCOUNT}
_NAME_OF = {UM: UM_NAME, CM: CM_NAME}


def _asset(label: str, code: str, wallet: str, profit: str | None = "0") -> dict:
    return build_row(label, asset=code, walletBalance=wallet, unrealizedProfit=profit)


def _read(label: str, rows: list, at_ms: int) -> Read:
    body = build_response(label, rows)
    if isinstance(body, dict) and "positions" in body:
        # No position unless a test lists one.
        body = {**body, "positions": []}
    return Read(label, to_json(body), at(at_ms), at(at_ms + 200))


def _income(label: str, seq: int, kind: str, amount: str, **fields) -> dict:
    values = {
        "incomeType": kind,
        "income": amount,
        "asset": "STB",
        "symbol": "" if kind == TRANSFER else "AAASTB",
        "time": T + seq * HOUR_MS,
    }
    return build_row(label, seq, **(values | fields))


def _key(label: str, row: dict) -> str:
    return f"{_RECORD[label]}:{row['incomeType']}:{row['tranId']}"


async def _snapshots(session, connection_id) -> list[tuple]:
    rows = await session.execute(
        text(
            "select a.name, i.symbol, s.quantity, s.as_of from finance.balance_snapshots s"
            " join finance.accounts a on a.id = s.account_id"
            " join finance.instruments i on i.id = s.instrument_id"
            " where a.source_connection_id = :c and s.kind = 'reported'"
            " order by s.as_of, a.name, i.symbol"
        ),
        {"c": connection_id},
    )
    return [tuple(r) for r in rows]


async def _differences(session, connection_id) -> list[tuple]:
    rows = await session.execute(
        text(
            "select a.name, i.symbol from finance.binance_reconciliation_differences d"
            " join finance.accounts a on a.id = d.account_id"
            " join finance.instruments i on i.id = d.instrument_id"
            " where a.source_connection_id = :c and d.closed_at is null"
            " order by a.name, i.symbol"
        ),
        {"c": connection_id},
    )
    return [tuple(r) for r in rows]


async def _never_transfer(session, key: str) -> bool:
    return await session.scalar(
        text("select never_transfer from finance.transactions where external_id = :e"), {"e": key}
    )


def test_the_family_is_walked_and_read() -> None:
    histories = {UM, CM}
    reports = {"um-balance", UM_ACCOUNT, "um-positions", "cm-balance", CM_ACCOUNT, "cm-positions"}
    assert histories | reports <= set(PHASE_B_BUILT)
    assert histories <= set(HISTORY_LABELS)
    assert reports <= set(BALANCE_LABELS)
    # Trades are walked per symbol the income rows name; the exchange information is read first.
    assert FUTURES_TRADES == {"um-trades": UM, "cm-trades": CM}
    for label in [*FUTURES_TRADES, *FUTURES_INFO]:
        assert label not in HISTORY_LABELS and label not in BALANCE_LABELS
        assert label not in TRADE_LABELS
    assert FUTURES_SOURCES == {"um_futures": UM_ACCOUNT, "cm_futures": CM_ACCOUNT}
    assert set(FUTURES_SOURCES.items()) <= set(OWN_SOURCES.items())
    assert HANDLERS[UM] is HANDLERS[CM]
    assert [listing.label for listing in MOVEMENT_LISTINGS][-2:] == [UM, CM]


# The reported balances.


async def test_reported_balance_includes_unrealised_profit(session, run) -> None:
    connection = await binance_connection(run)
    flags: list[str] = []
    um = [_asset(UM_ACCOUNT, "STB", "10", "2.5"), _asset(UM_ACCOUNT, "AAA", "1", "-0.25")]
    cm = [_asset(CM_ACCOUNT, "BBB", "3", "0.5")]
    listed = [
        build_row("wallet-balance", walletName="USDⓈ-M Futures", balance="1", assetBalances=[]),
        build_row("wallet-balance", walletName="COIN-M Futures", balance="1", assetBalances=[]),
    ]
    reads = {
        UM_ACCOUNT: _read(UM_ACCOUNT, um, T),
        CM_ACCOUNT: _read(CM_ACCOUNT, cm, T + 1_000),
        "wallet-balance": _read("wallet-balance", listed, T + 2_000),
    }

    await run(report_balances, connection, reads, flag=flags.append)

    assert await _snapshots(session, connection.id) == [
        (UM_NAME, "AAA", Decimal("0.75"), at(T)),
        (UM_NAME, "STB", Decimal("12.5"), at(T)),
        (CM_NAME, "BBB", Decimal("3.5"), at(T + 1_000)),
    ]
    # The wallet list agrees with each account's own report, and is not counted again.
    assert flags == []
    rows = await typed(session, "binance_um_account_assets", connection.id)
    assert sorted((r.asset, r.wallet_balance, r.unrealized_profit) for r in rows) == [
        ("AAA", Decimal(1), Decimal("-0.25")),
        ("STB", Decimal(10), Decimal("2.5")),
    ]

    # An unrealised profit the account does not state is never read as 0.
    unstated = [_asset(UM_ACCOUNT, "STB", "10", None)]
    with pytest.raises(BinanceDataError) as caught:
        await run(
            report_balances, connection, {UM_ACCOUNT: _read(UM_ACCOUNT, unstated, T + HOUR_MS)}
        )
    assert (caught.value.label, caught.value.field) == (UM_ACCOUNT, "assets[].unrealizedProfit")


async def test_reconciliation_uses_wallet_balance(session, run) -> None:
    connection = await binance_connection(run)
    first = [_asset(UM_ACCOUNT, "STB", "100", "5")]
    await run(report_balances, connection, {UM_ACCOUNT: _read(UM_ACCOUNT, first, T)})
    rows = [
        _income(UM, 1, "REALIZED_PNL", "3"),
        _income(UM, 2, "COMMISSION", "-0.5"),
        _income(UM, 3, "FUNDING_FEE", "0.25"),
    ]
    await normalize_rows(session, connection.id, (UM, rows))
    # The wallet balance rose by what the records say (2.75); the unrealised profit fell from 5 to
    # -4, so the reported balance fell by 6.25.
    later = [_asset(UM_ACCOUNT, "STB", "102.75", "-4")]
    await run(report_balances, connection, {UM_ACCOUNT: _read(UM_ACCOUNT, later, T + 4 * HOUR_MS)})

    assert [q for _, _, q, _ in await _snapshots(session, connection.id)] == [
        Decimal(105),
        Decimal("98.75"),
    ]
    assert await check(session, connection.id, ()) == []

    # A wallet balance the records do not explain is a difference, of the wallet balance's change.
    unexplained = [_asset(UM_ACCOUNT, "STB", "103.75", "-4")]
    await run(
        report_balances, connection, {UM_ACCOUNT: _read(UM_ACCOUNT, unexplained, T + 5 * HOUR_MS)}
    )
    [difference] = await check(session, connection.id, ())
    assert (difference.reported_change, difference.posted_sum) == (Decimal(1), Decimal(0))
    assert difference.from_at == at(T + 4 * HOUR_MS)


async def test_futures_daily_snapshots_are_usdm_wallet_balances(session, run) -> None:
    connection = await binance_connection(run)
    day = T + DAY_MS
    row = build_row("snapshot", type="futures", updateTime=day)
    data = dict(row["data"])  # pyright: ignore[reportArgumentType,reportCallIssue]
    data["assets"] = [{"asset": "STB", "marginBalance": "99", "walletBalance": "100"}]
    data["position"] = [
        {
            "entryPrice": "1",
            "markPrice": "2",
            "positionAmt": "3",
            "symbol": "AAASTB",
            "unRealizedProfit": "3",
        }
    ]
    rows = [{**row, "data": data}]

    assert await run(backfill_daily_snapshots, connection, rows, flag=lambda _: None) == 1
    assert await _snapshots(session, connection.id) == [(UM_NAME, "STB", Decimal(100), at(day))]


async def test_the_first_live_read_after_the_daily_snapshots_reconciles_on_wallet_balance(
    session, run
) -> None:
    connection = await binance_connection(run)
    day = T + DAY_MS - 1_000
    # A commission before the last daily snapshot, a realised profit after it.
    await normalize_rows(
        session,
        connection.id,
        (UM, [_income(UM, 0, "COMMISSION", "-1"), _income(UM, 30, "REALIZED_PNL", "3")]),
    )
    row = build_row("snapshot", type="futures", updateTime=day)
    data = dict(row["data"])  # pyright: ignore[reportArgumentType,reportCallIssue]
    data["assets"] = [{"asset": "STB", "marginBalance": "100", "walletBalance": "100"}]
    data["position"] = []
    await run(backfill_daily_snapshots, connection, [{**row, "data": data}], flag=lambda _: None)
    # The first live read: the wallet balance is the day's and the profit (103); the open
    # position's unrealised profit (7) is in the report only.
    live = [_asset(UM_ACCOUNT, "STB", "103", "7")]
    await run(report_balances, connection, {UM_ACCOUNT: _read(UM_ACCOUNT, live, T + 2 * DAY_MS)})

    async with httpx.AsyncClient() as http:
        ctx = await context(session, connection.id, http)
        async with command_context(session, CONNECTOR, "binance.opening", {}) as cmd:
            assert await set_openings(await Run.start(ctx, cmd, ctx.connection)) == 1

    assert [q for _, _, q, _ in await _snapshots(session, connection.id)] == [
        Decimal(100),
        Decimal(110),
    ]
    assert await check(session, connection.id, ()) == []
    [opening] = [k for k in await keys(session, connection.id) if k.startswith("opening:")]
    # At the daily snapshot: 100, of which the records before it explain -1.
    assert plain(await postings(session, opening))[0] == (UM_NAME, "STB", Decimal(101))


# The income records.


@pytest.mark.parametrize("label", [UM, CM])
async def test_income_types_cover_the_catalogue_enum(session, run, label: str) -> None:
    connection = await binance_connection(run)
    documented = income_types(label)
    assert TRANSFER in documented and INCOME_FEES <= documented
    kinds = sorted(documented - {TRANSFER})
    paid = [_income(label, 2 * n, kind, "-1.5") for n, kind in enumerate(kinds)]
    received = [_income(label, 2 * n + 1, kind, "2") for n, kind in enumerate(kinds)]
    unknown = _income(label, 999, "SOMETHING_NEW", "4")

    report = await normalize_rows(session, connection.id, (label, [*paid, *received, unknown]))

    wallet = _NAME_OF[label]
    for row in paid:
        memo = row["incomeType"].lower().replace("_", " ")
        found = await postings(session, _key(label, row))
        if row["incomeType"] in INCOME_FEES:
            assert found == [
                (wallet, "STB", Decimal("-1.5"), None, None, None, None, True, memo),
                ("Unassigned", "STB", Decimal("1.5"), None, None, None, None, False, ""),
            ], row["incomeType"]
        else:
            assert found == [
                (wallet, "STB", Decimal("-1.5"), None, None, None, None, False, memo),
                ("Unassigned", "STB", Decimal("1.5"), None, None, None, None, False, ""),
            ], row["incomeType"]
    for row in received:
        assert plain(await postings(session, _key(label, row))) == [
            (wallet, "STB", Decimal(2)),
            ("Unassigned", "STB", Decimal(-2)),
        ], row["incomeType"]
        assert await _never_transfer(session, _key(label, row)) is True
    # A type the catalogue does not list is flagged by name and not posted.
    assert len(await keys(session, connection.id)) == 2 * len(kinds)
    assert f"{label}.incomeType=SOMETHING_NEW" in report.new_flags
    stored = await typed(session, f"binance_{_RECORD[label]}", connection.id)
    assert [r.transaction_id for r in stored if r.income_type == "SOMETHING_NEW"] == [None]
    await assert_balanced_and_quiet(session, connection.id)


@pytest.mark.parametrize("universal_first", [True, False])
@pytest.mark.parametrize(
    ("label", "kind", "signed", "source", "destination"),
    [
        (UM, "MAIN_UMFUTURE", "50", SPOT, UM_NAME),
        (CM, "CMFUTURE_MAIN", "-50", CM_NAME, SPOT),
    ],
)
async def test_income_transfer_is_not_posted_twice(
    session,
    run,
    universal_first: bool,
    label: str,
    kind: str,
    signed: str,
    source: str,
    destination: str,
) -> None:
    connection = await binance_connection(run)
    universal = build_row(
        UNIVERSAL, 0, asset="STB", amount="50", type=kind, status="CONFIRMED", timestamp=T
    )
    moved = _income(label, 0, TRANSFER, signed, time=T + 20_000)
    universal_key = f"universal_transfer:{universal['tranId']}"
    listed = ((UNIVERSAL, [universal]), (label, [moved]))

    for name, rows in listed if universal_first else listed[::-1]:
        await normalize_rows(session, connection.id, (name, rows))

    assert await keys(session, connection.id) == [universal_key]
    [row] = await typed(session, f"binance_{_RECORD[label]}", connection.id)
    assert (row.movement_key, row.transaction_id) == (universal_key, None)
    assert await holdings(session, connection.id) == {
        (source, "STB"): Decimal(-50),
        (destination, "STB"): Decimal(50),
    }
    await assert_balanced_and_quiet(session, connection.id)

    # The universal transfer no longer confirmed: the income row posts the movement, its side
    # the row does not name against Unassigned.
    failed = {**universal, "status": "FAILED"}
    await normalize_rows(session, connection.id, (UNIVERSAL, [failed]))
    assert await keys(session, connection.id) == [_key(label, moved)]
    futures = _NAME_OF[label]
    sign = Decimal(1) if Decimal(signed) > 0 else Decimal(-1)
    assert sorted(plain(await postings(session, _key(label, moved)))) == sorted(
        [(futures, "STB", 50 * sign), ("Unassigned", "STB", -50 * sign)]
    )
    [row] = await typed(session, f"binance_{_RECORD[label]}", connection.id)
    assert row.movement_key is None and row.transaction_id is not None
    await assert_balanced_and_quiet(session, connection.id)


async def test_an_income_transfer_links_only_to_its_own_wallet_and_direction(session, run) -> None:
    connection = await binance_connection(run)
    # Same asset, amount and moment, but into Funding, and out of USDⓈ-M: neither is this one.
    elsewhere = build_row(
        UNIVERSAL, 0, asset="STB", amount="5", type="MAIN_FUNDING", status="CONFIRMED", timestamp=T
    )
    reverse = build_row(
        UNIVERSAL, 1, asset="STB", amount="5", type="UMFUTURE_MAIN", status="CONFIRMED", timestamp=T
    )
    moved = _income(UM, 0, TRANSFER, "5", time=T + 1_000)

    await normalize_rows(session, connection.id, (UNIVERSAL, [elsewhere, reverse]), (UM, [moved]))

    assert plain(await postings(session, _key(UM, moved))) == [
        ("Unassigned", "STB", Decimal(-5)),
        (UM_NAME, "STB", Decimal(5)),
    ]
    assert await _never_transfer(session, _key(UM, moved)) is True
    assert len(await keys(session, connection.id)) == 3
    await assert_balanced_and_quiet(session, connection.id)


async def test_trades_and_positions_are_typed_only(session, run) -> None:
    connection = await binance_connection(run)
    trades = [build_row("um-trades", 0, symbol="AAASTB"), build_row("cm-trades", 0)]
    await normalize_rows(
        session, connection.id, ("um-trades", trades[:1]), ("cm-trades", trades[1:])
    )
    positions = {
        "um-positions": _read("um-positions", [build_row("um-positions", symbol="AAASTB")], T),
        "cm-positions": _read("cm-positions", [build_row("cm-positions")], T + 1_000),
    }
    await run(report_balances, connection, positions)

    assert await keys(session, connection.id) == []
    for table in ("binance_um_trades", "binance_cm_trades"):
        [row] = await typed(session, table, connection.id)
        assert row.transaction_id is None
    for table in ("binance_um_positions", "binance_cm_positions"):
        assert len(await typed(session, table, connection.id)) == 1
    # A contract is not an instrument: a position is no snapshot.
    assert await _snapshots(session, connection.id) == []
    await assert_balanced_and_quiet(session, connection.id)


# The opening.


async def test_old_income_becomes_the_opening(session, run) -> None:
    connection = await binance_connection(run)
    old = [_income(UM, 0, "REALIZED_PNL", "3"), _income(UM, 1, "COMMISSION", "-1")]
    await normalize_rows(session, connection.id, (UM, old))
    report = [_asset(UM_ACCOUNT, "STB", "10", "4")]
    await run(report_balances, connection, {UM_ACCOUNT: _read(UM_ACCOUNT, report, T + DAY_MS)})

    async with httpx.AsyncClient() as http:
        ctx = await context(session, connection.id, http)
        async with command_context(session, CONNECTOR, "binance.opening", {}) as cmd:
            made = await set_openings(await Run.start(ctx, cmd, ctx.connection))

    assert made == 1
    [opening] = [
        k for k in await keys(session, connection.id) if k.startswith("opening:um_futures:")
    ]
    # The records before the report explain 2 of its wallet balance of 10: the opening is the
    # other 8. The unrealised profit (4) is in the report, and in no opening.
    assert plain(await postings(session, opening)) == [
        (UM_NAME, "STB", Decimal(8)),
        ("Opening balances", "STB", Decimal(-8)),
    ]
    assert await check(session, connection.id, ()) == []


# The sync.


@pytest.mark.parametrize(
    ("label", "kind", "coin"),
    [(UM, "MAIN_UMFUTURE", "STB"), (CM, "MAIN_CMFUTURE", "BBB")],
)
async def test_a_futures_wallet_reconciles_with_a_consistent_history(
    session, run, fake: FakeBinance, connector, clock: Clock, label: str, kind: str, coin: str
) -> None:
    account = _ACCOUNT_OF[label]
    fake.set_rows("spot-account", [build_row("spot-account", asset=coin, free="100", locked="0")])
    fake.set_rows(account, [_asset(account, coin, "100", "5")])
    connection = await binance_connection(run, config=later_config())
    first = await run_binance_sync(session, connection.id, fake.handler)
    assert first.status == "healthy", first.last_error

    start = clock.ms

    def hours(n: int) -> int:
        return start + n * HOUR_MS

    # 50 into the futures wallet, listed by the universal transfer history and the income
    # history (rule 2); a realised profit, a commission and a funding fee paid; the position's
    # unrealised profit moves from 5 to -2.
    fake.add(
        UNIVERSAL,
        build_row(
            UNIVERSAL, 0, asset=coin, amount="50", type=kind, status="CONFIRMED", timestamp=hours(1)
        ),
        when={"type": kind},
    )
    fake.add(
        label,
        _income(label, 0, TRANSFER, "50", asset=coin, time=hours(1) + 2_000),
        _income(label, 1, "REALIZED_PNL", "3", asset=coin, time=hours(2)),
        _income(label, 2, "COMMISSION", "-0.5", asset=coin, time=hours(2)),
        _income(label, 3, "FUNDING_FEE", "-0.25", asset=coin, time=hours(3)),
    )
    fake.set_rows("spot-account", [build_row("spot-account", asset=coin, free="50", locked="0")])
    fake.set_rows(account, [_asset(account, coin, "152.25", "-2")])
    notifier = RecordingNotifier()
    clock.ms += 6 * HOUR_MS
    after = await run_binance_sync(session, connection.id, fake.handler, notifier=notifier)

    assert after.status == "healthy", after.last_error
    assert await _differences(session, after.id) == []
    assert not [w for w in after.warnings if _CHANGED in w]
    assert not [m for m in notifier.messages if _CHANGED in m]
    live = await keys(session, after.id)
    assert [k for k in live if k.startswith("universal_transfer:")] == [
        f"universal_transfer:{build_row(UNIVERSAL, 0)['tranId']}"
    ]
    assert len([k for k in live if k.startswith(f"{_RECORD[label]}:")]) == 3
    reported = [
        q for name, _, q, _ in await _snapshots(session, after.id) if name.endswith("M Futures")
    ]
    assert reported == [Decimal(105), Decimal("150.25")]


async def test_futures_trades_are_walked_for_the_symbols_the_income_names(
    session, run, fake: FakeBinance, connector, clock: Clock
) -> None:
    traded = clock.ms - 2 * DAY_MS
    fake.add(
        UM,
        _income(UM, 0, "COMMISSION", "-0.1", symbol="AAASTB", time=traded),
        _income(UM, 1, TRANSFER, "5", time=traded),
    )
    fake.add(CM, _income(CM, 0, "COMMISSION", "-0.1", symbol="BBBUSD_PERP", time=traded))
    fake.add("um-trades", build_row("um-trades", 0, symbol="AAASTB", time=traded))
    connection = await binance_connection(run, config=later_config())
    # Income of a symbol more than 3 months old: Binance serves none of its trades now.
    long_ago = _income(UM, 7, "COMMISSION", "-0.1", symbol="OLDSTB", time=clock.ms - 100 * DAY_MS)
    await ingest(session, connection.id, UM, [long_ago])

    after = await run_binance_sync(session, connection.id, fake.handler)

    assert after.status == "healthy", after.last_error
    assert {s.params["symbol"] for s in fake.calls("um-trades")} == {"AAASTB"}
    assert "um-trades:OLDSTB" not in after.config[CURSORS_KEY]
    assert {s.params["symbol"] for s in fake.calls("cm-trades")} == {"BBBUSD_PERP"}
    for label in FUTURES_TRADES:
        for seen in fake.calls(label):
            start, end = int(seen.params["startTime"]), int(seen.params["endTime"])
            # At most 7 days a request, and no further back than 3 months.
            assert end - start < 7 * DAY_MS
            assert start >= clock.ms - 90 * DAY_MS
    [trade] = await typed(session, "binance_um_trades", after.id)
    assert trade.symbol == "AAASTB" and trade.transaction_id is None
    assert after.config[CURSORS_KEY]["um-trades:AAASTB"]["complete"] is True


async def test_each_futures_host_is_asked_its_limits_first(
    session, run, fake: FakeBinance, connector
) -> None:
    connection = await binance_connection(run, config=later_config())

    after = await run_binance_sync(session, connection.id, fake.handler)

    assert after.status == "healthy", after.last_error
    for host, info in (("fapi", "um-exchange-info"), ("dapi", "cm-exchange-info")):
        asked = [s.label for s in fake.requests if ENDPOINTS[s.label].limit == host]
        assert asked[0] == info and asked.count(info) == 1
    [limit] = await typed(session, "binance_um_exchange_info_rate_limits", after.id)
    assert (limit.rate_limit_type, limit.interval, limit.limit) == (
        "REQUEST_WEIGHT",
        "MINUTE",
        2400,
    )
    # The futures wallets are there and empty, as the probe's were: no account is made.
    assert not [n for n, *_ in await _snapshots(session, after.id) if "Futures" in n]


@pytest.mark.parametrize("failure", ["shape", "error"])
@pytest.mark.parametrize(
    ("label", "host"), [("um-exchange-info", "fapi"), ("cm-exchange-info", "dapi")]
)
async def test_a_futures_limits_read_that_fails_never_stops_the_sync(
    session, run, fake: FakeBinance, connector, clock: Clock, label: str, host: str, failure: str
) -> None:
    if failure == "shape":
        # COIN-M's page gives `exchangeFilters` as strings in its schema and `[[]]` in its
        # example: an answer the catalogue cannot type.
        fake.set_envelope(label, exchangeFilters=[[]])
    else:
        fake.fail(label, status=500)
    connection = await binance_connection(run, config=later_config())

    after = await run_binance_sync(session, connection.id, fake.handler)

    assert after.status == "healthy", after.last_error
    assert [f for f in after.config["flags"] if label in f] == [limits_unread_flag(label)]
    assert await typed(session, f"binance_{label.replace('-', '_')}", after.id) == []
    other = next(info for info in FUTURES_INFO if info != label)
    assert len(await typed(session, f"binance_{other.replace('-', '_')}", after.id)) == 1
    # The host keeps the assumed limit; the other takes its own.
    flags: list[str] = []
    async with fake.http() as http:
        client = client_for(clock, http)
        await client.sync_clock()
        await read_limits(client, flag=flags.append)
    assert flags == [limits_unread_flag(label)]
    other_host = "dapi" if host == "fapi" else "fapi"
    assert (client.caps[host], client.caps[other_host]) == (600, 2400)
