"""Spot trades, converts, asset dividends and dust conversions (design 5.7, plan Decision 10).

A trade is a swap on Spot: the buyer's base is weighed by Binance's `price` in the quote
(`cost_per_unit`), the seller's by the same price (`price_per_unit`), and the other side is
`quoteQty`; what a rounded price leaves is one "rounding" posting, within what rounding can
explain. A trade names only its symbol: its base and quote assets come from `exchangeInfo`, the
latest report listing it. A convert is a swap on the wallet it names, the received side weighed
by its cost in the given asset. A dividend is income on Spot. A dust conversion is one
transaction on the wallet it was asked for: each line a swap of a small balance into BNB
weighed by its cost, and each line's service charge a fee taken out of that BNB.
"""

from decimal import Decimal
from uuid import UUID

from sqlalchemy import text as sql

from lararium.modules.finance.binance.parse import ApiValue, Row
from lararium.modules.finance.binance.records import (
    Run,
    amount,
    code,
    fees,
    integer,
    post,
    rounding,
    text,
    unit_cost,
    when,
)
from lararium.modules.finance.ledger import PostingInput

TRADES = "my-trades"
CONVERT = "convert"
DIVIDENDS = "dividends"
DUST = "dust"
_CONVERT_WALLETS = {"SPOT": "spot", "FUNDING": "funding"}
_CONVERTED = "SUCCESS"
# The request's `accountType` (the row does not say it): SPOT and MARGIN (cross margin).
_DUST_WALLETS = {"SPOT": "spot", "MARGIN": "cross_margin"}
_DUST_LINES = "userAssetDribbletDetails"
# Binance converts dust into BNB; a line that does not name what it received received BNB.
_DUST_TARGET = "BNB"


async def _pair(run: Run, symbol: str) -> tuple[str, str] | None:
    """The base and quote asset of a symbol, as the latest `exchangeInfo` listing it says."""
    row = (
        await run.session.execute(
            sql(
                "select base_asset, quote_asset from finance.binance_exchange_info_symbols"
                " where connection_id = :c and symbol = :s order by snapshot_at desc limit 1"
            ),
            {"c": run.connection.id, "s": symbol},
        )
    ).first()
    return None if row is None else (row.base_asset, row.quote_asset)


async def trade(run: Run, row: Row) -> None:
    await post_trade(run, TRADES, row, "spot")


async def post_trade(
    run: Run, label: str, row: Row, wallet: str, *, quote_quantity: Decimal | None = None
) -> None:
    """A trade of the label's row as a swap on the wallet's account (Decision 10): the base
    weighed by `price` in the quote, the quote side `quote_quantity` (the row's `quoteQty`
    when None), the commission a fee, and what a rounded price leaves one "rounding" posting.
    A margin trade is one, on its margin wallet (design 5.7)."""
    symbol = text(row, "symbol") or ""
    pair = await _pair(run, symbol)
    if pair is None:
        run.flag(f"symbol {code(symbol)} has trades exchangeInfo does not list")
        return
    buyer = row.get("isBuyer")
    if not isinstance(buyer, bool):
        run.flag(f"{label}.isBuyer missing")
        return
    quantity = amount(label, row, "qty")
    price = amount(label, row, "price")
    if quote_quantity is None:
        quote_quantity = amount(label, row, "quoteQty")
    commission = amount(label, row, "commission")
    occurred_at = when(label, row, "time")
    if quantity is None or price is None or quote_quantity is None or commission is None:
        return
    if occurred_at is None:
        return
    base = await run.asset(pair[0])
    quote = await run.asset(pair[1])
    fee_asset = await run.asset(text(row, "commissionAsset") or "") if commission else quote
    if base is None or quote is None or fee_asset is None:
        return
    if base == quote:
        run.flag(f"symbol {code(symbol)} names one asset twice")
        return
    account_id = await run.account(wallet)
    if buyer:
        given = PostingInput(
            account_id, base, quantity, cost_per_unit=price, cost_instrument_id=quote
        )
    else:
        given = PostingInput(
            account_id, base, -quantity, price_per_unit=price, price_instrument_id=quote
        )
    postings = [
        given,
        PostingInput(account_id, quote, -quote_quantity if buyer else quote_quantity),
        *fees(account_id, run.unassigned, [(fee_asset, commission, "commission")]),
    ]
    await rounding(
        run,
        postings,
        currency=quote,
        quantity=quantity,
        price=price,
        problem="its quoteQty differs from qty times price by more than rounding",
    )
    transaction_id = await post(
        run,
        run.record.key,
        occurred_at=occurred_at,
        description=f"{'Buy' if buyer else 'Sell'} {symbol}",
        postings=postings,
    )
    await run.typed(transaction_id=transaction_id)


