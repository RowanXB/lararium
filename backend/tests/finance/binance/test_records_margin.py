"""The margin wallets in the ledger (design 5.4, 5.6, 5.7, 5.11): Cross Margin and Isolated
Margin hold signed net quantities (a borrowed asset is negative), reported from each account's
`netAsset`; a margin trade is a spot trade on its wallet, interest and the liquidation fee are
fees on it, a margin transfer is one transaction on two wallets unless the universal transfer
history already lists it, and borrowing, repaying and the other margin records are typed only.
An account Binance says is not created is not present."""

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import text

from lararium.modules.finance.binance.balances import Read, report_balances
from lararium.modules.finance.binance.catalogue import ENDPOINTS, ISOLATED_PARAMS
from lararium.modules.finance.binance.fetch import (
    BALANCE_LABELS,
    CURSORS_KEY,
    HISTORY_LABELS,
    MARGIN_TRADES,
    NOT_PRESENT_KEY,
    PHASE_B_BUILT,
    TRADE_LABELS,
    backfill_complete,
    cursor_keys,
    to_json,
    walk_keys,
)
from lararium.modules.finance.binance.reconcile import check
from lararium.modules.finance.binance.sync import FIRST_SYNC_DONE_KEY, SWEPT_KEY
from lararium.modules.finance.pricing import record_price
from lararium.modules.finance.valuation import holdings_asof

from .fixtures import BASE_TIME_MS, HOUR_MS, NOW_MS, FakeBinance, build_response, build_row
from .normalizing import (
    assert_balanced_and_quiet,
    at,
    holdings,
    ingest,
    keys,
    list_pairs,
    normalize_rows,
    plain,
    postings,
    run_normalize,
    transaction,
    typed,
)
from .syncing import (
    Clock,
    RecordingNotifier,
    binance_connection,
    later_config,
    run_binance_sync,
)

T = BASE_TIME_MS
CROSS = "Binance Cross Margin"
ISOLATED = "Binance Isolated Margin"
SPOT = "Binance Spot"
ACCOUNT = "margin-account"
ISOLATED_ACCOUNT = "margin-isolated-account"
TRANSFERS = "margin-transfers"
BORROW_REPAY = "margin-borrow-repay"
INTEREST = "margin-interest"
CAPITAL_FLOW = "margin-capital-flow"
LIQUIDATIONS = "margin-liquidations"
SMALL_LIABILITY = "margin-small-liability"
LIQUIDATION_LOAN = "margin-liquidation-loan"
LOAN_REPAYS = "margin-liquidation-loan-repays"
UNIVERSAL = "universal-transfer"
_CHANGED = "changed by something no record explains"

HISTORIES = (
    TRANSFERS,
    BORROW_REPAY,
    INTEREST,
    CAPITAL_FLOW,
    LIQUIDATIONS,
    SMALL_LIABILITY,
    LOAN_REPAYS,
)
REPORTS = (ACCOUNT, ISOLATED_ACCOUNT, LIQUIDATION_LOAN)


def _user_asset(code: str, net: str, *, borrowed: str = "0", interest: str = "0") -> dict:
    free = Decimal(net) + Decimal(borrowed) + Decimal(interest)
    return build_row(
        ACCOUNT,
        asset=code,
        free=format(max(free, Decimal(0)), "f"),
        locked="0",
        borrowed=borrowed,
        interest=interest,
        netAsset=net,
    )


def _leg(code: str, net: str) -> dict:
    return {
        "asset": code,
        "borrowEnabled": True,
        "borrowed": "0",
        "free": net,
        "interest": "0",
        "locked": "0",
        "netAsset": net,
        "netAssetOfBtc": "0",
        "repayEnabled": True,
        "totalAsset": net,
    }


def _pair(symbol: str, base: tuple[str, str], quote: tuple[str, str], created=True) -> dict:
    return build_row(
        ISOLATED_ACCOUNT,
        symbol=symbol,
        isolatedCreated=created,
        baseAsset=_leg(*base),
        quoteAsset=_leg(*quote),
    )


def _read(label: str, rows: list, at_ms: int, **envelope) -> Read:
    body = build_response(label, rows)
    if envelope:
        assert isinstance(body, dict)
        body = {**body, **envelope}
    return Read(label, to_json(body), at(at_ms), at(at_ms + 200))


