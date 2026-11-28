"""Fiat by bank, card purchases and sales, P2P orders, Pay and rebates in the ledger (design 5.7,
5.12): a fiat order and the fiat leg of a card payment or a P2P order are transfer legs, which
pair with the bank's (or the card's) row; the swaps, fees, Pay and rebates never are."""

from decimal import Decimal

import pytest
from sqlalchemy import text

from lararium.core.connections import create_connection
from lararium.modules.finance.accounts import create_account
from lararium.modules.finance.binance.catalogue import ENDPOINTS
from lararium.modules.finance.binance.fetch import (
    FIAT_PRODUCTS,
    HISTORY_LABELS,
    PHASE_B_BUILT,
    windows,
)
from lararium.modules.finance.binance.normalize import BinanceRecordError
from lararium.modules.finance.binance.parse import parse
from lararium.modules.finance.binance.records import unit_cost
from lararium.modules.finance.binance.tables import records, store
from lararium.modules.finance.identifiers import assign_identifier
from lararium.modules.finance.instruments import get_currency
from lararium.modules.finance.ledger import (
    CounterpartyFields,
    PostingInput,
    TransactionInput,
    upsert_connector_transaction,
)
from lararium.modules.finance.transfers import (
    find_transfer_violations,
    pair_records,
    transfer_contra,
)
from lararium.modules.finance.transfers.queries import list_transfers

from .fixtures import BASE_TIME_MS, DAY_MS, HOUR_MS, NOW_MS, FakeBinance, build_response, build_row
from .normalizing import (
    assert_balanced_and_quiet,
    at,
    card_payment,
    differences,
    heal,
    holdings,
    ingest,
    keys,
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
    funding_rows,
    later_config,
    run_binance_sync,
    spot_rows,
)

ORDERS = "fiat-orders"
PAYMENTS = "fiat-payments"
C2C = "c2c"
PAY = "pay"
REBATES = "rebate"
DEPOSIT, WITHDRAW = {"transactionType": "0"}, {"transactionType": "1"}
BUY, SELL = {"transactionType": "0"}, {"transactionType": "1"}
B_IDENTIFIER = "112233:12345678"
_CHANGED = "changed by something no record explains"


def _order(seq: int = 0, **fields) -> dict:
    values = {
        "orderNo": f"order-{seq}",
        "fiatCurrency": "EUR",
        "indicatedAmount": "100",
        "amount": "100",
        "totalFee": "0",
        "method": "BankAccount",
        "status": "Successful",
        "createTime": BASE_TIME_MS,
        "updateTime": BASE_TIME_MS,
    }
    return build_row(ORDERS, seq, **(values | fields))


def _payment(seq: int = 0, **fields) -> dict:
    # Binance's example: 20 EUR paid, a fee of 0.2, 4.462 bought at 4.437472.
    values = {
        "orderNo": f"payment-{seq}",
        "sourceAmount": "20.0",
        "fiatCurrency": "EUR",
        "obtainAmount": "4.462",
        "cryptoCurrency": "AAA",
        "totalFee": "0.2",
        "price": "4.437472",
        "status": "Completed",
        "paymentMethod": "Credit Card",
        "createTime": BASE_TIME_MS,
        "updateTime": BASE_TIME_MS,
    }
    return build_row(PAYMENTS, seq, **(values | fields))


def _p2p(seq: int = 0, **fields) -> dict:
    values = {
        "orderNumber": f"p2p-{seq}",
        "tradeType": "BUY",
        "asset": "BBB",
        "fiat": "EUR",
        "amount": "2",
        "totalPrice": "50",
        "unitPrice": "25",
        "orderStatus": "COMPLETED",
        "commission": "0",
        "createTime": BASE_TIME_MS,
    }
    return build_row(C2C, seq, **(values | fields))


def _pay(seq: int = 0, *details: tuple[str, str], **fields) -> dict:
    """A Pay row; each (currency, amount) element drawn whole from the row's `walletType`."""
    wallet = str(fields.get("walletType", 1))
    values = {
        "transactionId": f"pay-{seq}",
        "orderType": "PAY",
        "transactionTime": BASE_TIME_MS,
        "amount": "-3",
        "currency": "AAA",
        "walletType": 1,
        "fundsDetail": [
            {"currency": currency, "amount": quantity, "walletAssetCost": {wallet: quantity}}
            for currency, quantity in (details or (("AAA", "3"),))
        ],
    }
    return build_row(PAY, seq, **(values | fields))


