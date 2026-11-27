"""The margin wallets: trades, interest, the liquidation fee and transfers (design 5.7).

Cross Margin and Isolated Margin hold signed net quantities (`netAsset`): borrowing adds the
asset and the debt at once and repaying takes both away, so neither changes a net quantity and
both are typed only; so are the capital flow (it repeats the rows here), forced liquidation
orders, small liability exchanges and the liquidation loan's repays. What does change one:

- a margin trade, posted as a spot trade (Decision 10) on the wallet `isIsolated` names; no
  `quoteQty` is documented for it, so the quote side is `qty` times `price`;
- interest, charged into the debt as it accrues: a fee on the wallet (the row's
  `isolatedSymbol`, or the pair the request asked for, names Isolated Margin); portfolio
  margin's daily interest (`PORTFOLIO`) is not this account's and is typed only;
- the liquidation fee, which only the capital flow lists (`LIQUIDATION_FEE`): a fee on the
  wallet its `symbol` names;
- a margin transfer, rule 1 between two wallets through `link_movements` (the universal
  transfer history lists most of them first: rule 2); one with a side outside the wallets the
  connection keeps (a sub-account, the mining or the old fiat wallet) is income or an outflow
  of the margin wallet against Unassigned.

Only a `CONFIRMED` transfer is posted (the documents give that value only).
"""

from decimal import Decimal
from uuid import UUID

from sqlalchemy import text as sql

from lararium.modules.finance.binance.parse import Row
from lararium.modules.finance.binance.records import (
    Run,
    amount,
    fees,
    post,
    remove,
    spot,
    text,
    when,
)
from lararium.modules.finance.binance.records.transfers import (
    Listing,
    Movement,
    post_movement,
)
from lararium.modules.finance.ledger import PostingInput

TRADES = "margin-trades"
TRANSFERS = "margin-transfers"
BORROW_REPAY = "margin-borrow-repay"
INTEREST = "margin-interest"
CAPITAL_FLOW = "margin-capital-flow"
CROSS = "cross_margin"
ISOLATED = "isolated_margin"
_CONFIRMED = "CONFIRMED"
_LIQUIDATION_FEE = "LIQUIDATION_FEE"
_PORTFOLIO = "PORTFOLIO"
_TRUE = "TRUE"
_TRANSFER_KEY = "margin_transfer:"
# A margin transfer's `transFrom` and `transTo` (the catalogue's enum) to the wallet the
# connection keeps; None for a side outside them (a sub-account, the mother account's Spot, the
# mining and the old fiat wallet). FUTURES is USDⓈ-M, DELIVERY COIN-M.
TRANSFER_SIDES: dict[str, str | None] = {
    "SPOT": "spot",
    "CROSS_MARGIN": CROSS,
    "ISOLATED_MARGIN": ISOLATED,
    "FUNDING": "funding",
    "FUTURES": "um_futures",
    "DELIVERY": "cm_futures",
    "OPTION": "options",
    "FIAT": None,
    "MINING": None,
    "MOTHER_SPOT": None,
    "SUB_SPOT": None,
    "SUB_MARGIN": None,
}


def _asked(run: Run, name: str) -> str | None:
    value = run.record.record.request.get(name)
    return value if isinstance(value, str) and value else None


async def trade(run: Run, row: Row) -> None:
    flag = row.get("isIsolated")
    isolated = flag if isinstance(flag, bool) else _asked(run, "isIsolated") == _TRUE
    quantity = amount(TRADES, row, "qty")
    price = amount(TRADES, row, "price")
    if quantity is None or price is None:
        return
    wallet = ISOLATED if isolated else CROSS
    await spot.post_trade(run, TRADES, row, wallet, quote_quantity=quantity * price)


async def borrow_repay(run: Run, row: Row) -> None:
    """Typed only: it changes no net quantity. Which of the two it is, the request says."""
    await run.typed(request_type=_asked(run, "type"))


async def interest(run: Run, row: Row) -> None:
    if text(row, "type") == _PORTFOLIO:
        return
    pair = text(row, "isolatedSymbol") or _asked(run, "isolatedSymbol")
    await _fee(run, INTEREST, row, "interest", "interestAccuredTime", pair, "margin interest")


async def capital_flow(run: Run, row: Row) -> None:
    if text(row, "type") != _LIQUIDATION_FEE:
        return
    pair = text(row, "symbol") or _asked(run, "symbol")
    await _fee(run, CAPITAL_FLOW, row, "amount", "timestamp", pair, "liquidation fee")


