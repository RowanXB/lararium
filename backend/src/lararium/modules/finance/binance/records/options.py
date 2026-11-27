"""The Options wallet: its bill, the account's funding flow (design 5.7).

The Options wallet's balance changes only by its bill rows: premiums paid and received
(`CONTRACT`), exercise profit and loss, fees, and transfers in and out. Trades, exercises and
positions carry no money of their own (their premiums, profit and fees are bill rows) and are
typed only; a contract is never an instrument (design 5.5).

- Every documented `type` but `TRANSFER` is `amount` of `asset` on Options against Unassigned;
  a negative `FEE` or `EXERCISE_FEE` (`BILL_FEES`) is a fee (`is_fee`). A type the catalogue
  does not list is flagged by the parser and not posted.
- A `TRANSFER` row is a wallet-to-wallet movement through `link_movements` (rule 2): one the
  universal transfer history lists first is linked to it and not posted. The row names only
  the Options side (into it when `amount` is positive): posted by itself, its other side is
  Unassigned.
"""

from decimal import Decimal
from uuid import UUID

from sqlalchemy import text as sql

from lararium.modules.finance.binance.catalogue import FIELDS
from lararium.modules.finance.binance.parse import Row
from lararium.modules.finance.binance.records import (
    Run,
    amount,
    fees,
    post,
    remove,
    text,
    when,
)
from lararium.modules.finance.binance.records.transfers import (
    Listing,
    Movement,
    post_movement,
)
from lararium.modules.finance.ledger import PostingInput

BILL = "opt-bill"
WALLET = "options"
TRANSFER = "TRANSFER"
# The bill types whose negative amounts are fees the wallet paid.
BILL_FEES = frozenset({"FEE", "EXERCISE_FEE"})
_RECORD = "opt_bill"
_TABLE = f"finance.binance_{_RECORD}"


def bill_types() -> frozenset[str]:
    """The bill `type`s the catalogue lists (those Binance's documents name)."""
    found = next(f.enum for f in FIELDS[BILL] if f.path == "[].type")
    assert found is not None
    return found


def _sides(quantity: Decimal) -> tuple[str | None, str | None]:
    """A transfer's (from, to): the Options wallet and a side the row does not name."""
    return (None, WALLET) if quantity > 0 else (WALLET, None)


async def _unpost(run: Run, *, unlink: bool = False) -> None:
    """The current row posts nothing (now): its earlier posting, if any, is removed."""
    await remove(run, run.record.key)
    if unlink:
        await run.typed(transaction_id=None, movement_key=None)
    else:
        await run.typed(transaction_id=None)


async def _linked(run: Run) -> bool:
    """Whether the current transfer row is linked to a movement an earlier listing posts."""
    key = run.record.typed_key
    found = await run.session.scalar(
        sql(f"select movement_key from {_TABLE} where connection_id = :c and id = :i"),
        {"c": key["connection_id"], "i": key["id"]},
    )
    return found is not None


async def bill(run: Run, row: Row) -> None:
    kind = text(row, "type")
    coin = text(row, "asset") or ""
    quantity = amount(BILL, row, "amount")
    occurred_at = when(BILL, row, "createDate")
    if kind is None:
        run.flag(f"{BILL}.type missing")
    if kind not in bill_types() or quantity is None or occurred_at is None:
        # An undocumented type is flagged by the parser; its effect shows in reconciliation.
        await _unpost(run, unlink=True)
        return
    instrument_id = await run.asset(coin)
    if instrument_id is None:
        await _unpost(run, unlink=True)
        return
    if kind == TRANSFER:
        if await _linked(run):
            await _unpost(run)
            return
        movement = Movement(
            run.record.key, coin, quantity.copy_abs(), _sides(quantity), occurred_at
        )
        await run.typed(transaction_id=await post_movement(run, movement))
        return
    account_id = await run.account(WALLET)
    memo = kind.lower().replace("_", " ")
    if kind in BILL_FEES and quantity < 0:
        postings = fees(account_id, run.unassigned, [(instrument_id, -quantity, memo)])
    else:
        postings = [
            PostingInput(account_id, instrument_id, quantity, memo=memo),
            PostingInput(run.unassigned, instrument_id, -quantity),
        ]
    transaction_id = await post(
        run,
        run.record.key,
        occurred_at=occurred_at,
        description=f"Options {memo} {coin}",
        postings=postings,
    )
    await run.typed(transaction_id=transaction_id)


async def _movements(run: Run) -> list[Movement]:
    """Every `TRANSFER` row of the bill, with the movement it is linked to now."""
    rows = await run.session.execute(
        sql(
            "select id, asset, amount, create_date, movement_key"
            f" from {_TABLE}"
            " where connection_id = :c and type = :k and amount <> 0"
        ),
        {"c": run.connection.id, "k": TRANSFER},
    )
    return [
        Movement(
            f"{_RECORD}:{r.id}",
            r.asset,
            Decimal(r.amount).copy_abs(),
            _sides(Decimal(r.amount)),
            r.create_date,
            linked_to=r.movement_key,
        )
        for r in rows
    ]


async def _link(
    run: Run, movement: Movement, target: str | None, transaction_id: UUID | None
) -> None:
    await run.session.execute(
        sql(
            f"update {_TABLE} set movement_key = :m, transaction_id = :t"
            " where connection_id = :c and id = :i"
        ),
        {
            "m": target,
            "t": transaction_id,
            "c": run.connection.id,
            "i": movement.key.removeprefix(f"{_RECORD}:"),
        },
    )


# Rule 2: an options transfer is listed by the universal transfer history first.
LISTING = Listing(BILL, _movements, _link)