def _rebate(seq: int = 0, **fields) -> dict:
    values = {"asset": "AAA", "type": 1, "amount": "0.25", "updateTime": BASE_TIME_MS}
    return build_row(REBATES, seq, **(values | fields))


async def _legal_money(run, connection, code: str = "EUR") -> None:
    """Binance marks `code` legal money (its coin-config read), so it is that currency."""
    row = build_row("coin-config", coin=code, isLegalMoney=True)
    parsed = parse("coin-config", build_response("coin-config", [row]))
    key = {"connection_id": connection.id, "snapshot_at": at(NOW_MS)}
    for record in records("coin-config", parsed):
        await run(store, "coin-config", key, record, {"read_to": at(NOW_MS)})


async def _connection(run):
    connection = await binance_connection(run)
    await _legal_money(run, connection)
    return connection


async def _normalize(session, connection_id, label: str, rows, request=None):
    await ingest(session, connection_id, label, rows, request=request)
    return await run_normalize(session, connection_id)


async def _never_transfer(session, key: str) -> bool:
    return await session.scalar(
        text("select never_transfer from finance.transactions where external_id = :e"), {"e": key}
    )


async def _bank_transfer(session, key: str) -> bool:
    return await session.scalar(
        text("select bank_transfer from finance.transactions where external_id = :e"), {"e": key}
    )


async def _account(session, name: str):
    return await session.scalar(
        text("select id from finance.accounts where name = :n"), {"n": name}
    )


async def _id(session, key: str):
    return (await transaction(session, key)).id


async def _bank_row(session, run, quantity: str, occurred_at, *, identifier_id=None):
    """A bank connection's payment (or receipt) of EUR, a bank transfer naming the account
    `identifier_id` is (none: a stranger's); its contra from `transfer_contra`; its id."""
    bank = await run(create_connection, connector="monzo", display_name="Bank", config={})
    account = await run(
        create_account,
        name="Bank",
        ledger_type="asset",
        subtype="bank",
        balance_mode="transactions",
        source_connection_id=bank.id,
    )
    eur = (await get_currency(session, "EUR")).id
    contra = await transfer_contra(
        session,
        source=("connection", f"{bank.id}:bank_1"),
        own_account_id=account.id,
        counterparty_identifier_id=identifier_id,
        other_postings=0,
    )
    amount = Decimal(quantity)
    record, _ = await run(
        upsert_connector_transaction,
        TransactionInput(
            occurred_at=occurred_at,
            description="Transfer",
            origin="connector",
            connection_id=bank.id,
            external_id="bank_1",
            postings=[
                PostingInput(account.id, eur, amount),
                PostingInput(contra, eur, -amount),
            ],
            counterparty=CounterpartyFields(identifier_id=identifier_id, bank_transfer=True),
        ),
    )
    return record.id


# The family.


def test_the_family_is_walked() -> None:
    assert set(FIAT_PRODUCTS) == {ORDERS, PAYMENTS, C2C, PAY, REBATES}
    assert set(FIAT_PRODUCTS) <= set(PHASE_B_BUILT)
    assert set(FIAT_PRODUCTS) <= set(HISTORY_LABELS)


def test_rebate_history_starts_at_its_earliest_supported_date() -> None:
    # "The earliest supported startTime is June 10, 2020": no window older is asked.
    earliest = at(1_591_747_200_000)
    assert (earliest.year, earliest.month, earliest.day, earliest.hour) == (2020, 6, 10, 0)
    pieces = windows(ENDPOINTS[REBATES], None, NOW_MS)
    assert min(start for start, _ in pieces) == 1_591_747_200_000
    assert all(end - start <= 29 * DAY_MS for start, end in pieces)
    # The others keep their documented reach: P2P six months, Pay 18 (in 30 and 90 days).
    assert min(s for s, _ in windows(ENDPOINTS[C2C], None, NOW_MS)) > NOW_MS - 181 * DAY_MS
    assert min(s for s, _ in windows(ENDPOINTS[PAY], None, NOW_MS)) > NOW_MS - 541 * DAY_MS
    # A fiat order or card payment has no documented span: one window.
    assert len(windows(ENDPOINTS[ORDERS], None, NOW_MS)) == 1


# Fiat orders.


