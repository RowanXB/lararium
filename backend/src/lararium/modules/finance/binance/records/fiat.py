"""Fiat on Binance: deposits and withdrawals by bank, card purchases and sales, P2P orders, Pay
and rebates (design 5.7, 5.12).

A fiat order moves the currency between a bank and Spot: a transfer leg, its contra from
`transfer_contra`, marked a bank transfer, so it pairs with the bank's row once the owner has
registered the bank account Binance's fiat channel pays into or from on the Spot account. A card
purchase is two transactions: the fiat arriving on Spot from the card (`<key>:funding`, a leg
that pairs only through a card merchant) and the swap of that fiat for the crypto, with the fee;
one paid from the cash balance is the swap only. A P2P order is the same on Funding, at its
`unitPrice`: its fiat leg is a bank transfer to or from a stranger's account, which nothing can
be registered for, so it stays on Unassigned (where the bank's own row cancels it) unless the
owner pairs the two by hand. Everything else (the swaps, fees, Pay, rebates) is never a transfer.

Only a record in its documented final success status posts; in any other status what it posted
before is removed, so the ledger follows the row's latest version. A Pay transaction is what it
took from (or gave to) the wallet it names, against Unassigned. A rebate is income on Spot that a
dividend can have paid (rule 3, `normalize.link_payouts`).
"""

from datetime import datetime
from decimal import Decimal
from uuid import UUID

from lararium.modules.finance.binance.parse import ApiValue, Row
from lararium.modules.finance.binance.records import (
    Run,
    amount,
    code,
    fees,
    integer,
    post,
    remove,
    rounding,
    text,
    unit_cost,
    when,
)
from lararium.modules.finance.binance.records.earn import Rewards, reward
from lararium.modules.finance.ledger import PostingInput
from lararium.modules.finance.transfers import transfer_contra

ORDERS = "fiat-orders"
PAYMENTS = "fiat-payments"
C2C = "c2c"
PAY = "pay"
REBATES = "rebate"
# The documented final success statuses: only a record in one has moved money for good.
_ORDER_DONE = "Successful"
_PAYMENT_DONE = "Completed"
_C2C_DONE = "COMPLETED"
_CASH_BALANCE = "Cash Balance"
# `transactionType` as asked: a fiat order 0 deposit, 1 withdraw; a card payment 0 buy, 1 sell.
_TYPES = ("0", "1")
_IN = "0"
_BUY, _SELL = "BUY", "SELL"
# Pay's `walletType` 2 is Spot; every other (1 funding, 3 fiat, 4 or 6 card, 5 earn) is taken as
# Funding (design 6).
_PAY_SPOT = 2
_FUNDS = "fundsDetail"


async def _asked(run: Run, label: str) -> str | None:
    """The `transactionType` the record was asked for, kept on its typed row (the row does not
    say it); None, flagged, when it is missing or not one the catalogue documents."""
    found = run.record.record.request.get("transactionType")
    asked = found if isinstance(found, str) else None
    await run.typed(transaction_type=asked)
    if asked not in _TYPES:
        run.flag(f"{label}.transactionType {'missing' if asked is None else 'not known'}")
        return None
    return asked


async def _clear(run: Run, *keys: str) -> None:
    """Nothing of the record applies (not final, or no longer): what it posted is removed."""
    for key in keys:
        await remove(run, key)
    await run.typed(transaction_id=None)


async def _leg(
    run: Run,
    key: str,
    account_id: UUID,
    instrument_id: UUID,
    quantity: Decimal,
    *,
    occurred_at: datetime,
    description: str,
    bank_transfer: bool,
) -> UUID | None:
    """A transfer leg: the fiat on the wallet and its contra, nothing else (a leg has exactly
    two postings)."""
    contra = await transfer_contra(
        run.session,
        source=run.source(key),
        own_account_id=account_id,
        counterparty_identifier_id=None,
        other_postings=0,
    )
    return await post(
        run,
        key,
        occurred_at=occurred_at,
        description=description,
        postings=[
            PostingInput(account_id, instrument_id, quantity),
            PostingInput(contra, instrument_id, -quantity),
        ],
        transfer_leg=True,
        bank_transfer=bank_transfer,
    )