def _trade(seq: int = 0, *, isolated: bool = False, **fields) -> dict:
    values = {
        "symbol": "AAASTB",
        "isBuyer": True,
        "qty": "2",
        "price": "10",
        "commission": "0",
        "commissionAsset": "STB",
        "isIsolated": isolated,
        "time": T + seq * HOUR_MS,
    }
    return build_row(MARGIN_TRADES, seq, **(values | fields))


def _transfer(seq: int = 0, **fields) -> dict:
    values = {
        "amount": "50",
        "asset": "STB",
        "status": "CONFIRMED",
        "timestamp": T + seq * HOUR_MS,
        "type": "ROLL_IN",
        "transFrom": "SPOT",
        "transTo": "CROSS_MARGIN",
        "fromSymbol": None,
        "toSymbol": None,
    }
    return build_row(TRANSFERS, seq, **(values | fields))


def _universal(seq: int = 0, **fields) -> dict:
    values = {
        "asset": "STB",
        "amount": "50",
        "type": "MAIN_MARGIN",
        "status": "CONFIRMED",
        "timestamp": T + seq * HOUR_MS,
    }
    return build_row(UNIVERSAL, seq, **(values | fields))


def _interest(seq: int = 0, **fields) -> dict:
    values = {
        "asset": "STB",
        "interest": "0.1",
        "interestAccuredTime": T + seq * HOUR_MS,
        "type": "PERIODIC",
        "isolatedSymbol": None,
    }
    return build_row(INTEREST, seq, **(values | fields))


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


async def _account_names(session, connection_id) -> list[str]:
    found = await session.scalars(
        text("select name from finance.accounts where source_connection_id = :c order by name"),
        {"c": connection_id},
    )
    return list(found)


async def _never_transfer(session, key: str) -> bool:
    return await session.scalar(
        text("select never_transfer from finance.transactions where external_id = :e"), {"e": key}
    )


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


def _pairs(fake: FakeBinance, *pairs: tuple[str, str]) -> None:
    fake.set_rows(
        "exchange-info",
        [build_row("exchange-info", symbol=b + q, baseAsset=b, quoteAsset=q) for b, q in pairs],
    )


def _spot(*held: tuple[str, str]) -> list[dict]:
    return [build_row("spot-account", asset=coin, free=q, locked="0") for coin, q in held]


def test_the_family_is_walked_and_read() -> None:
    assert set(HISTORIES) | set(REPORTS) <= set(PHASE_B_BUILT)
    assert set(HISTORIES) <= set(HISTORY_LABELS)
    assert set(REPORTS) <= set(BALANCE_LABELS)
    # Margin trades are walked by symbol and trade id, as Spot's are.
    assert MARGIN_TRADES in TRADE_LABELS and MARGIN_TRADES not in HISTORY_LABELS
    # The histories that take an isolated pair are asked once more per pair.
    assert set(ISOLATED_PARAMS) == {TRANSFERS, BORROW_REPAY, INTEREST, CAPITAL_FLOW, LIQUIDATIONS}
    assert ISOLATED_PARAMS[CAPITAL_FLOW] == "symbol"
    assert walk_keys(TRANSFERS, {"AAASTB"}) == [
        (TRANSFERS, {}),
        (f"{TRANSFERS}:AAASTB", {"isolatedSymbol": "AAASTB"}),
    ]
    assert walk_keys(BORROW_REPAY, {"AAASTB"})[2:] == [
        (f"{BORROW_REPAY}:BORROW:AAASTB", {"type": "BORROW", "isolatedSymbol": "AAASTB"}),
        (f"{BORROW_REPAY}:REPAY:AAASTB", {"type": "REPAY", "isolatedSymbol": "AAASTB"}),
    ]
    assert walk_keys(SMALL_LIABILITY, {"AAASTB"}) == cursor_keys(ENDPOINTS[SMALL_LIABILITY])


# The reported balances.