async def test_fiat_deposit_and_withdrawal_on_spot(session, run) -> None:
    connection = await _connection(run)
    # A deposit: Binance credits `amount`, after its fee (typed only).
    deposit = _order(0, indicatedAmount="101", amount="100", totalFee="1")
    # A withdrawal: `amount` leaves for the bank, the fee is a transaction of its own.
    withdrawal = _order(
        1, amount="30", indicatedAmount="31", totalFee="1", createTime=BASE_TIME_MS + HOUR_MS
    )

    await ingest(session, connection.id, ORDERS, [deposit], request=DEPOSIT)
    await _normalize(session, connection.id, ORDERS, [withdrawal], request=WITHDRAW)

    key = f"fiat_order:{deposit['orderNo']}"
    out = f"fiat_order:{withdrawal['orderNo']}"
    assert plain(await postings(session, key)) == [
        ("Binance Spot", "EUR", Decimal(100)),
        ("Unassigned", "EUR", Decimal(-100)),
    ]
    assert plain(await postings(session, out)) == [
        ("Binance Spot", "EUR", Decimal(-30)),
        ("Unassigned", "EUR", Decimal(30)),
    ]
    assert [p[7] for p in await postings(session, f"{out}:fee")] == [True, False]
    assert plain(await postings(session, f"{out}:fee")) == [
        ("Binance Spot", "EUR", Decimal(-1)),
        ("Unassigned", "EUR", Decimal(1)),
    ]
    assert await keys(session, connection.id) == sorted([key, out, f"{out}:fee"])
    # Both are bank transfer legs; the fee is never a transfer.
    for leg in (key, out):
        assert (await _never_transfer(session, leg), await _bank_transfer(session, leg)) == (
            False,
            True,
        )
    assert await _never_transfer(session, f"{out}:fee") is True
    rows = {r.order_no: r for r in await typed(session, "binance_fiat_orders", connection.id)}
    assert rows[deposit["orderNo"]].transaction_type == "0"
    assert rows[withdrawal["orderNo"]].transaction_type == "1"
    assert rows[deposit["orderNo"]].transaction_id == await _id(session, key)
    await assert_balanced_and_quiet(session, connection.id)


async def test_pending_fiat_order_posts_nothing(session, run) -> None:
    connection = await _connection(run)
    pending = _order(0, status="Processing")

    await _normalize(session, connection.id, ORDERS, [pending], request=DEPOSIT)

    assert await keys(session, connection.id) == []
    [row] = await typed(session, "binance_fiat_orders", connection.id)
    assert (row.status, row.transaction_id) == ("Processing", None)
    await assert_balanced_and_quiet(session, connection.id)

    # Credited later: posted then, at its last update.
    credited_ms = BASE_TIME_MS + 2 * HOUR_MS
    await _normalize(
        session,
        connection.id,
        ORDERS,
        [{**pending, "status": "Successful", "updateTime": credited_ms}],
        request=DEPOSIT,
    )

    key = f"fiat_order:{pending['orderNo']}"
    assert await keys(session, connection.id) == [key]
    assert (await transaction(session, key)).occurred_at == at(credited_ms)
    await assert_balanced_and_quiet(session, connection.id)


async def test_fiat_deposit_pairs_with_an_identified_bank_row(session, run) -> None:
    # The owner registered the bank account Binance's fiat channel pays from on the Spot
    # account; the bank's row names it; Binance credits two days later.
    connection = await _connection(run)
    deposit = _order(0, createTime=BASE_TIME_MS, updateTime=BASE_TIME_MS + 2 * DAY_MS)
    await _normalize(session, connection.id, ORDERS, [deposit], request=DEPOSIT)
    spot = await _account(session, "Binance Spot")
    identifier = await run(assign_identifier, spot, "uk_bank_account", B_IDENTIFIER)
    sent = await _bank_row(session, run, "-100", at(BASE_TIME_MS), identifier_id=identifier.id)

    await heal(session)

    key = f"fiat_order:{deposit['orderNo']}"
    [transfer] = await list_transfers(session)
    assert transfer.status == "matched"
    assert (transfer.out_transaction_id, transfer.in_transaction_id) == (
        sent,
        await _id(session, key),
    )
    assert plain(await postings(session, key))[1][0] == "In transit"
    assert await find_transfer_violations(session) == []
    assert await heal(session) == 0
    await assert_balanced_and_quiet(session, connection.id)


async def test_fiat_order_in_a_currency_lararium_lacks_is_typed_and_not_posted(
    session, run
) -> None:
    connection = await _connection(run)
    await _legal_money(run, connection, "XFA")

    report = await _normalize(
        session, connection.id, ORDERS, [_order(0, fiatCurrency="XFA")], request=DEPOSIT
    )

    assert await keys(session, connection.id) == []
    assert "XFA is a currency Lararium does not have: it is not counted" in report.new_flags
    assert len(await typed(session, "binance_fiat_orders", connection.id)) == 1