async def _fee(
    run: Run, label: str, row: Row, field: str, time_field: str, pair: str | None, memo: str
) -> None:
    """A charge taken from the margin wallet (`is_fee`) against Unassigned: Isolated Margin when
    the record names a pair. The capital flow's amount is signed; a fee is taken whatever its
    sign."""
    coin = text(row, "asset") or ""
    instrument_id = await run.asset(coin)
    charged = amount(label, row, field)
    occurred_at = when(label, row, time_field)
    if instrument_id is None or charged is None or occurred_at is None:
        return
    account_id = await run.account(ISOLATED if pair else CROSS)
    transaction_id = await post(
        run,
        run.record.key,
        occurred_at=occurred_at,
        description=f"{memo.capitalize()} {coin}",
        postings=fees(account_id, run.unassigned, [(instrument_id, charged.copy_abs(), memo)]),
    )
    await run.typed(transaction_id=transaction_id)


def _sides(run: Run, row: Row) -> tuple[str | None, str | None] | None:
    """The two wallets a transfer is between (None: outside the connection's wallets); None
    when a side is missing or not one the catalogue lists (the parser flags the value)."""
    source, destination = text(row, "transFrom"), text(row, "transTo")
    if source not in TRANSFER_SIDES or destination not in TRANSFER_SIDES:
        if source is None or destination is None:
            run.flag(f"{TRANSFERS}.{'transFrom' if source is None else 'transTo'} missing")
        return None
    return TRANSFER_SIDES[source], TRANSFER_SIDES[destination]


async def _linked(run: Run) -> bool:
    """Whether the current transfer is linked to a movement an earlier listing posts."""
    key = run.record.typed_key
    found = await run.session.scalar(
        sql(
            "select movement_key from finance.binance_margin_transfers"
            " where connection_id = :c and tx_id = :t"
        ),
        {"c": key["connection_id"], "t": key["tx_id"]},
    )
    return found is not None


async def transfer(run: Run, row: Row) -> None:
    key = run.record.key
    if text(row, "status") != _CONFIRMED:
        # Pending: not moved yet; failed: moved nothing. A link it had no longer holds.
        await remove(run, key)
        await run.typed(transaction_id=None, movement_key=None)
        return
    sides = _sides(run, row)
    coin = text(row, "asset") or ""
    quantity = amount(TRANSFERS, row, "amount")
    occurred_at = when(TRANSFERS, row, "timestamp")
    if sides is None or quantity is None or occurred_at is None:
        return
    source, destination = sides
    if source is not None and destination is not None:
        if await _linked(run):
            await remove(run, key)
            await run.typed(transaction_id=None)
            return
        movement = Movement(key, coin, quantity, (source, destination), occurred_at)
        await run.typed(transaction_id=await post_movement(run, movement))
        return
    if source is None and destination is None:
        run.flag(f"{TRANSFERS} between two wallets outside the account")
        return
    instrument_id = await run.asset(coin)
    if instrument_id is None:
        return
    taken = (
        await run.account(source) if source is not None else run.unassigned,
        await run.account(destination) if destination is not None else run.unassigned,
    )
    transaction_id = await post(
        run,
        key,
        occurred_at=occurred_at,
        description=f"Transfer {coin}",
        postings=[
            PostingInput(taken[0], instrument_id, -quantity),
            PostingInput(taken[1], instrument_id, quantity),
        ],
    )
    await run.typed(transaction_id=transaction_id)


async def _movements(run: Run) -> list[Movement]:
    """Every confirmed margin transfer between two of the connection's wallets, with the
    movement it is linked to now."""
    inside = [name for name, wallet in TRANSFER_SIDES.items() if wallet is not None]
    rows = await run.session.execute(
        sql(
            "select tx_id, asset, amount, trans_from, trans_to, timestamp, movement_key"
            " from finance.binance_margin_transfers"
            " where connection_id = :c and status = :s"
            " and trans_from = any(:w) and trans_to = any(:w)"
        ),
        {"c": run.connection.id, "s": _CONFIRMED, "w": inside},
    )
    found: list[Movement] = []
    for r in rows:
        source, destination = TRANSFER_SIDES[r.trans_from], TRANSFER_SIDES[r.trans_to]
        assert source is not None and destination is not None
        found.append(
            Movement(
                f"{_TRANSFER_KEY}{r.tx_id}",
                r.asset,
                Decimal(r.amount),
                (source, destination),
                r.timestamp,
                linked_to=r.movement_key,
            )
        )
    return found


async def _link(
    run: Run, movement: Movement, target: str | None, transaction_id: UUID | None
) -> None:
    await run.session.execute(
        sql(
            "update finance.binance_margin_transfers"
            " set movement_key = :m, transaction_id = :t"
            " where connection_id = :c and tx_id = :id"
        ),
        {
            "m": target,
            "t": transaction_id,
            "c": run.connection.id,
            "id": movement.key.removeprefix(_TRANSFER_KEY),
        },
    )


# Rule 2: the margin transfer history lists what the universal transfer history lists first.
MARGIN_LISTING = Listing(TRANSFERS, _movements, _link)
