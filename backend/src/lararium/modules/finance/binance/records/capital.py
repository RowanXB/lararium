"""Deposits and withdrawals (design 5.7 and 5.12).

A deposit is posted once Binance counts it in the balance (`status` 1, or 6: credited but
locked); a withdrawal from the moment it is applied. Each is a transfer leg: its own posting
and the contra `transfer_contra` chooses, nothing else, so a fee is a transaction of its own.
A withdrawal cancelled, rejected or failed after it was posted is not voided, because the
balance really fell and rose again: it stops being a transfer leg (its contra moves to
Unassigned) and a refund puts the amount and the fee back.

The chain address on the other side is the counterparty identifier (Task 8:
`counterparty_identifier`); until it is known the contra is Unassigned unless the owner pairs
the leg.
"""

from decimal import Decimal
from uuid import UUID

from lararium.modules.finance.binance.parse import Row
from lararium.modules.finance.binance.records import (
    Run,
    amount,
    fees,
    integer,
    live,
    post,
    text,
    when,
)
from lararium.modules.finance.ledger import PostingInput
from lararium.modules.finance.transfers import transfer_contra

DEPOSITS = "deposits"
WITHDRAWALS = "withdrawals"
# `walletType`: 0 Spot, 1 Funding.
_WALLETS = {0: "spot", 1: "funding"}
# Deposits Binance counts in the balance: 1 success, 6 credited but cannot withdraw yet.
_CREDITED = frozenset({1, 6})
# Withdrawals out of the balance: 0 email sent, 2 awaiting approval, 4 processing, 6 completed.
_APPLIED = frozenset({0, 2, 4, 6})
# Withdrawals that gave the amount back: 1 cancelled, 3 rejected, 5 failure.
_ENDED = frozenset({1, 3, 5})
# What Binance puts in `txId` for a movement that never went on chain.
_OFF_CHAIN = ("Off-chain transfer", "Internal transfer")


def chain_hash(row: Row) -> str | None:
    """The lower-cased transaction hash of an on-chain movement (`transferType` 0 with a
    chain id), else None (design 5.12)."""
    tx_id = text(row, "txId")
    if tx_id is None or integer(row, "transferType") != 0 or tx_id.startswith(_OFF_CHAIN):
        return None
    return tx_id.lower()


async def counterparty_identifier(run: Run, label: str, row: Row) -> UUID | None:
    """The registry row of the chain address on the other side: a withdrawal's `address`, a
    deposit's `sourceAddress`. None until Task 8 registers chain addresses."""
    return None


async def _wallet(run: Run, label: str, row: Row) -> UUID | None:
    kind = integer(row, "walletType")
    wallet = _WALLETS.get(kind) if kind is not None else None
    if wallet is None:
        run.flag(f"{label}.walletType not known")
        return None
    return await run.account(wallet)


async def _onchain(run: Run, row: Row) -> None:
    found = chain_hash(row)
    await run.typed(tx_hash=found, is_onchain=found is not None)


async def _contra(run: Run, key: str, account_id: UUID, identifier_id: UUID | None) -> UUID:
    return await transfer_contra(
        run.session,
        source=run.source(key),
        own_account_id=account_id,
        counterparty_identifier_id=identifier_id,
        other_postings=0,
    )


async def deposit(run: Run, row: Row) -> None:
    await _onchain(run, row)
    if integer(row, "status") not in _CREDITED:
        return
    account_id = await _wallet(run, DEPOSITS, row)
    coin = text(row, "coin") or ""
    instrument_id = await run.asset(coin)
    quantity = amount(DEPOSITS, row, "amount")
    occurred_at = when(DEPOSITS, row, "completeTime") or when(DEPOSITS, row, "insertTime")
    if account_id is None or instrument_id is None or quantity is None or occurred_at is None:
        return
    key = run.record.key
    identifier_id = await counterparty_identifier(run, DEPOSITS, row)
    contra = await _contra(run, key, account_id, identifier_id)
    transaction_id = await post(
        run,
        key,
        occurred_at=occurred_at,
        description=f"Deposit {coin}",
        postings=[
            PostingInput(account_id, instrument_id, quantity),
            PostingInput(contra, instrument_id, -quantity),
        ],
        counterparty_identifier_id=identifier_id,
    )
    await run.typed(transaction_id=transaction_id)


async def withdrawal(run: Run, row: Row) -> None:
    await _onchain(run, row)
    status = integer(row, "status")
    key = run.record.key
    if status not in _APPLIED and not (status in _ENDED and await live(run, key)):
        # Pending nothing yet, ended before it was posted, or a status the catalogue does
        # not list (flagged by the parser): nothing to post.
        return
    account_id = await _wallet(run, WITHDRAWALS, row)
    coin = text(row, "coin") or ""
    instrument_id = await run.asset(coin)
    quantity = amount(WITHDRAWALS, row, "amount")
    applied_at = when(WITHDRAWALS, row, "applyTime")
    if account_id is None or instrument_id is None or quantity is None or applied_at is None:
        return
    fee = amount(WITHDRAWALS, row, "transactionFee") or Decimal(0)
    identifier_id = await counterparty_identifier(run, WITHDRAWALS, row)
    ended = status in _ENDED
    # An ended withdrawal is no transfer leg any more: its contra is Unassigned.
    contra = run.unassigned if ended else await _contra(run, key, account_id, identifier_id)
    transaction_id = await post(
        run,
        key,
        occurred_at=applied_at,
        description=f"Withdrawal {coin}",
        postings=[
            PostingInput(account_id, instrument_id, -quantity),
            PostingInput(contra, instrument_id, quantity),
        ],
        counterparty_identifier_id=identifier_id,
    )
    await run.typed(transaction_id=transaction_id)
    await post(
        run,
        f"{key}:fee",
        occurred_at=applied_at,
        description=f"Withdrawal fee {coin}",
        postings=fees(account_id, run.unassigned, [(instrument_id, fee, "fee")]),
    )
    if not ended:
        return
    refunded_at = when(WITHDRAWALS, row, "completeTime") or await run.report_time()
    await post(
        run,
        f"{key}:refund",
        occurred_at=refunded_at,
        description=f"Withdrawal refund {coin}",
        postings=[
            PostingInput(account_id, instrument_id, quantity),
            PostingInput(account_id, instrument_id, fee, memo="fee"),
            PostingInput(run.unassigned, instrument_id, -(quantity + fee)),
        ],
    )