# Card purchases and sales.


async def test_card_purchase_and_sale(session, run) -> None:
    connection = await _connection(run)
    bought = _payment(0)
    sold = _payment(
        1,
        sourceAmount="9",
        obtainAmount="2",
        totalFee="1",
        price="5",
        paymentMethod=None,
        createTime=BASE_TIME_MS + HOUR_MS,
    )

    await ingest(session, connection.id, PAYMENTS, [bought], request=BUY)
    await _normalize(session, connection.id, PAYMENTS, [sold], request=SELL)

    key = f"fiat_payment:{bought['orderNo']}"
    assert plain(await postings(session, f"{key}:funding")) == [
        ("Binance Spot", "EUR", Decimal(20)),
        ("Unassigned", "EUR", Decimal(-20)),
    ]
    # The fiat paid after the fee buys the crypto, weighed by what it cost: 19.8 / 4.462, which
    # Binance's `price` (typed only) rounds to 4.437472.
    swap = await postings(session, key)
    cost = Decimal("19.8") / Decimal("4.462")
    assert cost.quantize(Decimal("0.000001")) == Decimal("4.437472")
    assert [r[:5] for r in swap] == [
        (
            "Binance Spot",
            "AAA",
            Decimal("4.462"),
            unit_cost(Decimal("19.8"), Decimal("4.462")),
            "EUR",
        ),
        ("Binance Spot", "EUR", Decimal("-19.8"), None, None),
        ("Binance Spot", "EUR", Decimal("-0.2"), None, None),
        ("Unassigned", "EUR", Decimal("0.2"), None, None),
    ]
    assert swap[2][7] is True
    out = f"fiat_payment:{sold['orderNo']}"
    # A sale's `sourceAmount` is what the card received, after the fee.
    assert [r[:2] + r[2:3] + r[5:7] for r in await postings(session, out)] == [
        ("Binance Spot", "AAA", Decimal(-2), Decimal(5), "EUR"),
        ("Binance Spot", "EUR", Decimal(10), None, None),
        ("Binance Spot", "EUR", Decimal(-1), None, None),
        ("Unassigned", "EUR", Decimal(1), None, None),
    ]
    assert plain(await postings(session, f"{out}:funding")) == [
        ("Binance Spot", "EUR", Decimal(-9)),
        ("Unassigned", "EUR", Decimal(9)),
    ]
    held = await holdings(session, connection.id)
    assert ("Binance Spot", "EUR") not in held  # the fiat came from and went to the card
    assert held[("Binance Spot", "AAA")] == Decimal("2.462")
    # Only the fiat legs can pair, and only through a card merchant: no bank transfer.
    for leg in (f"{key}:funding", f"{out}:funding"):
        assert (await _never_transfer(session, leg), await _bank_transfer(session, leg)) == (
            False,
            False,
        )
    for swap_key in (key, out):
        assert await _never_transfer(session, swap_key) is True
    rows = {r.order_no: r for r in await typed(session, "binance_fiat_payments", connection.id)}
    assert rows[bought["orderNo"]].transaction_id == await _id(session, key)
    assert rows[sold["orderNo"]].transaction_type == "1"
    await assert_balanced_and_quiet(session, connection.id)


async def test_card_purchase_from_cash_balance_is_only_a_swap(session, run) -> None:
    connection = await _connection(run)
    paid = _payment(0, paymentMethod="Cash Balance")

    await _normalize(session, connection.id, PAYMENTS, [paid], request=BUY)

    key = f"fiat_payment:{paid['orderNo']}"
    assert await keys(session, connection.id) == [key]
    held = await holdings(session, connection.id)
    assert held[("Binance Spot", "EUR")] == Decimal(-20)
    assert held[("Binance Spot", "AAA")] == Decimal("4.462")
    await assert_balanced_and_quiet(session, connection.id)


async def test_card_purchase_pairs_with_a_card_payment_to_binances_merchant(session, run) -> None:
    connection = await _connection(run)
    bought = _payment(0)
    await _normalize(session, connection.id, PAYMENTS, [bought], request=BUY)
    spot = await _account(session, "Binance Spot")
    eur = (await get_currency(session, "EUR")).id
    leg = f"fiat_payment:{bought['orderNo']}:funding"

    await card_payment(session, run, spot, eur, Decimal(20), at(BASE_TIME_MS))
    await heal(session)

    [transfer] = await list_transfers(session)
    assert transfer.in_transaction_id == await _id(session, leg)
    assert plain(await postings(session, leg))[1][0] == "In transit"
    assert await find_transfer_violations(session) == []