async def test_borrowed_asset_is_negative(session, run) -> None:
    connection = await binance_connection(run)
    flags: list[str] = []
    rows = [_user_asset("AAA", "5"), _user_asset("BBB", "-3", borrowed="3.1", interest="0.1")]
    listed = build_row("wallet-balance", walletName="Cross Margin", balance="1", assetBalances=[])
    reads = {
        ACCOUNT: _read(ACCOUNT, rows, NOW_MS),
        "wallet-balance": _read("wallet-balance", [listed], NOW_MS + 1_000),
    }

    await run(report_balances, connection, reads, flag=flags.append)

    assert await _snapshots(session, connection.id) == [
        (CROSS, "AAA", Decimal(5), at(NOW_MS)),
        (CROSS, "BBB", Decimal(-3), at(NOW_MS)),
    ]
    # The wallet list's Cross Margin row agrees with the account's own report, and is not
    # counted a second time.
    assert flags == []
    gbp = await session.scalar(
        text(
            "select instrument_id from finance.instrument_identifiers"
            " where scheme = 'iso4217' and value = 'GBP'"
        )
    )
    day = at(NOW_MS).date()
    for symbol, price in (("AAA", "2"), ("BBB", "4")):
        instrument = await session.scalar(
            text("select id from finance.instruments where symbol = :s"), {"s": symbol}
        )
        await run(
            record_price,
            instrument_id=instrument,
            quote_instrument_id=gbp,
            price=Decimal(price),
            as_of_date=day,
        )
    assert await _net_worth(session, connection.id, day) == Decimal(-2)

    # The borrowed asset repaid in full is gone from the report: it reads 0.
    later = NOW_MS + HOUR_MS
    await run(report_balances, connection, {ACCOUNT: _read(ACCOUNT, rows[:1], later)})
    assert (await _snapshots(session, connection.id))[2:] == [
        (CROSS, "AAA", Decimal(5), at(later)),
        (CROSS, "BBB", Decimal(0), at(later)),
    ]


async def _net_worth(session, connection_id, day: date) -> Decimal:
    accounts = set(
        await session.scalars(
            text("select id from finance.accounts where source_connection_id = :c"),
            {"c": connection_id},
        )
    )
    rows = [h for h in await holdings_asof(session, day) if h.account_id in accounts]
    assert all(h.status == "ok" for h in rows), rows
    return sum((h.value for h in rows if h.value is not None), Decimal(0))


async def test_isolated_pairs_sum_per_asset(session, run) -> None:
    connection = await binance_connection(run)
    pairs = [
        _pair("AAASTB", ("AAA", "2"), ("STB", "-10")),
        _pair("BBBSTB", ("BBB", "1"), ("STB", "30")),
        # Not created: holds nothing, and is not a pair of the account.
        _pair("CCCSTB", ("CCC", "0"), ("STB", "0"), created=False),
    ]

    await run(report_balances, connection, {ISOLATED_ACCOUNT: _read(ISOLATED_ACCOUNT, pairs, T)})

    assert await _snapshots(session, connection.id) == [
        (ISOLATED, "AAA", Decimal(2), at(T)),
        (ISOLATED, "BBB", Decimal(1), at(T)),
        (ISOLATED, "STB", Decimal(20), at(T)),
    ]
    [row] = await typed(session, "binance_margin_isolated_account", connection.id)
    assert row.snapshot_at == at(T)
    legs = await typed(session, "binance_margin_isolated_account_assets", connection.id)
    assert sorted((r.symbol, r.base_asset_asset, r.quote_asset_net_asset) for r in legs) == [
        ("AAASTB", "AAA", Decimal(-10)),
        ("BBBSTB", "BBB", Decimal(30)),
        ("CCCSTB", "CCC", Decimal(0)),
    ]


async def test_uncreated_margin_account_is_skipped(
    session, run, fake: FakeBinance, connector, clock: Clock
) -> None:
    # The account the probe saw: the cross account answers `created: false` and every margin
    # history -3003; the isolated account lists no pair.
    _pairs(fake, ("AAA", "STB"))
    fake.set_envelope(ACCOUNT, created=False)
    for label in HISTORIES:
        fake.fail(label, code=-3003)
    connection = await binance_connection(run)

    first = await run_binance_sync(session, connection.id, fake.handler)

    assert first.status == "healthy", first.last_error
    assert first.config[FIRST_SYNC_DONE_KEY] is True
    missing = first.config[NOT_PRESENT_KEY]
    assert ACCOUNT in missing
    assert {key for label in HISTORIES for key, _ in cursor_keys(ENDPOINTS[label])} <= set(missing)
    assert fake.calls(MARGIN_TRADES) == []
    assert not [n for n in await _account_names(session, connection.id) if "Margin" in n]
    assert await _snapshots(session, connection.id) == []

    # Like a product Binance does not serve: not asked again for 7 days.
    fake.requests.clear()
    clock.ms += 5 * HOUR_MS
    again = await run_binance_sync(session, connection.id, fake.handler)
    assert again.status == "healthy", again.last_error
    assert fake.calls(ACCOUNT) == [] and fake.calls(MARGIN_TRADES) == []
    assert not [s for s in fake.requests if s.label in HISTORIES]


