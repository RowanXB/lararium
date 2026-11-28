"""Spot trades, converts and asset dividends in the ledger (design 5.7, plan Decision 10), and
regenerating the ledger from the raw records."""

from decimal import Decimal

import pytest

from lararium.modules.finance.binance.normalize import BinanceRecordError
from lararium.modules.finance.ledger import find_unbalanced_transactions

from .fixtures import BASE_TIME_MS, HOUR_MS, build_row
from .normalizing import (
    assert_balanced_and_quiet,
    at,
    holdings,
    keys,
    ledger,
    list_pairs,
    normalize_rows,
    plain,
    postings,
    run_normalize,
    transaction,
    typed,
)
from .syncing import binance_connection


def _trade(seq: int, *, buyer: bool, qty: str, price: str, quote_qty: str, **fields) -> dict:
    values = {
        "symbol": "AAABBB",
        "isBuyer": buyer,
        "qty": qty,
        "price": price,
        "quoteQty": quote_qty,
        "commission": "0",
        "commissionAsset": "BBB",
    }
    return build_row("my-trades", seq, **(values | fields))


async def _connection(run):
    connection = await binance_connection(run)
    await list_pairs(run, connection, ("AAABBB", "AAA", "BBB"))
    return connection


async def test_buy_and_sell(session, run) -> None:
    connection = await _connection(run)
    buy = _trade(
        0,
        buyer=True,
        qty="2",
        price="3.00000000",
        quote_qty="6",
        commission="0.01",
        commissionAsset="CCC",
    )
    sell = _trade(
        1,
        buyer=False,
        qty="1",
        price="3.00000000",
        quote_qty="3",
        commission="0.001",
        commissionAsset="AAA",
    )

    await normalize_rows(session, connection.id, ("my-trades", [buy, sell]))

    bought = await postings(session, f"spot_trade:AAABBB:{buy['id']}")
    assert bought == [
        ("Binance Spot", "AAA", Decimal(2), Decimal(3), "BBB", None, None, False, ""),
        ("Binance Spot", "BBB", Decimal(-6), None, None, None, None, False, ""),
        ("Binance Spot", "CCC", Decimal("-0.01"), None, None, None, None, True, "commission"),
        ("Unassigned", "CCC", Decimal("0.01"), None, None, None, None, False, ""),
    ]
    sold = await postings(session, f"spot_trade:AAABBB:{sell['id']}")
    assert sold == [
        ("Binance Spot", "AAA", Decimal(-1), None, None, Decimal(3), "BBB", False, ""),
        ("Binance Spot", "BBB", Decimal(3), None, None, None, None, False, ""),
        ("Binance Spot", "AAA", Decimal("-0.001"), None, None, None, None, True, "commission"),
        ("Unassigned", "AAA", Decimal("0.001"), None, None, None, None, False, ""),
    ]
    # The commission in the base asset reduces the base.
    found = await holdings(session, connection.id)
    assert found[("Binance Spot", "AAA")] == Decimal("0.999")
    assert found[("Binance Spot", "BBB")] == Decimal(-3)
    sold_at = await transaction(session, f"spot_trade:AAABBB:{sell['id']}")
    assert sold_at.occurred_at == at(BASE_TIME_MS + HOUR_MS)
    [first, second] = sorted(
        await typed(session, "binance_my_trades", connection.id), key=lambda r: r.id
    )
    assert first.transaction_id is not None and second.transaction_id is not None
    await assert_balanced_and_quiet(session, connection.id)


async def test_trade_rounding_residual(session, run) -> None:
    connection = await _connection(run)
    # qty times price is 12.345678; quoteQty is off in its last place.
    row = _trade(0, buyer=True, qty="100", price="0.12345678", quote_qty="12.3456782")

    await normalize_rows(session, connection.id, ("my-trades", [row]))

    found = await postings(session, f"spot_trade:AAABBB:{row['id']}")
    rounded = ("Unassigned", "BBB", Decimal("2E-7"), None, None, None, None, False, "rounding")
    assert found[-1] == rounded
    # A trade states no income.
    assert (await transaction(session, f"spot_trade:AAABBB:{row['id']}")).income_kind is None
    await assert_balanced_and_quiet(session, connection.id)


