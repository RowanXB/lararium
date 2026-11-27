"""The futures wallets, USDⓈ-M and COIN-M: their income histories (design 5.7).

A futures wallet's balance changes only by its income rows: realised profit, commissions,
funding, transfers in and out and the rest of the catalogue's `incomeType`s. Trades and
positions carry no money of their own (their profit and fees are income rows) and are typed
only; a contract is never an instrument (design 5.5).

- Every documented `incomeType` but `TRANSFER` is `income` of `asset` on the wallet against
  Unassigned; a negative commission, funding fee or insurance clearance (`INCOME_FEES`) is a fee
  (`is_fee`). A type the catalogue does not list is flagged by the parser and not posted.
- A `TRANSFER` row is a wallet-to-wallet movement through `link_movements` (rule 2): one the
  universal or the margin transfer history lists first is linked to it and not posted. The row
  names only the futures side (into it when `income` is positive): posted by itself, its other
  side is Unassigned.
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

UM_INCOME = "um-income"
CM_INCOME = "cm-income"
# Each income history's wallet.
WALLETS = {UM_INCOME: "um_futures", CM_INCOME: "cm_futures"}
TRANSFER = "TRANSFER"
# The income types whose negative amounts are fees the wallet paid.
INCOME_FEES = frozenset({"COMMISSION", "FUNDING_FEE", "INSURANCE_CLEAR"})


def income_types(label: str) -> frozenset[str]:
    """The `incomeType`s the catalogue lists for an income history."""
    found = next(f.enum for f in FIELDS[label] if f.path == "[].incomeType")
    assert found is not None
    return found


def _record(label: str) -> str:
    return label.replace("-", "_")


def _table(label: str) -> str:
    return f"finance.binance_{_record(label)}"


def _sides(label: str, income: Decimal) -> tuple[str | None, str | None]:
    """A transfer's (from, to): the futures wallet and a side the row does not name."""
    wallet = WALLETS[label]
    return (None, wallet) if income > 0 else (wallet, None)


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
        sql(
            f"select movement_key from {_table(run.record.label)}"
            " where connection_id = :c and income_type = :k and tran_id = :t"
        ),
        {"c": key["connection_id"], "k": key["income_type"], "t": key["tran_id"]},
    )
    return found is not None


async def income(run: Run, row: Row) -> None:
    label = run.record.label
    kind = text(row, "incomeType")
    coin = text(row, "asset") or ""
    quantity = amount(label, row, "income")
    occurred_at = when(label, row, "time")
    if kind not in income_types(label) or quantity is None or occurred_at is None:
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
            run.record.key, coin, quantity.copy_abs(), _sides(label, quantity), occurred_at
        )
        await run.typed(transaction_id=await post_movement(run, movement))
        return
    account_id = await run.account(WALLETS[label])
    memo = kind.lower().replace("_", " ")
    if kind in INCOME_FEES and quantity < 0:
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
        description=f"Futures {memo} {coin}",
        postings=postings,
    )
    await run.typed(transaction_id=transaction_id)


def _movements_of(label: str):
    async def movements(run: Run) -> list[Movement]:
        """Every `TRANSFER` row of the income history, with the movement it is linked to now."""
        rows = await run.session.execute(
            sql(
                "select tran_id, asset, income, time, movement_key"
                f" from {_table(label)}"
                " where connection_id = :c and income_type = :k and income <> 0"
            ),
            {"c": run.connection.id, "k": TRANSFER},
        )
        return [
            Movement(
                f"{_record(label)}:{TRANSFER}:{r.tran_id}",
                r.asset,
                Decimal(r.income).copy_abs(),
                _sides(label, Decimal(r.income)),
                r.time,
                linked_to=r.movement_key,
            )
            for r in rows
        ]

    return movements


def _link_of(label: str):
    prefix = f"{_record(label)}:{TRANSFER}:"

    async def link(
        run: Run, movement: Movement, target: str | None, transaction_id: UUID | None
    ) -> None:
        await run.session.execute(
            sql(
                f"update {_table(label)} set movement_key = :m, transaction_id = :t"
                " where connection_id = :c and income_type = :k and tran_id = :id"
            ),
            {
                "m": target,
                "t": transaction_id,
                "c": run.connection.id,
                "k": TRANSFER,
                "id": movement.key.removeprefix(prefix),
            },
        )

    return link


# Rule 2: a futures transfer is listed by the universal (or the margin) transfer history first.
UM_LISTING = Listing(UM_INCOME, _movements_of(UM_INCOME), _link_of(UM_INCOME))
CM_LISTING = Listing(CM_INCOME, _movements_of(CM_INCOME), _link_of(CM_INCOME))