async def test_borrowing_leaves_the_net_quantity_unchanged(session, run) -> None:
    connection = await binance_connection(run)
    await run(report_balances, connection, {ACCOUNT: _read(ACCOUNT, [_user_asset("AAA", "10")], T)})
    borrowed = build_row(
        BORROW_REPAY, 0, type="MANUAL", asset="AAA", amount="5", status="CONFIRMED", timestamp=T + 1
    )
    repaid = build_row(
        BORROW_REPAY,
        1,
        type="MANUAL",
        asset="AAA",
        amount="2.01",
        principal="2",
        interest="0.01",
        status="CONFIRMED",
        timestamp=T + 2,
    )
    await ingest(session, connection.id, BORROW_REPAY, [borrowed], request={"type": "BORROW"})
    await ingest(session, connection.id, BORROW_REPAY, [repaid], request={"type": "REPAY"})
    # 5 borrowed, 2 of it repaid with 0.01 of interest: free 13, borrowed 3, net still 10 (the
    # interest was charged, and posted, when it accrued).
    later = [_user_asset("AAA", "10", borrowed="3")]
    await run(report_balances, connection, {ACCOUNT: _read(ACCOUNT, later, T + HOUR_MS)})

    await run_normalize(session, connection.id)

    assert await keys(session, connection.id) == []
    rows = await typed(session, "binance_margin_borrow_repay", connection.id)
    assert sorted((r.amount, r.transaction_id) for r in rows) == [
        (Decimal("2.01"), None),
        (Decimal(5), None),
    ]
    assert await check(session, connection.id, ()) == []
    await assert_balanced_and_quiet(session, connection.id)


# The records.


async def test_margin_trade_is_a_spot_trade_on_its_wallet(session, run) -> None:
    connection = await binance_connection(run)
    await list_pairs(run, connection, ("AAASTB", "AAA", "STB"))
    cross = _trade(0, commission="0.01", commissionAsset="STB")
    isolated = _trade(1, isolated=True, isBuyer=False, qty="1.5", price="10.5")

    await normalize_rows(session, connection.id, (MARGIN_TRADES, [cross, isolated]))

    bought = await postings(session, f"margin_trade:AAASTB:{cross['id']}")
    assert bought == [
        (CROSS, "AAA", Decimal(2), Decimal(10), "STB", None, None, False, ""),
        (CROSS, "STB", Decimal(-20), None, None, None, None, False, ""),
        (CROSS, "STB", Decimal("-0.01"), None, None, None, None, True, "commission"),
        ("Unassigned", "STB", Decimal("0.01"), None, None, None, None, False, ""),
    ]
    sold = await postings(session, f"margin_trade:AAASTB:{isolated['id']}")
    # No quoteQty is documented for a margin trade: the quote side is qty times price.
    assert sold == [
        (ISOLATED, "AAA", Decimal("-1.5"), None, None, Decimal("10.5"), "STB", False, ""),
        (ISOLATED, "STB", Decimal("15.75"), None, None, None, None, False, ""),
    ]
    key = f"margin_trade:AAASTB:{cross['id']}"
    assert await _never_transfer(session, key) is True
    assert (await transaction(session, key)).occurred_at == at(T)
    rows = await typed(session, "binance_margin_trades", connection.id)
    assert all(r.transaction_id is not None for r in rows) and len(rows) == 2
    await assert_balanced_and_quiet(session, connection.id)


async def test_margin_trade_without_its_isolated_flag_takes_the_request(session, run) -> None:
    connection = await binance_connection(run)
    await list_pairs(run, connection, ("AAASTB", "AAA", "STB"))
    row = _trade(0, isIsolated=None)
    await ingest(
        session,
        connection.id,
        MARGIN_TRADES,
        [row],
        request={"symbol": "AAASTB", "isIsolated": "TRUE"},
    )

    await run_normalize(session, connection.id)

    assert plain(await postings(session, f"margin_trade:AAASTB:{row['id']}")) == [
        (ISOLATED, "AAA", Decimal(2)),
        (ISOLATED, "STB", Decimal(-20)),
    ]