async def test_pending_card_payment_posts_nothing_and_a_refund_unposts(session, run) -> None:
    connection = await _connection(run)
    paid = _payment(0)
    await _normalize(session, connection.id, PAYMENTS, [{**paid, "status": "Processing"}], BUY)
    assert await keys(session, connection.id) == []

    await _normalize(session, connection.id, PAYMENTS, [paid], BUY)
    assert len(await keys(session, connection.id)) == 2
    await _normalize(session, connection.id, PAYMENTS, [{**paid, "status": "Refunded"}], BUY)

    assert await keys(session, connection.id) == []
    [row] = await typed(session, "binance_fiat_payments", connection.id)
    assert row.transaction_id is None
    await assert_balanced_and_quiet(session, connection.id)


# P2P.


async def test_p2p_buy_and_sell_on_funding(session, run) -> None:
    connection = await _connection(run)
    bought = _p2p(0, commission="0.01")
    # 1.6 at 0.75 is 1.2: a total of 1.21 is within what a price of two decimals rounds.
    sold = _p2p(
        1,
        tradeType="SELL",
        amount="1.6",
        unitPrice="0.75",
        totalPrice="1.21",
        createTime=BASE_TIME_MS + HOUR_MS,
    )

    await _normalize(session, connection.id, C2C, [bought, sold])

    key = f"c2c:{bought['orderNumber']}"
    assert plain(await postings(session, f"{key}:funding")) == [
        ("Binance Funding", "EUR", Decimal(50)),
        ("Unassigned", "EUR", Decimal(-50)),
    ]
    assert [r[:5] for r in await postings(session, key)] == [
        ("Binance Funding", "BBB", Decimal(2), Decimal(25), "EUR"),
        ("Binance Funding", "EUR", Decimal(-50), None, None),
        ("Binance Funding", "BBB", Decimal("-0.01"), None, None),
        ("Unassigned", "BBB", Decimal("0.01"), None, None),
    ]
    out = f"c2c:{sold['orderNumber']}"
    assert plain(await postings(session, out)) == [
        ("Binance Funding", "BBB", Decimal("-1.6")),
        ("Binance Funding", "EUR", Decimal("1.21")),
        ("Unassigned", "EUR", Decimal("-0.01")),
    ]
    assert (await postings(session, out))[2][8] == "rounding"
    assert plain(await postings(session, f"{out}:funding")) == [
        ("Binance Funding", "EUR", Decimal("-1.21")),
        ("Unassigned", "EUR", Decimal("1.21")),
    ]
    for leg in (f"{key}:funding", f"{out}:funding"):
        assert (await _never_transfer(session, leg), await _bank_transfer(session, leg)) == (
            False,
            True,
        )
    assert await _never_transfer(session, key) is True
    held = await holdings(session, connection.id)
    assert ("Binance Funding", "EUR") not in held
    assert held[("Binance Funding", "BBB")] == Decimal("0.39")
    rows = {r.order_number: r for r in await typed(session, "binance_c2c", connection.id)}
    assert rows[bought["orderNumber"]].transaction_id == await _id(session, key)
    await assert_balanced_and_quiet(session, connection.id)


async def test_p2p_beyond_rounding_fails_naming_its_key(session, run) -> None:
    connection = await _connection(run)
    order = _p2p(0, totalPrice="52")

    with pytest.raises(BinanceRecordError) as caught:
        await _normalize(session, connection.id, C2C, [order])

    assert caught.value.key == f"c2c:{order['orderNumber']}"
    assert "52" not in str(caught.value)


async def test_p2p_order_not_completed_posts_nothing(session, run) -> None:
    connection = await _connection(run)
    paid = _p2p(0, orderStatus="BUYER_PAYED")

    await _normalize(session, connection.id, C2C, [paid, _p2p(1, orderStatus="CANCELLED")])

    assert await keys(session, connection.id) == []
    assert len(await typed(session, "binance_c2c", connection.id)) == 2


async def test_p2p_buy_nets_to_zero_on_unassigned(session, run) -> None:
    # The bank paid a stranger's account: nothing pairs, and the bank's outflow and Binance's
    # fiat credit cancel on Unassigned.
    connection = await _connection(run)
    order = _p2p(0)
    await _normalize(session, connection.id, C2C, [order])
    await _bank_row(session, run, "-50", at(BASE_TIME_MS - HOUR_MS))

    await heal(session)

    assert await list_transfers(session) == []
    unassigned = await session.scalar(
        text(
            "select sum(p.quantity) from finance.postings p"
            " join finance.transactions t on t.id = p.transaction_id"
            " join finance.accounts a on a.id = p.account_id"
            " join finance.instruments i on i.id = p.instrument_id"
            " where a.name = 'Unassigned' and i.symbol = 'EUR' and t.status <> 'void'"
        )
    )
    assert unassigned == 0
    assert await find_transfer_violations(session) == []