async def test_trade_beyond_rounding_fails_naming_its_key(session, run) -> None:
    connection = await _connection(run)
    row = _trade(0, buyer=False, qty="100", price="0.12345678", quote_qty="12.345680")

    with pytest.raises(BinanceRecordError) as caught:
        await normalize_rows(session, connection.id, ("my-trades", [row]))

    message = str(caught.value)
    assert f"spot_trade:AAABBB:{row['id']}" in message
    assert "12.34" not in message and "100" not in message.replace(str(row["id"]), "")


async def test_a_price_with_a_positive_exponent_allows_no_more_rounding(session, run) -> None:
    connection = await _connection(run)
    # 1E+3 is exact to the unit: 1001 against 1000 is beyond rounding.
    row = _trade(0, buyer=True, qty="1", price="1E+3", quote_qty="1001")

    with pytest.raises(BinanceRecordError):
        await normalize_rows(session, connection.id, ("my-trades", [row]))


async def test_trade_of_an_unlisted_symbol_is_flagged(session, run) -> None:
    connection = await _connection(run)
    row = _trade(0, buyer=True, qty="1", price="1", quote_qty="1", symbol="CCCDDD")

    report = await normalize_rows(session, connection.id, ("my-trades", [row]))

    assert await keys(session, connection.id) == []
    assert report.new_flags == ["symbol CCCDDD has trades exchangeInfo does not list"]
    assert len(await typed(session, "binance_my_trades", connection.id)) == 1


def _convert(seq: int = 0, **fields) -> dict:
    values = {
        "orderStatus": "SUCCESS",
        "walletType": "SPOT",
        "fromAsset": "AAA",
        "fromAmount": "3",
        "toAsset": "BBB",
        "toAmount": "2",
    }
    return build_row("convert", seq, **(values | fields))


async def test_convert_on_the_wallet_it_names(session, run) -> None:
    connection = await binance_connection(run)
    on_spot, on_funding = _convert(0), _convert(1, walletType="FUNDING")

    await normalize_rows(session, connection.id, ("convert", [on_spot, on_funding]))

    assert await postings(session, f"convert:{on_funding['orderId']}") == [
        ("Binance Funding", "AAA", Decimal(-3), None, None, None, None, False, ""),
        ("Binance Funding", "BBB", Decimal(2), Decimal("1.5"), "AAA", None, None, False, ""),
    ]
    assert plain(await postings(session, f"convert:{on_spot['orderId']}"))[0][0] == "Binance Spot"
    found = await transaction(session, f"convert:{on_spot['orderId']}")
    assert found.occurred_at == at(BASE_TIME_MS)  # createTime
    await assert_balanced_and_quiet(session, connection.id)


async def test_convert_at_a_ratio_with_no_end_balances(session, run) -> None:
    connection = await binance_connection(run)
    row = _convert(fromAmount="1", toAmount="3")

    await normalize_rows(session, connection.id, ("convert", [row]))

    [_, received] = await postings(session, f"convert:{row['orderId']}")
    assert received[3] == Decimal("0." + "3" * 30)
    assert await find_unbalanced_transactions(session) == []


async def test_convert_not_succeeded_is_flagged_and_not_posted(session, run) -> None:
    connection = await binance_connection(run)

    report = await normalize_rows(
        session,
        connection.id,
        ("convert", [_convert(0, orderStatus="PROCESS"), _convert(1, walletType="MARGIN")]),
    )

    assert await keys(session, connection.id) == []
    assert report.new_flags == ["convert.orderStatus=PROCESS", "convert.walletType=MARGIN"]


async def test_dividend_is_income_on_spot(session, run) -> None:
    connection = await binance_connection(run)
    row = build_row("dividends", asset="AAA", amount="0.5", direction=1)

    await normalize_rows(session, connection.id, ("dividends", [row]))

    key = f"dividend:{row['id']}"
    assert plain(await postings(session, key)) == [
        ("Binance Spot", "AAA", Decimal("0.5")),
        ("Unassigned", "AAA", Decimal("-0.5")),
    ]
    assert (await transaction(session, key)).occurred_at == at(BASE_TIME_MS)  # divTime
    assert (await transaction(session, key)).income_kind == "reward"
    await assert_balanced_and_quiet(session, connection.id)