async def test_interest_is_a_fee_on_the_margin_wallet(session, run) -> None:
    connection = await binance_connection(run)
    cross = _interest(0)
    isolated = _interest(1, asset="AAA", interest="0.002", isolatedSymbol="AAASTB")
    # Portfolio margin's daily interest is not this account's: typed only.
    portfolio = _interest(2, type="PORTFOLIO", interest="0.3")

    await normalize_rows(session, connection.id, (INTEREST, [cross, isolated, portfolio]))

    cross_key = f"margin_interest:{cross['txId']}:{T}:STB"
    assert await postings(session, cross_key) == [
        (CROSS, "STB", Decimal("-0.1"), None, None, None, None, True, "margin interest"),
        ("Unassigned", "STB", Decimal("0.1"), None, None, None, None, False, ""),
    ]
    assert plain(
        await postings(session, f"margin_interest:{isolated['txId']}:{T + HOUR_MS}:AAA")
    ) == [
        (ISOLATED, "AAA", Decimal("-0.002")),
        ("Unassigned", "AAA", Decimal("0.002")),
    ]
    assert await _never_transfer(session, cross_key) is True
    assert len(await keys(session, connection.id)) == 2
    await assert_balanced_and_quiet(session, connection.id)


async def test_liquidation_fee_is_a_fee_and_the_rest_of_the_flow_typed_only(session, run) -> None:
    connection = await binance_connection(run)
    fee = build_row(
        CAPITAL_FLOW,
        0,
        type="LIQUIDATION_FEE",
        asset="STB",
        amount="-1.25",
        symbol=None,
        timestamp=T,
    )
    isolated_fee = build_row(
        CAPITAL_FLOW, 1, type="LIQUIDATION_FEE", asset="AAA", amount="-0.5", symbol="AAASTB"
    )
    borrow = build_row(CAPITAL_FLOW, 2, type="BORROW", asset="STB", amount="100", symbol=None)
    sold = build_row(
        CAPITAL_FLOW, 3, type="SELL_LIQUIDATION", asset="AAA", amount="-3", symbol=None
    )

    await normalize_rows(session, connection.id, (CAPITAL_FLOW, [fee, isolated_fee, borrow, sold]))

    assert await postings(session, f"margin_capital_flow:{fee['id']}") == [
        (CROSS, "STB", Decimal("-1.25"), None, None, None, None, True, "liquidation fee"),
        ("Unassigned", "STB", Decimal("1.25"), None, None, None, None, False, ""),
    ]
    assert plain(await postings(session, f"margin_capital_flow:{isolated_fee['id']}")) == [
        (ISOLATED, "AAA", Decimal("-0.5")),
        ("Unassigned", "AAA", Decimal("0.5")),
    ]
    assert len(await keys(session, connection.id)) == 2
    rows = await typed(session, "binance_margin_capital_flow", connection.id)
    assert sorted((r.type, r.transaction_id is None) for r in rows) == [
        ("BORROW", True),
        ("LIQUIDATION_FEE", False),
        ("LIQUIDATION_FEE", False),
        ("SELL_LIQUIDATION", True),
    ]
    await assert_balanced_and_quiet(session, connection.id)


async def test_other_margin_records_are_typed_only(session, run) -> None:
    connection = await binance_connection(run)
    liquidation = build_row(LIQUIDATIONS, 0, symbol="AAASTB", side="SELL", isIsolated=False)
    small = build_row(SMALL_LIABILITY, 0, asset="AAA", amount="0.001", targetAsset="BNB")
    loan_repay = build_row(LOAN_REPAYS, 0, asset="STB", amount="3", status="SUCCESS")

    await normalize_rows(
        session,
        connection.id,
        (LIQUIDATIONS, [liquidation]),
        (SMALL_LIABILITY, [small]),
        (LOAN_REPAYS, [loan_repay]),
    )

    assert await keys(session, connection.id) == []
    for table in (
        "binance_margin_liquidations",
        "binance_margin_small_liability",
        "binance_margin_liquidation_loan_repays",
    ):
        [row] = await typed(session, table, connection.id)
        assert row.transaction_id is None
    await assert_balanced_and_quiet(session, connection.id)


