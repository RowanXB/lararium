"""Wallet-to-wallet transfers and Cloud Mining (design 5.7 rules 1 and 2).

A transfer between two Binance wallets is one transaction with a posting on each account and
no In transit (rule 1): one record describes both sides, so it is never a transfer leg. Only a
`CONFIRMED` one is posted. The three isolated-margin types are asked once per isolated pair;
the row does not name the pair, so the requested one is stored with it.

A movement two endpoints both list is posted once (rule 2). The listings are taken in order
(`Listing`, `normalize.MOVEMENT_LISTINGS`; the universal transfer history first, then the
margin transfer history, the USDⓈ-M and COIN-M futures income `TRANSFER` rows and the options
bill's `TRANSFER` rows): a movement of a later listing with one of an earlier listing, posted,
of the same asset and amount between the same two wallets within 60 seconds is linked to it and
not posted (`link_movements`). With none, it is posted by rule 1 itself. A futures income or
options bill row names only its own wallet: its other side matches any wallet, and posted by
itself it is Unassigned.

Cloud Mining has no wallet of its own on Binance: a payment leaves Spot and a refund comes back
to it, against Unassigned.
"""

from collections import defaultdict
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from uuid import UUID

from sqlalchemy import text as sql

from lararium.modules.finance.binance.parse import Row
from lararium.modules.finance.binance.records import (
    Run,
    amount,
    integer,
    post,
    remove,
    text,
    when,
)
from lararium.modules.finance.ledger import PostingInput

UNIVERSAL = "universal-transfer"
CLOUD_MINING = "cloud-mining"
_CONFIRMED = "CONFIRMED"
# Rule 2: two listings of one movement are at most this far apart.
LINK_WINDOW = timedelta(seconds=60)

# Each documented `type` (the catalogue's enum) to the wallet it moves from and the wallet it
# moves to: MAIN is Spot, MARGIN cross margin.
TRANSFER_WALLETS: dict[str, tuple[str, str]] = {
    "MAIN_UMFUTURE": ("spot", "um_futures"),
    "MAIN_CMFUTURE": ("spot", "cm_futures"),
    "MAIN_MARGIN": ("spot", "cross_margin"),
    "UMFUTURE_MAIN": ("um_futures", "spot"),
    "UMFUTURE_MARGIN": ("um_futures", "cross_margin"),
    "CMFUTURE_MAIN": ("cm_futures", "spot"),
    "CMFUTURE_MARGIN": ("cm_futures", "cross_margin"),
    "MARGIN_MAIN": ("cross_margin", "spot"),
    "MARGIN_UMFUTURE": ("cross_margin", "um_futures"),
    "MARGIN_CMFUTURE": ("cross_margin", "cm_futures"),
    "ISOLATEDMARGIN_MARGIN": ("isolated_margin", "cross_margin"),
    "MARGIN_ISOLATEDMARGIN": ("cross_margin", "isolated_margin"),
    "ISOLATEDMARGIN_ISOLATEDMARGIN": ("isolated_margin", "isolated_margin"),
    "MAIN_FUNDING": ("spot", "funding"),
    "FUNDING_MAIN": ("funding", "spot"),
    "FUNDING_UMFUTURE": ("funding", "um_futures"),
    "UMFUTURE_FUNDING": ("um_futures", "funding"),
    "MARGIN_FUNDING": ("cross_margin", "funding"),
    "FUNDING_MARGIN": ("funding", "cross_margin"),
    "FUNDING_CMFUTURE": ("funding", "cm_futures"),
    "CMFUTURE_FUNDING": ("cm_futures", "funding"),
    "MAIN_OPTION": ("spot", "options"),
    "OPTION_MAIN": ("options", "spot"),
    "UMFUTURE_OPTION": ("um_futures", "options"),
    "OPTION_UMFUTURE": ("options", "um_futures"),
    "MARGIN_OPTION": ("cross_margin", "options"),
    "OPTION_MARGIN": ("options", "cross_margin"),
    "FUNDING_OPTION": ("funding", "options"),
    "OPTION_FUNDING": ("options", "funding"),
    "MAIN_PORTFOLIO_MARGIN": ("spot", "portfolio_margin"),
    "PORTFOLIO_MARGIN_MAIN": ("portfolio_margin", "spot"),
}
# Cloud Mining `type`: 248 a payment (out of Spot), 249 a refund (back to it).
_CLOUD_SIGNS = {248: -1, 249: 1}
_CLOUD_DONE = "S"


@dataclass(frozen=True)
class Movement:
    """One listing of a wallet-to-wallet movement: its record's ledger key, what moved, between
    which wallets (from, to) and when; and the key of the movement it is linked to now. A side
    the listing does not name (a futures income row's other wallet) is None."""

    key: str
    asset: str
    amount: Decimal
    wallets: tuple[str | None, str | None]
    at: datetime
    linked_to: str | None = None


type Movements = Callable[[Run], Awaitable[list[Movement]]]
# Store that a movement is linked to the movement `target` (None: no longer linked) and the
# transaction it posts now (None while linked).
type Link = Callable[[Run, Movement, str | None, UUID | None], Awaitable[None]]


@dataclass(frozen=True)
class Listing:
    """An endpoint that lists wallet-to-wallet movements, for rule 2: the label, every
    movement of the connection it lists and is posting (or linked), and how a movement's link
    is stored. The first listing's movements are never linked, so it needs no `link`."""

    label: str
    movements: Movements
    link: Link | None = None


