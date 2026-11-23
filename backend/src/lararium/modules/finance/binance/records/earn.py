"""Flexible Simple Earn: subscriptions, redemptions and rewards (design 5.7).

A subscription or redemption moves an asset between two Binance wallets: one transaction with
a posting on each, no In transit (rule 1). Only a finished one is posted (`SUCCESS`, `PAID`): a
pending one has not been counted yet and a failed one moved nothing. A reward is income on
Earn; one Binance paid out as an asset dividend is linked to it instead (rule 3,
`normalize.link_payouts`), so its posting is removed there.
"""

from decimal import Decimal
from uuid import UUID

from sqlalchemy import text as sql

from lararium.modules.finance.binance import layout
from lararium.modules.finance.binance.parse import Row
from lararium.modules.finance.binance.records import (
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
_WALLETS = {"SPOT": "spot", "FUNDING": "funding"}
_BOTH = "SPOTANDFUNDING"
_SUBSCRIBED = "SUCCESS"
_REDEEMED = "PAID"


async def _sources(run: Run, row: Row, total: Decimal) -> list[tuple[UUID, Decimal]] | None:
    """The wallets a subscription took its amount from, with what each gave."""
    source = text(row, "sourceAccount")
    if source in _WALLETS:
        return [(await run.account(_WALLETS[source]), total)]
    if source != _BOTH:
        if source is None:
            run.flag(f"{SUBSCRIPTIONS}.sourceAccount missing")
        return None  # a value the catalogue does not list is flagged by the parser
    spot = amount(SUBSCRIPTIONS, row, "amtFromSpot") or Decimal(0)
    funding = amount(SUBSCRIPTIONS, row, "amtFromFunding") or Decimal(0)
    if spot + funding != total:
        run.flag(f"{SUBSCRIPTIONS}.amtFromSpot and amtFromFunding do not make the amount")
        return None
    return [(await run.account("spot"), spot), (await run.account("funding"), funding)]


async def flexible_subscription(run: Run, row: Row) -> None:
    if text(row, "status") != _SUBSCRIBED:
        return
    coin = text(row, "asset") or ""
    instrument_id = await run.asset(coin)
    quantity = amount(SUBSCRIPTIONS, row, "amount")
    occurred_at = when(SUBSCRIPTIONS, row, "time")
    if instrument_id is None or quantity is None or occurred_at is None:
        return
    sources = await _sources(run, row, quantity)
    if sources is None:
        return
    postings = [PostingInput(account_id, instrument_id, -given) for account_id, given in sources]
    postings.append(PostingInput(await run.account("earn"), instrument_id, quantity))
    transaction_id = await post(
        run,
        run.record.key,
        occurred_at=occurred_at,
        description=f"Earn subscription {coin}",
        postings=postings,
    )
    await run.typed(transaction_id=transaction_id)


async def flexible_redemption(run: Run, row: Row) -> None:
    if text(row, "status") != _REDEEMED:
        return
    destination = text(row, "destAccount")
    if destination not in _WALLETS:
        if destination is None:
            run.flag(f"{REDEMPTIONS}.destAccount missing")
        return
    coin = text(row, "asset") or ""
    instrument_id = await run.asset(coin)
    quantity = amount(REDEMPTIONS, row, "amount")
    occurred_at = when(REDEMPTIONS, row, "time")
    if instrument_id is None or quantity is None or occurred_at is None:
        return
    transaction_id = await post(
        run,
        run.record.key,
        occurred_at=occurred_at,
        description=f"Earn redemption {coin}",
        postings=[
            PostingInput(await run.account("earn"), instrument_id, -quantity),
            PostingInput(await run.account(_WALLETS[destination]), instrument_id, quantity),
        ],
    )
    await run.typed(transaction_id=transaction_id)


async def linked(run: Run) -> bool:
    """Whether the current reward is linked to its dividend."""
    key = run.record.typed_key
    found = await run.session.scalar(
        sql(
            "select dividend_id from finance.binance_earn_flexible_rewards"
            " where connection_id = :c and content_hash = :h and occurrence = :o"
        ),
        {"c": key["connection_id"], "h": key[layout.CONTENT_HASH], "o": key[layout.OCCURRENCE]},
    )
    return found is not None


async def flexible_reward(run: Run, row: Row) -> None:
    """`+rewards` on Earn against Unassigned, of every type; a reward `link_payouts` has
    linked to its dividend posts nothing."""
    key = run.record.key
    if await linked(run):
        await remove(run, key)
        await run.typed(transaction_id=None)
        return
    coin = text(row, "asset") or ""
    instrument_id = await run.asset(coin)
    quantity = amount(REWARDS, row, "rewards")
    occurred_at = when(REWARDS, row, "time")
    if instrument_id is None or quantity is None or occurred_at is None:
        return
    transaction_id = await post(
        run,
        key,
        occurred_at=occurred_at,
        description=f"Earn reward {coin}",
        postings=[
            PostingInput(await run.account("earn"), instrument_id, quantity),
            PostingInput(run.unassigned, instrument_id, -quantity),
        ],
    )
    await run.typed(transaction_id=transaction_id)