async def test_margin_transfer_is_one_transaction_on_two_wallets(session, run) -> None:
    connection = await binance_connection(run)
    into = _transfer(0)
    out_of_isolated = _transfer(
        1,
        asset="AAA",
        amount="1.5",
        type="ROLL_OUT",
        transFrom="ISOLATED_MARGIN",
        transTo="SPOT",
        fromSymbol="AAASTB",
    )
    from_sub_account = _transfer(2, amount="7", transFrom="SUB_SPOT")
    pending = _transfer(3, status="PENDING")

    await normalize_rows(
        session, connection.id, (TRANSFERS, [into, out_of_isolated, from_sub_account, pending])
    )

    key = f"margin_transfer:{into['txId']}"
    assert plain(await postings(session, key)) == [
        (SPOT, "STB", Decimal(-50)),
        (CROSS, "STB", Decimal(50)),
    ]
    assert await _never_transfer(session, key) is True
    assert plain(await postings(session, f"margin_transfer:{out_of_isolated['txId']}")) == [
        (ISOLATED, "AAA", Decimal("-1.5")),
        (SPOT, "AAA", Decimal("1.5")),
    ]
    # From a sub-account, outside this connection: income on Cross Margin.
    assert plain(await postings(session, f"margin_transfer:{from_sub_account['txId']}")) == [
        ("Unassigned", "STB", Decimal(-7)),
        (CROSS, "STB", Decimal(7)),
    ]
    assert len(await keys(session, connection.id)) == 3
    await assert_balanced_and_quiet(session, connection.id)

    # Confirmed later: posted then.
    await normalize_rows(session, connection.id, (TRANSFERS, [{**pending, "status": "CONFIRMED"}]))
    assert len(await keys(session, connection.id)) == 4
    await assert_balanced_and_quiet(session, connection.id)


@pytest.mark.parametrize("universal_first", [True, False])
async def test_margin_transfer_the_universal_history_lists_is_posted_once(
    session, run, universal_first: bool
) -> None:
    connection = await binance_connection(run)
    universal = _universal(0, timestamp=T)
    margin = _transfer(0, timestamp=T + 30_000)
    universal_key = f"universal_transfer:{universal['tranId']}"
    margin_key = f"margin_transfer:{margin['txId']}"
    listed = ((UNIVERSAL, [universal]), (TRANSFERS, [margin]))

    for label, rows in listed if universal_first else listed[::-1]:
        await normalize_rows(session, connection.id, (label, rows))

    assert await keys(session, connection.id) == [universal_key]
    [row] = await typed(session, "binance_margin_transfers", connection.id)
    assert (row.movement_key, row.transaction_id) == (universal_key, None)
    assert await holdings(session, connection.id) == {
        (SPOT, "STB"): Decimal(-50),
        (CROSS, "STB"): Decimal(50),
    }
    await assert_balanced_and_quiet(session, connection.id)

    # The universal transfer no longer confirmed: the margin transfer posts the movement.
    await normalize_rows(session, connection.id, (UNIVERSAL, [{**universal, "status": "FAILED"}]))
    assert await keys(session, connection.id) == [margin_key]
    [row] = await typed(session, "binance_margin_transfers", connection.id)
    assert row.movement_key is None and row.transaction_id is not None
    await assert_balanced_and_quiet(session, connection.id)


# The isolated pairs, the walks and the first sync.


def test_isolated_pair_cursors_count_toward_the_first_sync() -> None:
    config = later_config()
    del config[FIRST_SYNC_DONE_KEY]
    assert backfill_complete(config)
    assert not backfill_complete(config, {"AAASTB"})
    cursors = dict(config[CURSORS_KEY])  # pyright: ignore[reportArgumentType,reportCallIssue]
    done = {"fetched_to": NOW_MS, "backfilled_to": 0, "complete": True}
    for label in [UNIVERSAL, *ISOLATED_PARAMS]:
        for key, _ in walk_keys(label, {"AAASTB"}):
            cursors[key] = done
    assert backfill_complete({**config, CURSORS_KEY: cursors}, {"AAASTB"})


