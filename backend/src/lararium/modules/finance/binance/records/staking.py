"""Yield tokens and staking (design 5.5, 5.7): BFUSD and RWUSD, ETH and SOL staking, WBETH
wraps, soft staking and SOL boost rewards.

A subscription, stake, redemption, wrap or unwrap is a swap on Spot: the given asset leaves,
the token (or the asset it stands for) arrives weighed by its cost in the given one (Decision
10). Only a finished one (`SUCCESS`) is posted. A redemption Binance pays out later
(`arrivalTime` after `time`, or still `PENDING`) takes the given asset at `time` and holds what
it buys on the Pending redemptions account (`accounts.REDEEMING`) until it arrives on Spot at
`arrivalTime` (`<key>:arrival`); never In transit, which only a transfer's leg may post on. A
BFUSD or RWUSD redemption's `fee` is taken from what it received: `receiveAmount` is net of it
(Binance's example: 51 redeemed, 50 received, a fee of 1).

None of these rows has an id, and a row's `status` changes as it completes: its ledger key is
what the catalogue identifies it by (`time`, the given asset and amount) and its occurrence,
not its content, so a row seen pending and then finished is one movement, with one typed row:
the version applied last replaces the earlier ones (`_supersede`).

WBETH and BNSOL rewards accrue inside the token's rate and change no quantity: typed only.
BFUSD, RWUSD and soft staking rewards and claimed SOL boost rewards are income on Spot, unless
an asset dividend paid them (rule 3); a distributed boost reward waits to be claimed and is
typed only.
"""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import text as sql

from lararium.modules.finance.binance import layout, tables
from lararium.modules.finance.binance.accounts import REDEEMING
from lararium.modules.finance.binance.parse import Row
from lararium.modules.finance.binance.records import (
    Handler,
    Run,
    amount,
    fees,
    post,
    remove,
    text,
    unit_cost,
    when,
)
from lararium.modules.finance.binance.records.earn import Rewards, reward
from lararium.modules.finance.ledger import PostingInput
from lararium.modules.finance.money import to_plain_str

BFUSD_SUBSCRIPTIONS = "bfusd-subscriptions"
BFUSD_REDEMPTIONS = "bfusd-redemptions"
RWUSD_SUBSCRIPTIONS = "rwusd-subscriptions"
RWUSD_REDEMPTIONS = "rwusd-redemptions"
ETH_STAKES = "eth-stakes"
ETH_REDEMPTIONS = "eth-redemptions"
WBETH_WRAPS = "wbeth-wraps"
WBETH_UNWRAPS = "wbeth-unwraps"
SOL_STAKES = "sol-stakes"
SOL_REDEMPTIONS = "sol-redemptions"
BOOST_REWARDS = "sol-boost-rewards"
_DONE = "SUCCESS"
_PENDING = "PENDING"
# A boost reward is asked per `type`: CLAIM is one claimed to Spot, DISTRIBUTE one handed out
# to be claimed (`sol-unclaimed-rewards` lists what is unclaimed).
_CLAIM = "CLAIM"


@dataclass(frozen=True)
class Swap:
    """A history of swaps on Spot: the row's fields of what it gives and receives (asset,
    amount), a redemption's fee taken from what it received, and whether it can arrive later."""

    label: str
    given: tuple[str, str]
    received: tuple[str, str]
    description: str
    fee: str | None = None
    arrives: bool = False


_TOKEN = (("asset", "amount"), ("receiveAsset", "receiveAmount"))
_STAKE = (("asset", "amount"), ("distributeAsset", "distributeAmount"))
_WRAP = (("fromAsset", "fromAmount"), ("toAsset", "toAmount"))
SWAPS = {
    s.label: s
    for s in (
        Swap(BFUSD_SUBSCRIPTIONS, *_TOKEN, "BFUSD subscription"),
        Swap(BFUSD_REDEMPTIONS, *_TOKEN, "BFUSD redemption", fee="fee", arrives=True),
        Swap(RWUSD_SUBSCRIPTIONS, *_TOKEN, "RWUSD subscription"),
        Swap(RWUSD_REDEMPTIONS, *_TOKEN, "RWUSD redemption", fee="fee", arrives=True),
        Swap(ETH_STAKES, *_STAKE, "ETH staking"),
        Swap(ETH_REDEMPTIONS, *_STAKE, "ETH staking redemption", arrives=True),
        Swap(SOL_STAKES, *_STAKE, "SOL staking"),
        Swap(SOL_REDEMPTIONS, *_STAKE, "SOL staking redemption", arrives=True),
        Swap(WBETH_WRAPS, *_WRAP, "WBETH wrap"),
        Swap(WBETH_UNWRAPS, *_WRAP, "WBETH unwrap"),
    )
}


def movement_key(run: Run, swap: Swap, row: Row, occurred_at: datetime) -> str | None:
    """`<record>:<time ms>:<given asset>:<given amount>:<occurrence>`: the same for every
    status the row is seen in."""
    coin = text(row, swap.given[0])
    quantity = amount(swap.label, row, swap.given[1])
    if coin is None or quantity is None:
        return None
    record = run.record.key.split(":", 1)[0]
    occurrence = run.record.typed_key[layout.OCCURRENCE]
    plain = to_plain_str(quantity.normalize())
    return f"{record}:{tables.millis(occurred_at)}:{coin}:{plain}:{occurrence}"