async def order(run: Run, row: Row) -> None:
    """A fiat deposit (`+amount`, what Binance credited after its fee) or withdrawal (`-amount`,
    and the fee as a transaction of its own) on Spot, a bank transfer leg."""
    kind = await _asked(run, ORDERS)
    key = run.record.key
    if kind is None or text(row, "status") != _ORDER_DONE:
        await _clear(run, key, f"{key}:fee")
        return
    currency = text(row, "fiatCurrency") or ""
    instrument_id = await run.asset(currency)
    quantity = amount(ORDERS, row, "amount")
    fee = amount(ORDERS, row, "totalFee") or Decimal(0)
    deposit = kind == _IN
    # A deposit counts when it is credited (its last update, to Successful); a withdrawal
    # leaves when it is asked, as a crypto withdrawal does at `applyTime`.
    created_at = when(ORDERS, row, "createTime")
    occurred_at = (when(ORDERS, row, "updateTime") or created_at) if deposit else created_at
    if instrument_id is None or quantity is None or occurred_at is None:
        return
    account_id = await run.account("spot")
    transaction_id = await _leg(
        run,
        key,
        account_id,
        instrument_id,
        quantity if deposit else -quantity,
        occurred_at=occurred_at,
        description=f"Fiat {'deposit' if deposit else 'withdrawal'} {currency}",
        bank_transfer=True,
    )
    await run.typed(transaction_id=transaction_id)
    # A deposit's fee was taken before Binance credited `amount`: typed only.
    await post(
        run,
        f"{key}:fee",
        occurred_at=occurred_at,
        description=f"Fiat withdrawal fee {currency}",
        postings=[] if deposit else fees(account_id, run.unassigned, [(instrument_id, fee, "fee")]),
    )


def _swap(
    account_id: UUID,
    crypto: UUID,
    crypto_quantity: Decimal,
    fiat: UUID,
    fiat_quantity: Decimal,
    price: Decimal,
    *,
    buy: bool,
) -> list[PostingInput]:
    """Crypto bought or sold for fiat, as a spot trade is posted (Decision 10): the crypto is
    the base, weighed by `price` in the fiat."""
    if buy:
        return [
            PostingInput(
                account_id, crypto, crypto_quantity, cost_per_unit=price, cost_instrument_id=fiat
            ),
            PostingInput(account_id, fiat, -fiat_quantity),
        ]
    return [
        PostingInput(
            account_id, crypto, -crypto_quantity, price_per_unit=price, price_instrument_id=fiat
        ),
        PostingInput(account_id, fiat, fiat_quantity),
    ]


async def payment(run: Run, row: Row) -> None:
    """A card purchase (or sale) on Spot: the fiat `sourceAmount` arriving from (leaving to) the
    card (`<key>:funding`), and the swap of fiat for `obtainAmount` of the crypto with `totalFee`
    as a fee. `sourceAmount` is what the card paid or received: a purchase's includes the fee
    (Binance's example: 20 paid, a fee of 0.2, 4.462 bought at 4.437472), a sale's is after it.
    Paid from the cash balance, it is the swap only."""
    kind = await _asked(run, PAYMENTS)
    key = run.record.key
    funding = f"{key}:funding"
    if kind is None or text(row, "status") != _PAYMENT_DONE:
        await _clear(run, key, funding)
        return
    fiat_code = text(row, "fiatCurrency") or ""
    crypto_code = text(row, "cryptoCurrency") or ""
    fiat = await run.asset(fiat_code)
    crypto = await run.asset(crypto_code)
    source = amount(PAYMENTS, row, "sourceAmount")
    obtained = amount(PAYMENTS, row, "obtainAmount")
    fee = amount(PAYMENTS, row, "totalFee") or Decimal(0)
    occurred_at = when(PAYMENTS, row, "createTime")
    if fiat is None or crypto is None or occurred_at is None:
        return
    if source is None or obtained is None or obtained == 0:
        return
    if fiat == crypto:
        run.flag(f"{PAYMENTS} names one asset twice")
        return
    buy = kind == _IN
    traded = source - fee if buy else source + fee
    account_id = await run.account("spot")
    swap = _swap(account_id, crypto, obtained, fiat, traded, unit_cost(traded, obtained), buy=buy)
    transaction_id = await post(
        run,
        key,
        occurred_at=occurred_at,
        description=f"Card {'purchase' if buy else 'sale'} {crypto_code}",
        postings=[*swap, *fees(account_id, run.unassigned, [(fiat, fee, "fee")])],
    )
    await run.typed(transaction_id=transaction_id)
    if text(row, "paymentMethod") == _CASH_BALANCE:
        await remove(run, funding)
        return
    await _leg(
        run,
        funding,
        account_id,
        fiat,
        source if buy else -source,
        occurred_at=occurred_at,
        description=f"Card {'payment' if buy else 'payout'} {fiat_code}",
        bank_transfer=False,
    )