async def test_isolated_pairs_are_asked_for_their_transfers_and_trades(
    session, run, fake: FakeBinance, connector, clock: Clock
) -> None:
    _pairs(fake, ("AAA", "STB"), ("BBB", "STB"))
    fake.set_rows(
        ISOLATED_ACCOUNT,
        [
            _pair("AAASTB", ("AAA", "1"), ("STB", "0")),
            _pair("BBBSTB", ("BBB", "0"), ("STB", "0"), created=False),
        ],
    )
    connection = await binance_connection(run)

    first = await run_binance_sync(session, connection.id, fake.handler)

    assert first.status == "healthy", first.last_error
    isolated_types = {
        (s.params["type"], s.params.get("fromSymbol"), s.params.get("toSymbol"))
        for s in fake.calls(UNIVERSAL)
        if "ISOLATED" in s.params["type"]
    }
    assert isolated_types == {
        ("ISOLATEDMARGIN_MARGIN", "AAASTB", None),
        ("MARGIN_ISOLATEDMARGIN", None, "AAASTB"),
    }
    for label, name in ISOLATED_PARAMS.items():
        asked = {s.params.get(name) for s in fake.calls(label)}
        assert asked == {None, "AAASTB"}, label
    isolated_trades = {
        s.params["symbol"] for s in fake.calls(MARGIN_TRADES) if "isIsolated" in s.params
    }
    assert isolated_trades == {"AAASTB"}
    assert all(
        s.params["isIsolated"] == "TRUE"
        for s in fake.calls(MARGIN_TRADES)
        if "isIsolated" in s.params
    )
    # The first sync asks the cross margin trades of every listed symbol.
    cross = {s.params["symbol"] for s in fake.calls(MARGIN_TRADES) if "isIsolated" not in s.params}
    assert cross == {"AAASTB", "BBBSTB"}
    assert first.config[FIRST_SYNC_DONE_KEY] is True
    assert first.config[CURSORS_KEY][f"{TRANSFERS}:AAASTB"]["complete"] is True

    # A pair that appears later starts its own walk; the first sync stays done.
    fake.set_rows(
        ISOLATED_ACCOUNT,
        [_pair("AAASTB", ("AAA", "1"), ("STB", "0")), _pair("BBBSTB", ("BBB", "0"), ("STB", "0"))],
    )
    fake.requests.clear()
    clock.ms += 5 * HOUR_MS
    later = await run_binance_sync(session, connection.id, fake.handler)

    assert later.status == "healthy", later.last_error
    assert later.config[FIRST_SYNC_DONE_KEY] is True
    assert {s.params.get("isolatedSymbol") for s in fake.calls(TRANSFERS)} == {
        None,
        "AAASTB",
        "BBBSTB",
    }
    assert later.config[CURSORS_KEY][f"{TRANSFERS}:BBBSTB"]["complete"] is True


async def test_capital_flow_is_asked_in_windows_without_paging_by_id(
    session, run, fake: FakeBinance, connector
) -> None:
    connection = await binance_connection(run)

    after = await run_binance_sync(session, connection.id, fake.handler)

    assert after.status == "healthy", after.last_error
    calls = fake.calls(CAPITAL_FLOW)
    assert calls and all("fromId" not in s.params for s in calls)
    assert all(
        int(s.params["endTime"]) - int(s.params["startTime"]) < 7 * 24 * HOUR_MS for s in calls
    )


# Reconciliation: each margin wallet with a consistent history.


async def test_cross_margin_reconciles_with_a_consistent_history(
    session, run, fake: FakeBinance, connector, clock: Clock
) -> None:
    _pairs(fake, ("AAA", "STB"))
    fake.set_rows("spot-account", _spot(("STB", "100")))
    fake.set_rows(ACCOUNT, [_user_asset("AAA", "1")])
    connection = await binance_connection(run, config=later_config())
    first = await run_binance_sync(session, connection.id, fake.handler)
    assert first.status == "healthy", first.last_error

    start = clock.ms

    def hours(n: int) -> int:
        return start + n * HOUR_MS

    # 50 STB into Cross Margin, listed by both transfer histories; a buy of 2 AAA at 10 with a
    # commission; an hour's interest on a borrow, which itself changes no net quantity.
    fake.add(UNIVERSAL, _universal(0, timestamp=hours(1)), when={"type": "MAIN_MARGIN"})
    fake.add(TRANSFERS, _transfer(0, timestamp=hours(1) + 2_000))
    fake.add(MARGIN_TRADES, _trade(0, time=hours(2), commission="0.01", commissionAsset="STB"))
    fake.add(
        BORROW_REPAY,
        build_row(
            BORROW_REPAY, 0, asset="STB", amount="30", status="CONFIRMED", timestamp=hours(3)
        ),
        when={"type": "BORROW"},
    )
    fake.add(INTEREST, _interest(0, interestAccuredTime=hours(4)))
    fake.set_rows("spot-account", _spot(("STB", "50")))
    fake.set_rows(
        ACCOUNT,
        [_user_asset("AAA", "3"), _user_asset("STB", "29.89", borrowed="30", interest="0.1")],
    )
    notifier = RecordingNotifier()
    clock.ms += 6 * HOUR_MS
    after = await run_binance_sync(session, connection.id, fake.handler, notifier=notifier)

    assert after.status == "healthy", after.last_error
    assert await _differences(session, after.id) == []
    assert not [w for w in after.warnings if _CHANGED in w]
    assert not [m for m in notifier.messages if _CHANGED in m]
    live = await keys(session, after.id)
    assert [k for k in live if k.startswith(("margin_transfer:", "universal_transfer:"))] == [
        f"universal_transfer:{_universal(0)['tranId']}"
    ]
    assert [k for k in live if k.startswith("margin_trade:")] == [
        f"margin_trade:AAASTB:{_trade(0)['id']}"
    ]