async def test_owner_can_pair_a_p2p_order_with_a_bank_row(session, run) -> None:
    connection = await _connection(run)
    order = _p2p(0)
    await _normalize(session, connection.id, C2C, [order])
    sent = await _bank_row(session, run, "-50", at(BASE_TIME_MS - HOUR_MS))
    leg = f"c2c:{order['orderNumber']}:funding"
    await heal(session)

    await run(pair_records, sent, await _id(session, leg))
    # A later run (a regenerate replays every record) and heal keep the owner's pair.
    await run_normalize(session, connection.id, again=True)
    await heal(session)

    [transfer] = await list_transfers(session)
    assert transfer.status == "matched"
    assert {transfer.out_transaction_id, transfer.in_transaction_id} == {
        sent,
        await _id(session, leg),
    }
    assert plain(await postings(session, leg))[1][0] == "In transit"
    assert await _never_transfer(session, leg) is False
    assert await find_transfer_violations(session) == []
    assert await heal(session) == 0


# Pay.


async def test_pay_takes_each_funds_detail_from_its_wallet(session, run) -> None:
    connection = await _connection(run)
    paid = _pay(0, ("AAA", "3"), ("BBB", "0.5"), amount="-4")
    received = _pay(1, amount="7", currency="AAA", walletType=2, fundsDetail=[])
    refunded = _pay(2, ("AAA", "1"), orderType="PAY_REFUND", amount="1", walletType=2)

    await _normalize(session, connection.id, PAY, [paid, received, refunded])

    assert plain(await postings(session, f"pay:{paid['transactionId']}")) == [
        ("Binance Funding", "AAA", Decimal(-3)),
        ("Binance Funding", "BBB", Decimal("-0.5")),
        ("Unassigned", "AAA", Decimal(3)),
        ("Unassigned", "BBB", Decimal("0.5")),
    ]
    assert plain(await postings(session, f"pay:{received['transactionId']}")) == [
        ("Binance Spot", "AAA", Decimal(7)),
        ("Unassigned", "AAA", Decimal(-7)),
    ]
    assert plain(await postings(session, f"pay:{refunded['transactionId']}")) == [
        ("Binance Spot", "AAA", Decimal(1)),
        ("Unassigned", "AAA", Decimal(-1)),
    ]
    assert (await transaction(session, f"pay:{refunded['transactionId']}")).description == (
        "Pay PAY_REFUND"
    )
    assert await _never_transfer(session, f"pay:{paid['transactionId']}") is True
    [detail] = [
        r
        for r in await typed(session, "binance_pay", connection.id)
        if r.pay_transaction_id == paid["transactionId"]
    ]
    assert detail.transaction_id == await _id(session, f"pay:{paid['transactionId']}")
    costs = await session.scalar(
        text(
            "select count(*) from finance.binance_pay_funds_detail_wallet_asset_cost"
            " where connection_id = :c"
        ),
        {"c": connection.id},
    )
    assert costs == 3
    await assert_balanced_and_quiet(session, connection.id)


async def test_a_combined_pay_payment_is_taken_from_each_wallet_it_drew_on(session, run) -> None:
    # `walletAssetCost`: "cost from that wallet", by wallet type (1 Funding, 2 Spot, 5 Earn).
    connection = await _connection(run)
    split = _pay(
        0,
        amount="-4",
        walletType=1,
        walletTypes=[1, 2, 5],
        fundsDetail=[
            {"currency": "AAA", "amount": "3", "walletAssetCost": {"1": "1", "2": "2"}},
            {"currency": "BBB", "amount": "1", "walletAssetCost": {"5": "1"}},
        ],
    )
    # No costs: the row's own wallet; type 3 (fiat) is held on Spot.
    fiat = _pay(
        1,
        amount="-5",
        currency="EUR",
        walletType=3,
        fundsDetail=[{"currency": "EUR", "amount": "5", "walletAssetCost": {}}],
    )

    await _normalize(session, connection.id, PAY, [split, fiat])

    assert plain(await postings(session, f"pay:{split['transactionId']}")) == [
        ("Binance Funding", "AAA", Decimal(-1)),
        ("Binance Spot", "AAA", Decimal(-2)),
        ("Binance Earn", "BBB", Decimal(-1)),
        ("Unassigned", "AAA", Decimal(3)),
        ("Unassigned", "BBB", Decimal(1)),
    ]
    assert plain(await postings(session, f"pay:{fiat['transactionId']}")) == [
        ("Binance Spot", "EUR", Decimal(-5)),
        ("Unassigned", "EUR", Decimal(5)),
    ]
    await assert_balanced_and_quiet(session, connection.id)


