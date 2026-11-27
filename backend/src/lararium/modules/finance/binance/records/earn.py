"""Simple Earn (flexible and locked) and on-chain yields: subscriptions, redemptions and rewards
(design 5.7); and the rewards rule 3 links, for every product (`Rewards`).

A subscription or redemption moves an asset between two Binance wallets: one transaction with
a posting on each, no In transit (rule 1). Only a finished one is posted (`SUCCESS`, `PAID`): a
pending one has not been counted yet and a failed one moved nothing. A locked or on-chain
redemption names no wallet: one that ends the position (at maturity, or early) goes to Spot, and
one into Simple Earn flexible stays in the Earn wallet, so it posts nothing.

A reward is income on its product's wallet; one Binance paid out as an asset dividend is linked
to it instead (rule 3, `normalize.link_payouts`), so its posting is removed there. The flexible
collateral record moves an asset pledged inside Earn: it is typed only.
"""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import text as sql

from lararium.modules.finance.binance import layout
from lararium.modules.finance.binance.fetch import record_type
from lararium.modules.finance.binance.parse import Row
from lararium.modules.finance.binance.records import (
    Handler,
    Run,
    amount,
    post,
    remove,
    text,
    when,
)
from lararium.modules.finance.ledger import PostingInput

SUBSCRIPTIONS = "earn-flexible-subscriptions"
REDEMPTIONS = "earn-flexible-redemptions"
REWARDS = "earn-flexible-rewards"
LOCKED_SUBSCRIPTIONS = "earn-locked-subscriptions"
LOCKED_REDEMPTIONS = "earn-locked-redemptions"
LOCKED_REWARDS = "earn-locked-rewards"
ONCHAIN_SUBSCRIPTIONS = "onchain-subscriptions"
ONCHAIN_REDEMPTIONS = "onchain-redemptions"
ONCHAIN_REWARDS = "onchain-rewards"
_WALLETS = {"SPOT": "spot", "FUNDING": "funding"}
_BOTH = "SPOTANDFUNDING"
_SUBSCRIBED = "SUCCESS"
_REDEEMED = "PAID"
# Where a locked or on-chain redemption goes, by its `type`: to Spot when it ends the position
# (the catalogue: MATURE "to Spot"; an early one is taken to go there too), None when it moves
# into Simple Earn flexible (the Earn wallet holds both).
_LOCKED_TO = {"MATURE": "spot", "AHEAD": "spot", "NEW_TRANSFERRED": None}
_ONCHAIN_TO = {"NORMAL": "spot", "EARLY": "spot", "CONVERT": None}


async def _sources(
    run: Run, label: str, row: Row, total: Decimal
) -> list[tuple[UUID, Decimal]] | None:
    """The wallets a subscription took its amount from, with what each gave."""
    source = text(row, "sourceAccount")
    if source in _WALLETS:
        return [(await run.account(_WALLETS[source]), total)]
    if source != _BOTH:
        if source is None:
            run.flag(f"{label}.sourceAccount missing")
        return None  # a value the catalogue does not list is flagged by the parser
    spot = amount(label, row, "amtFromSpot") or Decimal(0)
    funding = amount(label, row, "amtFromFunding") or Decimal(0)
    if spot + funding != total:
        run.flag(f"{label}.amtFromSpot and amtFromFunding do not make the amount")
        return None
    return [(await run.account("spot"), spot), (await run.account("funding"), funding)]


def subscription(label: str, description: str) -> Handler:
    """A subscription of `label`: `-amount` on the wallets it came from, `+amount` on Earn."""

    async def handle(run: Run, row: Row) -> None:
        if text(row, "status") != _SUBSCRIBED:
            await remove(run, run.record.key)
            await run.typed(transaction_id=None)
            return
        coin = text(row, "asset") or ""
        instrument_id = await run.asset(coin)
        quantity = amount(label, row, "amount")
        occurred_at = when(label, row, "time")
        if instrument_id is None or quantity is None or occurred_at is None:
            return
        sources = await _sources(run, label, row, quantity)
        if sources is None:
            return
        postings = [
            PostingInput(account_id, instrument_id, -given) for account_id, given in sources
        ]
        postings.append(PostingInput(await run.account("earn"), instrument_id, quantity))
        transaction_id = await post(
            run,
            run.record.key,
            occurred_at=occurred_at,
            description=f"{description} {coin}",
            postings=postings,
        )
        await run.typed(transaction_id=transaction_id)

    return handle


flexible_subscription = subscription(SUBSCRIPTIONS, "Earn subscription")
locked_subscription = subscription(LOCKED_SUBSCRIPTIONS, "Locked Earn subscription")
onchain_subscription = subscription(ONCHAIN_SUBSCRIPTIONS, "On-chain yields subscription")


async def _redeem(
    run: Run, label: str, row: Row, destination: str, description: str
) -> UUID | None:
    """`-amount` on Earn, `+amount` on the destination wallet."""
    coin = text(row, "asset") or ""
    instrument_id = await run.asset(coin)
    quantity = amount(label, row, "amount")
    occurred_at = when(label, row, "time")
    if instrument_id is None or quantity is None or occurred_at is None:
        return None
    return await post(
        run,
        run.record.key,
        occurred_at=occurred_at,
        description=f"{description} {coin}",
        postings=[
            PostingInput(await run.account("earn"), instrument_id, -quantity),
            PostingInput(await run.account(destination), instrument_id, quantity),
        ],
    )