async def test_isolated_margin_reconciles_with_a_consistent_history(
    session, run, fake: FakeBinance, connector, clock: Clock
) -> None:
    _pairs(fake, ("AAA", "STB"))
    fake.set_rows("spot-account", _spot(("STB", "100")))
    fake.set_rows(ISOLATED_ACCOUNT, [_pair("AAASTB", ("AAA", "0"), ("STB", "10"))])
    connection = await binance_connection(run, config=later_config())
    first = await run_binance_sync(session, connection.id, fake.handler)
    assert first.status == "healthy", first.last_error

    start = clock.ms

    def hours(n: int) -> int:
        return start + n * HOUR_MS

    pair = {"isolatedSymbol": "AAASTB"}
    fake.add(
        TRANSFERS,
        _transfer(0, amount="5", transTo="ISOLATED_MARGIN", toSymbol="AAASTB", timestamp=hours(1)),
        when=pair,
    )
    fake.add(MARGIN_TRADES, _trade(0, isolated=True, qty="1", price="4", time=hours(2)))
    fake.add(
        INTEREST,
        _interest(0, interest="0.05", isolatedSymbol="AAASTB", interestAccuredTime=hours(3)),
        when=pair,
    )
    fake.set_rows("spot-account", _spot(("STB", "95")))
    fake.set_rows(ISOLATED_ACCOUNT, [_pair("AAASTB", ("AAA", "1"), ("STB", "10.95"))])
    notifier = RecordingNotifier()
    clock.ms += 6 * HOUR_MS
    after = await run_binance_sync(session, connection.id, fake.handler, notifier=notifier)

    assert after.status == "healthy", after.last_error
    assert await _differences(session, after.id) == []
    assert not [m for m in notifier.messages if _CHANGED in m]
    found = await holdings(session, after.id)
    # The opening of 10, the transfer in, the trade and the interest.
    assert (found[(ISOLATED, "AAA")], found[(ISOLATED, "STB")]) == (Decimal(1), Decimal("10.95"))
    assert sorted(k for k in await keys(session, after.id) if not k.startswith("opening:")) == [
        f"margin_interest:{_interest(0)['txId']}:{hours(3)}:STB",
        f"margin_trade:AAASTB:{_trade(0)['id']}",
        f"margin_transfer:{_transfer(0)['txId']}",
    ]


async def test_a_margin_round_trip_through_an_unseen_asset_is_found_by_the_sweep(
    session, run, fake: FakeBinance, connector, clock: Clock
) -> None:
    _pairs(fake, ("ZZZ", "STB"), ("AAA", "STB"))
    fake.set_rows(ACCOUNT, [_user_asset("STB", "100")])
    connection = await binance_connection(run, config=later_config())
    await run_binance_sync(session, connection.id, fake.handler)
    start = clock.ms
    # STB falls and rises through ZZZ, which no balance ever shows, on Cross Margin.
    fake.add(
        MARGIN_TRADES,
        _trade(0, symbol="ZZZSTB", qty="5", price="2", time=start + HOUR_MS),
        _trade(1, symbol="ZZZSTB", isBuyer=False, qty="5", price="2.4", time=start + 2 * HOUR_MS),
    )
    fake.set_rows(ACCOUNT, [_user_asset("STB", "102")])
    fake.requests.clear()
    clock.ms += 4 * HOUR_MS

    after = await run_binance_sync(session, connection.id, fake.handler)

    assert after.status == "healthy", after.last_error
    swept = [s for s in fake.calls(MARGIN_TRADES) if "startTime" in s.params]
    assert {s.params["symbol"] for s in swept} == {"AAASTB", "ZZZSTB"}
    # Spot's trades are not swept for a margin difference.
    assert [s for s in fake.calls("my-trades") if "startTime" in s.params] == []
    assert await _differences(session, after.id) == []
    [(code, _, wallet)] = after.config[SWEPT_KEY]
    assert (code, wallet) == ("STB", "cross_margin")