async def c2c(run: Run, row: Row) -> None:
    """A completed P2P order on Funding: the fiat `totalPrice` paid in by (out to) the bank
    (`<key>:funding`, a bank transfer leg), and the swap of it for `amount` of the asset at
    `unitPrice` (what rounding leaves, one "rounding" posting), the `commission` a fee."""
    key = run.record.key
    funding = f"{key}:funding"
    if text(row, "orderStatus") != _C2C_DONE:
        await _clear(run, key, funding)
        return
    side = text(row, "tradeType")
    if side not in (_BUY, _SELL):
        if side is None:
            run.flag(f"{C2C}.tradeType missing")
        return  # a value the catalogue does not list is flagged by the parser
    fiat_code = text(row, "fiat") or ""
    crypto_code = text(row, "asset") or ""
    fiat = await run.asset(fiat_code)
    crypto = await run.asset(crypto_code)
    quantity = amount(C2C, row, "amount")
    total = amount(C2C, row, "totalPrice")
    price = amount(C2C, row, "unitPrice")
    commission = amount(C2C, row, "commission") or Decimal(0)
    occurred_at = when(C2C, row, "createTime")
    if fiat is None or crypto is None or occurred_at is None:
        return
    if quantity is None or quantity == 0 or total is None:
        return
    if fiat == crypto:
        run.flag(f"{C2C} names one asset twice")
        return
    buy = side == _BUY
    account_id = await run.account("funding")
    postings = [
        *_swap(
            account_id,
            crypto,
            quantity,
            fiat,
            total,
            price if price is not None else unit_cost(total, quantity),
            buy=buy,
        ),
        *fees(account_id, run.unassigned, [(crypto, commission, "commission")]),
    ]
    if price is not None:
        await rounding(
            run,
            postings,
            currency=fiat,
            quantity=quantity,
            price=price,
            problem="its totalPrice differs from amount times unitPrice by more than rounding",
        )
    transaction_id = await post(
        run,
        key,
        occurred_at=occurred_at,
        description=f"P2P {'buy' if buy else 'sell'} {crypto_code}",
        postings=postings,
    )
    await run.typed(transaction_id=transaction_id)
    await _leg(
        run,
        funding,
        account_id,
        fiat,
        total if buy else -total,
        occurred_at=occurred_at,
        description=f"P2P {'payment' if buy else 'proceeds'} {fiat_code}",
        bank_transfer=True,
    )


def _funds(row: Row) -> list[dict[str, ApiValue]]:
    found = row.get(_FUNDS)
    return [item for item in found if isinstance(item, dict)] if isinstance(found, list) else []


async def pay(run: Run, row: Row) -> None:
    """A Pay transaction: each `fundsDetail` element's `amount` of its `currency` on the wallet
    the row names (Funding, or Spot), signed by the row's `amount`, against Unassigned. A row
    with no `fundsDetail` moves its own `amount` of its `currency`."""
    signed = amount(PAY, row, "amount")
    occurred_at = when(PAY, row, "transactionTime")
    if signed is None or occurred_at is None:
        return
    sign = Decimal(-1) if signed < 0 else Decimal(1)
    pieces: list[tuple[str, Decimal]] = []
    for detail in _funds(row):
        currency = text(detail, "currency")
        quantity = amount(PAY, detail, "amount")
        if currency is None or quantity is None:
            run.flag(f"{PAY}.{_FUNDS} element without its currency or amount")
            return
        pieces.append((currency, quantity.copy_abs()))
    if not pieces:
        currency = text(row, "currency")
        if currency is None:
            run.flag(f"{PAY}.currency missing")
            return
        pieces.append((currency, signed.copy_abs()))
    wallet = "spot" if integer(row, "walletType") == _PAY_SPOT else "funding"
    account_id = await run.account(wallet)
    postings: list[PostingInput] = []
    totals: dict[UUID, Decimal] = {}
    for currency, quantity in pieces:
        instrument_id = await run.asset(currency)
        if instrument_id is None:
            return  # never one-sided: a record naming an uncounted asset is not posted
        postings.append(PostingInput(account_id, instrument_id, sign * quantity))
        totals[instrument_id] = totals.get(instrument_id, Decimal(0)) + sign * quantity
    postings += [PostingInput(run.unassigned, i, -q) for i, q in totals.items() if q != 0]
    transaction_id = await post(
        run,
        run.record.key,
        occurred_at=occurred_at,
        description=f"Pay {code(text(row, 'orderType'))}",
        postings=postings,
    )
    await run.typed(transaction_id=transaction_id)


# A rebate's type (1 commission rebate, 2 referral kickback) does not say whether a dividend paid
# it: every one can be its dividend (design 6).
REBATE_REWARDS = Rewards(REBATES, "spot", "asset", "amount", "Rebate", time_field="updateTime")
REWARD_HISTORIES = (REBATE_REWARDS,)

rebate = reward(REBATE_REWARDS)