async def _supersede(run: Run, swap: Swap, row: Row, occurred_at: datetime) -> None:
    """Delete the typed rows of the same movement (time, given asset and amount, occurrence)
    in another version (another status): Binance lists one movement, which it has updated."""
    key = run.record.typed_key
    asset_column, amount_column = (layout.snake(name) for name in swap.given)
    await run.session.execute(
        sql(
            f"delete from finance.{layout.table_name(swap.label)}"
            f" where connection_id = :c and time = :t and {asset_column} = :a"
            f" and {amount_column} = :q and occurrence = :o and content_hash <> :h"
        ),
        {
            "c": key["connection_id"],
            "t": occurred_at,
            "a": text(row, swap.given[0]),
            "q": amount(swap.label, row, swap.given[1]),
            "o": key[layout.OCCURRENCE],
            "h": key[layout.CONTENT_HASH],
        },
    )


def _swap_handler(swap: Swap) -> Handler:
    async def handle(run: Run, row: Row) -> None:
        occurred_at = when(swap.label, row, "time")
        if occurred_at is None:
            return
        key = movement_key(run, swap, row, occurred_at)
        if key is None:
            return
        await _supersede(run, swap, row, occurred_at)
        arrival = f"{key}:arrival"
        status = text(row, "status")
        if status is None:
            run.flag(f"{swap.label}.status missing")
        pending = swap.arrives and status == _PENDING
        if status != _DONE and not pending:
            # Failed, or not yet counted: nothing moved.
            await remove(run, key)
            await remove(run, arrival)
            await run.typed(transaction_id=None)
            return
        given_code, received_code = text(row, swap.given[0]), text(row, swap.received[0])
        given = await run.asset(given_code or "")
        received = await run.asset(received_code or "")
        given_amount = amount(swap.label, row, swap.given[1])
        net = amount(swap.label, row, swap.received[1])
        fee = (amount(swap.label, row, swap.fee) if swap.fee else None) or Decimal(0)
        if given is None or received is None or given_amount is None or net is None:
            return
        if given == received:
            run.flag(f"{swap.label} names one asset twice")
            return
        gross = net + fee
        if gross == 0:
            return
        arrives_at = when(swap.label, row, "arrivalTime") if swap.arrives else None
        later = pending or (arrives_at is not None and arrives_at > occurred_at)
        spot = await run.account("spot")
        holder = await run.account(REDEEMING) if later else spot
        postings = [
            PostingInput(spot, given, -given_amount),
            PostingInput(
                holder,
                received,
                gross,
                cost_per_unit=unit_cost(given_amount, gross),
                cost_instrument_id=given,
            ),
            *fees(holder, run.unassigned, [(received, fee, "redemption fee")]),
        ]
        description = f"{swap.description} {given_code} to {received_code}"
        transaction_id = await post(
            run, key, occurred_at=occurred_at, description=description, postings=postings
        )
        await run.typed(transaction_id=transaction_id)
        if not later or pending or arrives_at is None:
            await remove(run, arrival)
            return
        await _arrive(run, arrival, holder, spot, received, net, arrives_at, description)

    return handle


async def _arrive(
    run: Run,
    key: str,
    holder: UUID,
    spot: UUID,
    received: UUID,
    net: Decimal,
    at: datetime,
    description: str,
) -> None:
    """What a redemption bought leaves Pending redemptions for Spot when it arrives: a move
    between two of the connection's accounts, with no cost of its own, as Phase A's moves
    between wallets; the received side's cost sits on its Pending redemptions posting."""
    await post(
        run,
        key,
        occurred_at=at,
        description=f"{description} arrived",
        postings=[PostingInput(holder, received, -net), PostingInput(spot, received, net)],
    )


SWAP_HANDLERS: dict[str, Handler] = {label: _swap_handler(swap) for label, swap in SWAPS.items()}


# Rewards (rule 3).

# BFUSD's reward names its asset (its example: BFUSD; the account also reports USDT profit);
# RWUSD's names none and is taken to be RWUSD. Both tokens are held on Spot.
BFUSD_REWARDS = Rewards("bfusd-rewards", "spot", "rewardAsset", "rewardsAmount", "BFUSD reward")
RWUSD_REWARDS = Rewards(
    "rwusd-rewards", "spot", None, "rewardsAmount", "RWUSD reward", coin="RWUSD"
)
# Soft staking earns on what Spot holds, and pays its reward there.
SOFT_STAKING_REWARDS = Rewards(
    "soft-staking-rewards", "spot", "rewardAsset", "rewards", "Soft staking reward"
)
CLAIMED_BOOST_REWARDS = Rewards(
    BOOST_REWARDS,
    "spot",
    "token",
    "amount",
    "SOL boost reward",
    linkable=f"type = '{_CLAIM}' and coalesce(status, '{_DONE}') = '{_DONE}'",
)
REWARD_HISTORIES = (BFUSD_REWARDS, RWUSD_REWARDS, SOFT_STAKING_REWARDS, CLAIMED_BOOST_REWARDS)

bfusd_reward = reward(BFUSD_REWARDS)
rwusd_reward = reward(RWUSD_REWARDS)
soft_staking_reward = reward(SOFT_STAKING_REWARDS)
_claimed_boost_reward = reward(CLAIMED_BOOST_REWARDS)


async def boost_reward(run: Run, row: Row) -> None:
    """A claimed boost reward (`CLAIM`, not failed) is income on Spot; a distributed one is
    typed only."""
    asked = run.record.record.request.get("type")
    kind = asked if isinstance(asked, str) else None
    await run.typed(type=kind)
    if kind != _CLAIM or text(row, "status") not in (None, _DONE):
        if kind is None:
            run.flag(f"{BOOST_REWARDS}.type missing")
        await remove(run, run.record.key)
        await run.typed(transaction_id=None)
        return
    await _claimed_boost_reward(run, row)
