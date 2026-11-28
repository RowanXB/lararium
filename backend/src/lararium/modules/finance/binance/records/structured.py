"""Dual investment and discount buy (design 5.7), posted from the position rows of each report:
neither has a history endpoint.

A position's subscription moves the invested asset from Spot to Earn (`<record>:<id>`): at
`subscriptionTime` for dual investment, and for discount buy, which documents no subscription
time, its settlement date less its duration in days. A settled position's invested amount
leaves Earn (`<record>:<id>:settlement`) against Unassigned: Binance documents neither the
asset nor the amount it settles in, so what Spot receives shows as a reconciliation difference
(design 6). A refunded one (`REFUND_SUCCESS`, "refund to spot account") returns to Spot. The
settlement is dated at the settlement date, or when a report first showed it settled if that
is earlier, and keeps that date.

A position read again in a later report posts the same transactions, so nothing changes; one
whose purchase failed posts nothing.
"""

from datetime import datetime, timedelta

from lararium.modules.finance.binance import tables
from lararium.modules.finance.binance.parse import Row
from lararium.modules.finance.binance.records import (
    Run,
    amount,
    integer,
    post,
    posted_at,
    remove,
    text,
    when,
)
from lararium.modules.finance.ledger import PostingInput

DUAL_INVESTMENT = "dci-positions"
DISCOUNT_BUY = "discount-positions"
# The ledger key's record name of each label's positions.
RECORDS = {DUAL_INVESTMENT: "dci_position", DISCOUNT_BUY: "discount_position"}
_SETTLED = "SETTLED"
_REFUNDED = "REFUND_SUCCESS"
_FAILED = "PURCHASE_FAIL"
_STATUSES = frozenset(
    {"PENDING", "PURCHASE_SUCCESS", "SETTLING", _SETTLED, "REFUNDING", _REFUNDED, _FAILED}
)


def seen_at(run: Run) -> datetime:
    """The start of the read the current position row came from."""
    content = run.record.record.content
    assert isinstance(content, dict)
    started = content["read_from_ms"]
    assert isinstance(started, int)
    return tables.moment(started)


async def _position(
    run: Run,
    row: Row,
    *,
    label: str,
    asset_field: str,
    amount_field: str,
    subscribed_at: datetime | None,
    description: str,
) -> None:
    key = run.record.key
    settlement = f"{key}:settlement"
    status = text(row, "purchaseStatus")
    if status is None:
        run.flag(f"{label}.purchaseStatus missing")
    if status not in _STATUSES or status == _FAILED:
        # Failed, or a status the parser flags: nothing is known to have moved.
        await remove(run, key)
        await remove(run, settlement)
        return
    coin = text(row, asset_field) or ""
    instrument_id = await run.asset(coin)
    quantity = amount(label, row, amount_field)
    if instrument_id is None or quantity is None or subscribed_at is None:
        return
    spot, earn = await run.account("spot"), await run.account("earn")
    await post(
        run,
        key,
        occurred_at=subscribed_at,
        description=f"{description} {coin}",
        postings=[
            PostingInput(spot, instrument_id, -quantity),
            PostingInput(earn, instrument_id, quantity),
        ],
    )
    if status not in (_SETTLED, _REFUNDED):
        await remove(run, settlement)
        return
    seen = seen_at(run)
    settles = when(label, row, "settleDate")
    at = await posted_at(run, settlement) or min(settles or seen, seen)
    other = run.unassigned if status == _SETTLED else spot
    await post(
        run,
        settlement,
        occurred_at=at,
        description=f"{description} {'settled' if status == _SETTLED else 'refunded'} {coin}",
        postings=[
            PostingInput(earn, instrument_id, -quantity),
            PostingInput(other, instrument_id, quantity),
        ],
    )


async def dual_investment(run: Run, row: Row) -> None:
    await _position(
        run,
        row,
        label=DUAL_INVESTMENT,
        asset_field="investCoin",
        amount_field="subscriptionAmount",
        subscribed_at=when(DUAL_INVESTMENT, row, "subscriptionTime"),
        description="Dual investment",
    )


async def discount_buy(run: Run, row: Row) -> None:
    settles = when(DISCOUNT_BUY, row, "settleDate")
    days = integer(row, "duration")
    if settles is None:
        run.flag(f"{DISCOUNT_BUY}.settleDate missing")
    if days is None:
        run.flag(f"{DISCOUNT_BUY}.duration missing")
    subscribed_at = None if settles is None or days is None else settles - timedelta(days=days)
    await _position(
        run,
        row,
        label=DISCOUNT_BUY,
        asset_field="investAsset",
        amount_field="depositAmount",
        subscribed_at=subscribed_at,
        description="Discount buy",
    )