async def test_ld_asset_is_typed_and_not_posted(session, run) -> None:
    connection = await binance_connection(run)
    # AAA is a known asset, so LDAAA is its Earn mirror.
    await normalize_rows(session, connection.id, ("dividends", [build_row("dividends", 0)]))
    mirror = build_row("dividends", 1, asset="LDAAA")

    await normalize_rows(session, connection.id, ("dividends", [mirror]))

    assert f"dividend:{mirror['id']}" not in await keys(session, connection.id)
    assert len(await typed(session, "binance_dividends", connection.id)) == 2


async def test_regenerate_gives_the_same_ledger(session, run) -> None:
    connection = await _connection(run)
    deposit = build_row("deposits", coin="AAA", amount="5", walletType=0, transferType=0, status=1)
    withdrawal = build_row(
        "withdrawals", coin="AAA", amount="1", transactionFee="0.1", walletType=1, status=4
    )
    trade = _trade(0, buyer=True, qty="100", price="0.12345678", quote_qty="12.3456782")
    reward = build_row("earn-flexible-rewards", asset="BBB", rewards="0.2", type="BONUS")
    dividend = build_row("dividends", asset="BBB", amount="0.2", direction=1)
    await normalize_rows(
        session,
        connection.id,
        ("deposits", [deposit]),
        ("withdrawals", [withdrawal]),
        ("my-trades", [trade]),
        ("earn-flexible-rewards", [reward, reward]),
        ("convert", [_convert()]),
    )
    await normalize_rows(
        session,
        connection.id,
        ("withdrawals", [{**withdrawal, "status": 5}]),
        ("dividends", [dividend]),
    )
    before = await ledger(session, connection.id)

    report = await run_normalize(session, connection.id, again=True)

    assert report.records == 8
    assert await ledger(session, connection.id) == before
    await assert_balanced_and_quiet(session, connection.id)


async def _uncounted_currency(run, connection, code: str) -> None:
    """Binance marks `code` legal money; Lararium has no currency of it."""
    from lararium.modules.finance.binance.parse import parse
    from lararium.modules.finance.binance.tables import records, store

    from .fixtures import NOW_MS, build_response
    from .test_balances import _at

    row = build_row("coin-config", coin=code, isLegalMoney=True)
    parsed = parse("coin-config", build_response("coin-config", [row]))
    key = {"connection_id": connection.id, "snapshot_at": _at(NOW_MS)}
    for record in records("coin-config", parsed):
        await run(store, "coin-config", key, record, {"read_to": _at(NOW_MS)})


@pytest.mark.parametrize("side", ["quote", "commission", "convert"])
async def test_a_record_naming_a_currency_lararium_lacks_is_typed_and_not_posted(
    session, run, side: str
) -> None:
    # One side that is not counted: the record is not posted at all, never one-sided.
    connection = await binance_connection(run)
    await list_pairs(run, connection, ("AAABBB", "AAA", "BBB"), ("AAAXFA", "AAA", "XFA"))
    await _uncounted_currency(run, connection, "XFA")
    if side == "convert":
        label = "convert"
        row = build_row(
            label,
            fromAsset="XFA",
            toAsset="AAA",
            fromAmount="100",
            toAmount="1",
            orderStatus="SUCCESS",
            walletType="SPOT",
        )
    else:
        label = "my-trades"
        fields = {"symbol": "AAAXFA", "commissionAsset": "XFA"} if side == "quote" else {}
        if side == "commission":
            fields = {"commissionAsset": "XFA"}
        row = _trade(
            0, buyer=True, qty="1", price="100", quote_qty="100", commission="0.5", **fields
        )

    report = await normalize_rows(session, connection.id, (label, [row]))

    assert await keys(session, connection.id) == []
    assert "XFA is a currency Lararium does not have: it is not counted" in report.new_flags
    table = "binance_convert" if label == "convert" else "binance_my_trades"
    assert len(await typed(session, table, connection.id)) == 1