_COSTS_FLAG = "pay.fundsDetail.walletAssetCost"


@pytest.mark.parametrize(
    ("costs", "flag"),
    [
        ({"1": "1", "2": "1"}, f"{_COSTS_FLAG} does not make the amount"),
        ({"7": "3"}, f"{_COSTS_FLAG} names a wallet type not known"),
        # Only ASCII digits name a wallet type: an Arabic-Indic three is none.
        ({"\u0663": "3"}, f"{_COSTS_FLAG} names a wallet type not known"),
        ({"2": None}, f"{_COSTS_FLAG} has a cost missing"),
        ({"2": "-3"}, f"{_COSTS_FLAG} has a negative cost"),
    ],
)
async def test_pay_costs_that_cannot_say_where_it_came_from_leave_it_on_the_row_wallet(
    session, run, costs, flag
) -> None:
    # Never left unposted: the whole element is on the row's own wallet (1, Funding), flagged.
    connection = await _connection(run)
    row = _pay(0, fundsDetail=[{"currency": "AAA", "amount": "3", "walletAssetCost": costs}])

    report = await _normalize(session, connection.id, PAY, [row])

    assert flag in report.new_flags
    assert plain(await postings(session, f"pay:{row['transactionId']}")) == [
        ("Binance Funding", "AAA", Decimal(-3)),
        ("Unassigned", "AAA", Decimal(3)),
    ]
    await assert_balanced_and_quiet(session, connection.id)


async def test_a_zero_pay_cost_posts_nothing_on_its_wallet(session, run) -> None:
    connection = await _connection(run)
    costs = {"1": "3", "2": "0"}
    row = _pay(0, fundsDetail=[{"currency": "AAA", "amount": "3", "walletAssetCost": costs}])

    report = await _normalize(session, connection.id, PAY, [row])

    assert report.new_flags == []
    assert plain(await postings(session, f"pay:{row['transactionId']}")) == [
        ("Binance Funding", "AAA", Decimal(-3)),
        ("Unassigned", "AAA", Decimal(3)),
    ]
    assert "Binance Spot" not in {
        r.name for r in await session.execute(text("select name from finance.accounts"))
    }


async def _refund(session, key: str) -> bool:
    return await session.scalar(
        text("select refund from finance.transactions where external_id = :e"), {"e": key}
    )


async def test_only_a_pay_refund_is_marked_a_refund(session, run) -> None:
    # Card refunds design 4.1: money back from a merchant. A Pay refund is; a red packet or a
    # P2P holding given back, a payment, a card sale's payout and a card purchase are not.
    connection = await _connection(run)
    refunded = _pay(0, ("AAA", "1"), orderType="PAY_REFUND", amount="1")
    returned = _pay(1, ("AAA", "1"), orderType="CRYPTO_BOX_RF", amount="1")
    paid = _pay(2)
    await ingest(session, connection.id, PAY, [refunded, returned, paid])
    await ingest(session, connection.id, PAYMENTS, [_payment(0)], request=BUY)
    await _normalize(
        session, connection.id, PAYMENTS, [_payment(1, paymentMethod=None)], request=SELL
    )

    marked = {k for k in await keys(session, connection.id) if await _refund(session, k)}
    assert marked == {f"pay:{refunded['transactionId']}"}
    # No Pay order or card payment states an income kind: money from others is the owner's to
    # categorise, and a refund is negative spending.
    for key in await keys(session, connection.id):
        assert (await transaction(session, key)).income_kind is None, key
    await assert_balanced_and_quiet(session, connection.id)


# Rebates.