async def flexible_redemption(run: Run, row: Row) -> None:
    if text(row, "status") != _REDEEMED:
        return
    destination = text(row, "destAccount")
    if destination not in _WALLETS:
        if destination is None:
            run.flag(f"{REDEMPTIONS}.destAccount missing")
        return
    transaction_id = await _redeem(run, REDEMPTIONS, row, _WALLETS[destination], "Earn redemption")
    await run.typed(transaction_id=transaction_id)


def locked_redemption(label: str, to: dict[str, str | None], description: str) -> Handler:
    """A locked or on-chain redemption, paid (`PAID`): to the wallet its `type` goes to."""

    async def handle(run: Run, row: Row) -> None:
        kind = text(row, "type")
        if kind is None:
            run.flag(f"{label}.type missing")
        destination = to.get(kind or "")
        if text(row, "status") != _REDEEMED or destination is None:
            # Not paid yet, into flexible (it stays in Earn), or a type the parser flags.
            await remove(run, run.record.key)
            await run.typed(transaction_id=None)
            return
        await run.typed(transaction_id=await _redeem(run, label, row, destination, description))

    return handle


locked_earn_redemption = locked_redemption(LOCKED_REDEMPTIONS, _LOCKED_TO, "Locked Earn redemption")
onchain_redemption = locked_redemption(
    ONCHAIN_REDEMPTIONS, _ONCHAIN_TO, "On-chain yields redemption"
)


# Rewards (rule 3).


@dataclass(frozen=True)
class Rewards:
    """A reward history: its label, the wallet a reward that no dividend paid accrues on, the
    row's fields of its asset (`coin`: the asset when the row names none) and amount, and which
    of its rows can be a dividend's payout (`linkable`, SQL on the label's table)."""

    label: str
    wallet: str
    asset_field: str | None
    amount_field: str
    description: str
    coin: str | None = None
    linkable: str = "true"

    @property
    def table(self) -> str:
        return layout.table_name(self.label)

    @property
    def record(self) -> str:
        return record_type(self.label).removeprefix("binance.")

    @property
    def asset_sql(self) -> str:
        """The reward's asset in the label's table."""
        if self.asset_field is None:
            assert self.coin is not None and self.coin.isalnum()
            return f"'{self.coin}'"
        return layout.snake(self.asset_field)

    @property
    def amount_sql(self) -> str:
        return layout.snake(self.amount_field)

    def asset_of(self, run: Run, row: Row) -> str | None:
        if self.asset_field is None:
            return self.coin
        found = text(row, self.asset_field)
        if found is None:
            run.flag(f"{self.label}.{self.asset_field} missing")
        return found


async def linked(run: Run, rewards: Rewards) -> bool:
    """Whether the current reward is linked to its dividend."""
    key = run.record.typed_key
    found = await run.session.scalar(
        sql(
            f"select dividend_id from finance.{rewards.table}"
            " where connection_id = :c and content_hash = :h and occurrence = :o"
        ),
        {"c": key["connection_id"], "h": key[layout.CONTENT_HASH], "o": key[layout.OCCURRENCE]},
    )
    return found is not None


async def post_reward(
    run: Run, rewards: Rewards, key: str, coin: str, quantity: Decimal, occurred_at: datetime
) -> UUID | None:
    """`+amount` on the product's wallet against Unassigned."""
    instrument_id = await run.asset(coin)
    if instrument_id is None:
        return None
    return await post(
        run,
        key,
        occurred_at=occurred_at,
        description=f"{rewards.description} {coin}",
        postings=[
            PostingInput(await run.account(rewards.wallet), instrument_id, quantity),
            PostingInput(run.unassigned, instrument_id, -quantity),
        ],
    )


def reward(rewards: Rewards) -> Handler:
    """A reward of the history; one `link_payouts` has linked to its dividend posts nothing."""

    async def handle(run: Run, row: Row) -> None:
        key = run.record.key
        if await linked(run, rewards):
            await remove(run, key)
            await run.typed(transaction_id=None)
            return
        coin = rewards.asset_of(run, row)
        quantity = amount(rewards.label, row, rewards.amount_field)
        occurred_at = when(rewards.label, row, "time")
        if coin is None or quantity is None or occurred_at is None:
            return
        transaction_id = await post_reward(run, rewards, key, coin, quantity, occurred_at)
        await run.typed(transaction_id=transaction_id)

    return handle


# The reward types known to be paid out as an asset dividend (rule 3); any other type posts on
# Earn, and a payout of one would show as a reconciliation difference.
PAID_OUT = ("BONUS",)
FLEXIBLE_REWARDS = Rewards(
    REWARDS,
    "earn",
    "asset",
    "rewards",
    "Earn reward",
    linkable=f"type in ({', '.join(f"'{kind}'" for kind in PAID_OUT)})",
)
# A locked or on-chain reward's type does not say whether it is paid out (the catalogue: one
# example, "Locked Rewards"): every one can be its dividend.
LOCKED_EARN_REWARDS = Rewards(LOCKED_REWARDS, "earn", "asset", "amount", "Locked Earn reward")
ONCHAIN_YIELD_REWARDS = Rewards(
    ONCHAIN_REWARDS, "earn", "asset", "amount", "On-chain yields reward"
)
REWARD_HISTORIES = (FLEXIBLE_REWARDS, LOCKED_EARN_REWARDS, ONCHAIN_YIELD_REWARDS)

flexible_reward = reward(FLEXIBLE_REWARDS)
locked_reward = reward(LOCKED_EARN_REWARDS)
onchain_reward = reward(ONCHAIN_YIELD_REWARDS)