async def convert(run: Run, row: Row) -> None:
    status = text(row, "orderStatus")
    if status != _CONVERTED:
        run.flag(f"{CONVERT}.orderStatus={code(status)}")
        return
    wallet_type = text(row, "walletType")
    wallet = _CONVERT_WALLETS.get(wallet_type or "")
    if wallet is None:
        run.flag(f"{CONVERT}.walletType={code(wallet_type)}")
        return
    given_code, received_code = text(row, "fromAsset") or "", text(row, "toAsset") or ""
    given = await run.asset(given_code)
    received = await run.asset(received_code)
    given_amount = amount(CONVERT, row, "fromAmount")
    received_amount = amount(CONVERT, row, "toAmount")
    occurred_at = when(CONVERT, row, "createTime")
    if given is None or received is None or occurred_at is None:
        return
    if given_amount is None or received_amount is None or received_amount == 0:
        return
    if given == received:
        run.flag(f"{CONVERT} names one asset twice")
        return
    account_id = await run.account(wallet)
    transaction_id = await post(
        run,
        run.record.key,
        occurred_at=occurred_at,
        description=f"Convert {given_code} to {received_code}",
        postings=[
            PostingInput(account_id, given, -given_amount),
            PostingInput(
                account_id,
                received,
                received_amount,
                cost_per_unit=unit_cost(given_amount, received_amount),
                cost_instrument_id=given,
            ),
        ],
    )
    await run.typed(transaction_id=transaction_id)


async def dividend(run: Run, row: Row) -> None:
    coin = text(row, "asset") or ""
    instrument_id = await run.asset(coin)
    quantity = amount(DIVIDENDS, row, "amount")
    occurred_at = when(DIVIDENDS, row, "divTime")
    if instrument_id is None or quantity is None or occurred_at is None:
        return
    # `direction` -1 is a debit.
    if integer(row, "direction") == -1:
        quantity = -quantity
    account_id = await run.account("spot")
    transaction_id = await post(
        run,
        run.record.key,
        occurred_at=occurred_at,
        description=f"Dividend {coin}",
        postings=[
            PostingInput(account_id, instrument_id, quantity),
            PostingInput(run.unassigned, instrument_id, -quantity),
        ],
    )
    await run.typed(transaction_id=transaction_id)


def _lines(row: Row) -> list[dict[str, ApiValue]]:
    found = row.get(_DUST_LINES)
    return [line for line in found if isinstance(line, dict)] if isinstance(found, list) else []


async def dust(run: Run, row: Row) -> None:
    """Each line: `-amount` of the small asset, `+(transferedAmount + serviceChargeAmount)` of
    BNB weighed by its cost in it, `-serviceChargeAmount` of BNB as a fee; all on the wallet the
    request asked for. `transferedAmount` is the BNB credited after the charge (Binance's
    example: the charge is 2% of the two together), so the line's BNB changes by it."""
    asked = run.record.record.request.get("accountType")
    account_type = asked if isinstance(asked, str) else None
    await run.typed(account_type=account_type)
    wallet = _DUST_WALLETS.get(account_type or "")
    if wallet is None:
        run.flag(f"{DUST}.accountType {'missing' if account_type is None else 'not known'}")
        return
    account_id = await run.account(wallet)
    occurred_at = when(DUST, row, "operateTime")
    swaps: list[PostingInput] = []
    charged: list[tuple[UUID, Decimal, str]] = []
    for line in _lines(row):
        given_code = text(line, "fromAsset") or ""
        received_code = text(line, "targetAsset") or _DUST_TARGET
        given = await run.asset(given_code)
        received = await run.asset(received_code)
        given_amount = amount(DUST, line, "amount")
        credited = amount(DUST, line, "transferedAmount")
        charge = amount(DUST, line, "serviceChargeAmount") or Decimal(0)
        occurred_at = occurred_at or when(DUST, line, "operateTime")
        if given is None or received is None or given_amount is None:
            continue
        if credited is None:
            continue
        # What the small balance bought: the BNB credited and the charge taken from it.
        received_amount = credited + charge
        if received_amount == 0:
            continue
        if given == received:
            run.flag(f"{DUST} names one asset twice")
            continue
        swaps += [
            PostingInput(account_id, given, -given_amount),
            PostingInput(
                account_id,
                received,
                received_amount,
                cost_per_unit=unit_cost(given_amount, received_amount),
                cost_instrument_id=given,
            ),
        ]
        charged.append((received, charge, "service charge"))
    if occurred_at is None:
        return
    transaction_id = await post(
        run,
        run.record.key,
        occurred_at=occurred_at,
        description="Dust conversion",
        postings=[*swaps, *fees(account_id, run.unassigned, charged)],
    )
    await run.typed(transaction_id=transaction_id)