async def test_rebate_is_income_on_spot_unless_a_dividend_paid_it(session, run) -> None:
    connection = await _connection(run)
    alone = _rebate(0, type=1)
    paid = _rebate(1, type=2, amount="0.7", asset="BBB")
    dividend = build_row(
        "dividends", asset="BBB", amount="0.7", direction=1, divTime=BASE_TIME_MS + 2 * HOUR_MS
    )

    await _normalize(session, connection.id, REBATES, [alone, paid])
    rebate_keys = [k for k in await keys(session, connection.id) if k.startswith("rebate:")]
    assert len(rebate_keys) == 2
    for key in rebate_keys:
        assert await _never_transfer(session, key) is True
    await _normalize(session, connection.id, "dividends", [dividend])

    held = await holdings(session, connection.id)
    assert held[("Binance Spot", "AAA")] == Decimal("0.25")
    # Posted once, as the dividend.
    assert held[("Binance Spot", "BBB")] == Decimal("0.7")
    rows = {r.asset: r for r in await typed(session, "binance_rebate", connection.id)}
    assert rows["BBB"].dividend_id == str(dividend["id"])
    assert rows["BBB"].transaction_id is None
    assert rows["AAA"].transaction_id is not None
    assert rows["AAA"].update_time == at(BASE_TIME_MS)
    # A rebate is a reward, as the dividend that pays one is (income categories design 4.3).
    [key] = [k for k in await keys(session, connection.id) if k.startswith("rebate:")]
    assert (await transaction(session, key)).income_kind == "reward"
    await assert_balanced_and_quiet(session, connection.id)


# Reconciliation: Spot and Funding with a consistent history of the family's records.


async def test_spot_and_funding_reconcile_with_fiat_cards_p2p_pay_and_rebates(
    session, run, fake: FakeBinance, connector, clock: Clock
) -> None:
    fake.add("coin-config", build_row("coin-config", coin="EUR", isLegalMoney=True))
    fake.set_rows("spot-account", spot_rows(("AAA", "10"), ("EUR", "50")))
    fake.set_rows("funding-asset", funding_rows(("BBB", "1")))
    connection = await binance_connection(run, config=later_config())
    first = await run_binance_sync(session, connection.id, fake.handler)
    assert first.status == "healthy", first.last_error
    start = clock.ms

    def moment(hours: int) -> dict:
        return {"createTime": start + hours * HOUR_MS}

    fake.add(ORDERS, _order(0, updateTime=start + HOUR_MS, **moment(1)), when=DEPOSIT)
    fake.add(
        ORDERS,
        _order(1, amount="30", indicatedAmount="31", totalFee="1", **moment(2)),
        when=WITHDRAW,
    )
    fake.add(PAYMENTS, _payment(0, **moment(3)), when=BUY)
    fake.add(
        PAYMENTS,
        _payment(
            1,
            sourceAmount="10",
            totalFee="0.1",
            obtainAmount="1",
            paymentMethod="Cash Balance",
            **moment(3),
        ),
        when=BUY,
    )
    fake.add(C2C, _p2p(0, commission="0.01", **moment(4)), when={"tradeType": "BUY"})
    fake.add(PAY, _pay(0, ("BBB", "0.5"), amount="-0.5", transactionTime=start + 5 * HOUR_MS))
    fake.add(REBATES, _rebate(0, updateTime=start + 5 * HOUR_MS))
    fake.set_rows("spot-account", spot_rows(("AAA", "15.712"), ("EUR", "109")))
    fake.set_rows("funding-asset", funding_rows(("BBB", "2.49")))
    notifier = RecordingNotifier()

    clock.ms += 6 * HOUR_MS
    after = await run_binance_sync(session, connection.id, fake.handler, notifier=notifier)

    assert after.status == "healthy", after.last_error
    records_posted = [k for k in await keys(session, connection.id) if not k.startswith("opening:")]
    assert len(records_posted) == 10
    assert await differences(session, connection.id) == []
    assert not [w for w in after.warnings if _CHANGED in w]
    assert not [m for m in notifier.messages if _CHANGED in m]
    asked = {(s.label, s.params.get("transactionType")) for s in fake.requests}
    assert {(ORDERS, "0"), (ORDERS, "1"), (PAYMENTS, "0"), (PAYMENTS, "1")} <= asked
    # Every documented time parameter, and no other.
    time_params = {
        ORDERS: ("beginTime", "endTime"),
        PAYMENTS: ("beginTime", "endTime"),
        C2C: ("startTimestamp", "endTimestamp"),
        PAY: ("startTime", "endTime"),
        REBATES: ("startTime", "endTime"),
    }
    names = {"beginTime", "endTime", "startTimestamp", "endTimestamp", "startTime"}
    for label, wanted in time_params.items():
        calls = fake.calls(label)
        assert calls, label
        for seen in calls:
            assert set(wanted) <= set(seen.params), label
            assert names & set(seen.params) == set(wanted), label