async def post_movement(run: Run, movement: Movement) -> UUID | None:
    """Rule 1: `-amount` on the first wallet's account and `+amount` on the second's, one
    transaction keyed by the record; never a transfer leg. A side the listing does not name is
    Unassigned."""
    instrument_id = await run.asset(movement.asset)
    if instrument_id is None:
        return None

    async def account(wallet: str | None) -> UUID:
        return run.unassigned if wallet is None else await run.account(wallet)

    source, destination = movement.wallets
    return await post(
        run,
        movement.key,
        occurred_at=movement.at,
        description=f"Transfer {movement.asset}",
        postings=[
            PostingInput(await account(source), instrument_id, -movement.amount),
            PostingInput(await account(destination), instrument_id, movement.amount),
        ],
    )


def same_wallets(one: tuple[str | None, str | None], other: tuple[str | None, str | None]) -> bool:
    """Whether two listings can be of one movement: on each side the same wallet, or one of them
    not naming it, and at least one side named by both (two rows that each name only another
    side say nothing of one movement)."""
    sides = list(zip(one, other, strict=True))
    if any(a is not None and b is not None and a != b for a, b in sides):
        return False
    return any(a is not None and b is not None for a, b in sides)


def _request_text(run: Run, name: str) -> str | None:
    value = run.record.record.request.get(name)
    return value if isinstance(value, str) and value else None


async def universal(run: Run, row: Row) -> None:
    await run.typed(
        from_symbol=_request_text(run, "fromSymbol"), to_symbol=_request_text(run, "toSymbol")
    )
    kind = text(row, "type")
    wallets = TRANSFER_WALLETS.get(kind or "")
    if wallets is None:
        # A type the catalogue does not list is flagged by the parser.
        if kind is None:
            run.flag(f"{UNIVERSAL}.type missing")
        return
    key = run.record.key
    if text(row, "status") != _CONFIRMED:
        # Pending: not moved yet; failed: moved nothing.
        await remove(run, key)
        await run.typed(transaction_id=None)
        return
    coin = text(row, "asset") or ""
    quantity = amount(UNIVERSAL, row, "amount")
    occurred_at = when(UNIVERSAL, row, "timestamp")
    if quantity is None or occurred_at is None:
        return
    movement = Movement(key, coin, quantity, wallets, occurred_at)
    await run.typed(transaction_id=await post_movement(run, movement))


async def _universal_movements(run: Run) -> list[Movement]:
    rows = await run.session.execute(
        sql(
            "select tran_id, asset, amount, type, timestamp"
            " from finance.binance_universal_transfer"
            " where connection_id = :c and status = :s and type = any(:types)"
        ),
        {"c": run.connection.id, "s": _CONFIRMED, "types": list(TRANSFER_WALLETS)},
    )
    return [
        Movement(
            f"universal_transfer:{r.tran_id}",
            r.asset,
            r.amount,
            TRANSFER_WALLETS[r.type],
            r.timestamp,
        )
        for r in rows
    ]


# The universal transfer history: the first listing of any movement it lists.
UNIVERSAL_LISTING = Listing(UNIVERSAL, _universal_movements)


async def link_movements(run: Run, listings: Sequence[Listing]) -> None:
    """Rule 2 over every movement the listings hold, in the listings' order: a movement of a
    later listing is linked to the nearest posted movement of an earlier one with the same
    asset, amount and wallets (`same_wallets`) within 60 seconds, and its own posting removed;
    one no longer linked is posted by rule 1 again.

    The links are worked out afresh each run, so they do not depend on the order the records
    arrived in: pairs nearest first (ties by time and key), each movement of a listing linked
    at most once, each posted movement taken at most once by each later listing. Only a link
    that changes is written."""
    posted: defaultdict[tuple[str, Decimal], list[Movement]] = defaultdict(list)
    for index, listing in enumerate(listings):
        movements = await listing.movements(run)
        wanted: dict[str, str] = {}
        if index > 0:
            pairs: list[tuple[timedelta, datetime, str, datetime, str]] = []
            for movement in movements:
                for target in posted[(movement.asset, movement.amount)]:
                    apart = abs(movement.at - target.at)
                    if apart <= LINK_WINDOW and same_wallets(movement.wallets, target.wallets):
                        pairs.append((apart, movement.at, movement.key, target.at, target.key))
            taken: set[str] = set()
            for _, _, key, _, target_key in sorted(pairs):
                if key in wanted or target_key in taken:
                    continue
                wanted[key] = target_key
                taken.add(target_key)
        for movement in movements:
            target = wanted.get(movement.key)
            if target is None:
                posted[(movement.asset, movement.amount)].append(movement)
            if target == movement.linked_to:
                continue
            assert listing.link is not None, f"{listing.label} cannot be linked"
            if target is not None:
                await remove(run, movement.key)
                await listing.link(run, movement, target, None)
            else:
                transaction_id = await post_movement(run, movement)
                await listing.link(run, movement, None, transaction_id)


async def cloud_mining(run: Run, row: Row) -> None:
    sign = _CLOUD_SIGNS.get(integer(row, "type") or 0)
    status = text(row, "status")
    if sign is None or status not in (None, _CLOUD_DONE):
        # Binance lists only successful ones; another type or status is flagged by the parser.
        return
    coin = text(row, "asset") or ""
    instrument_id = await run.asset(coin)
    quantity = amount(CLOUD_MINING, row, "amount")
    occurred_at = when(CLOUD_MINING, row, "createTime")
    if instrument_id is None or quantity is None or occurred_at is None:
        return
    transaction_id = await post(
        run,
        run.record.key,
        occurred_at=occurred_at,
        description=f"Cloud Mining {'refund' if sign > 0 else 'payment'} {coin}",
        postings=[
            PostingInput(await run.account("spot"), instrument_id, sign * quantity),
            PostingInput(run.unassigned, instrument_id, -sign * quantity),
        ],
    )
    await run.typed(transaction_id=transaction_id)
