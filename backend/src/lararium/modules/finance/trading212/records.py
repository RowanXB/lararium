"""Trading 212 items that move money or shares, as connector transactions (design §5.7).

Every item is one transaction under its key (Decision 9), typed in its table. Fees are
postings of their own on the account (`is_fee`), with one Unassigned posting per currency as
their counterpart. A cash movement has exactly two postings, the contra at position 1, so M4
can match it.

Sign rules until the owner's first trade shows Trading 212's: the side decides a fill's
quantity sign; its cash is `walletImpact.netValue`, out for a buy and in for a sell when it is
not negative, as given when it is; each `taxes[]` entry is a fee of -|quantity| in its
currency; the cash besides the fees weighs the shares, at |cash| / quantity per share. A fill
whose postings still do not balance within rounding fails the run, naming the fill. A cash
movement's sign comes from its type; a TRANSFER's is taken as given and flagged.

Opening balances (design §5.5): Trading 212's history depth is undocumented. What the
account's first summary reports (cash and the positions its sync saw), less what the history
posts up to the time that summary arrived, is one opening adjustment per instrument, against
Opening balances, the day before the account's earliest record; every run sets it afresh, so
history arriving later moves it, and a full history leaves none.
"""

from collections.abc import Sequence
from datetime import datetime, time, timedelta
from decimal import Decimal
from uuid import UUID

from sqlalchemy import func, select, text

from lararium.core import models as core_models
from lararium.core.timeutil import local_date
from lararium.modules.finance import models
from lararium.modules.finance.connector_run import (
    holdings,
    not_opening,
    per_unit,
    settle_rounding,
)
from lararium.modules.finance.instruments import InstrumentRecord, get_instrument
from lararium.modules.finance.ledger import CounterpartyFields, PostingInput
from lararium.modules.finance.money import to_plain_str, tolerance
from lararium.modules.finance.report_sightings import first_sighting, list_sightings
from lararium.modules.finance.trading212 import accounts, reports
from lararium.modules.finance.trading212.fields import ENDPOINTS
from lararium.modules.finance.trading212.state import (
    LONDON,
    Item,
    Record,
    Report,
    Run,
    Trading212DataError,
    flat,
    moment_at,
    number_at,
    object_at,
    objects_at,
    record_key,
    reports_of,
    stored,
    text_at,
)

SIDES = frozenset({"BUY", "SELL"})
FILL_TYPES = frozenset(
    """TRADE STOCK_SPLIT STOCK_DISTRIBUTION FOP FOP_CORRECTION CUSTOM_STOCK_DISTRIBUTION
    EQUITY_RIGHTS SCRIP_STOCK_DIVIDENDS STOCK_DIVIDENDS STOCK_ACQUISITION
    CASH_AND_STOCK_ACQUISITION SPIN_OFF""".split()
)
# Shares moved in free of payment: a transfer's leg (design §5.7).
_TRANSFER_FILLS = frozenset({"FOP", "FOP_CORRECTION"})
# A cash movement's type: the sign of the account's leg (None: as given) and its description.
TRANSACTIONS = {
    "DEPOSIT": (1, "Deposit"),
    "WITHDRAW": (-1, "Withdrawal"),
    "FEE": (-1, "Fee"),
    "INTEREST_ON_FREE_CASH": (1, "Interest on cash"),
    "LENDING_INTEREST": (1, "Share lending interest"),
    "TRANSFER": (None, "Transfer"),
}
# Movements with another account of the owner's, or the owner's card (design §5.12).
_THROUGH_CONTRA = frozenset({"DEPOSIT", "WITHDRAW", "TRANSFER"})
TRANSFER = "TRANSFER"
# The owner checks the first real TRANSFER's sign (design §10).
TRANSFER_FLAG = "TRANSFER"
OPENING = "opening:"
OPENING_DESCRIPTION = "Opening balance adjustment"


# Order fills -----------------------------------------------------------------------------


def _fees(
    account_id: UUID, unassigned: UUID, fees: Sequence[tuple[UUID, Decimal, str]]
) -> list[PostingInput]:
    """Each fee on the account, then Unassigned's counterpart per currency."""
    postings: list[PostingInput] = []
    totals: dict[UUID, Decimal] = {}
    for instrument_id, amount, memo in fees:
        postings.append(PostingInput(account_id, instrument_id, amount, is_fee=True, memo=memo))
        totals[instrument_id] = totals.get(instrument_id, Decimal(0)) + amount
    postings += [
        PostingInput(unassigned, instrument_id, -total)
        for instrument_id, total in totals.items()
        if total != 0
    ]
    return postings


def _plain(quantity: Decimal) -> str:
    return to_plain_str(quantity.copy_abs().normalize())


async def _rounding(
    run: Run,
    fill_id: str,
    postings: list[PostingInput],
    unassigned: UUID,
    currency: InstrumentRecord,
    quantity: Decimal,
) -> None:
    """The fill's rounding residual (connector_run.settle_rounding); more than rounding
    fails the run, naming the fill."""
    await settle_rounding(
        run.session,
        postings,
        unassigned,
        currency,
        quantity,
        lambda: Trading212DataError(
            f"Trading 212 order fill {fill_id}: its cash differs from its quantity times"
            " price by more than rounding"
        ),
    )


async def _post_fill(run: Run, record: Record, key: str, order: Item, fill: Item) -> UUID | None:
    """The fill's transaction; None when it posts nothing (flagged when that is a value
    the mapping does not know)."""
    side, fill_type = text_at(order, "side"), text_at(fill, "type")
    if side not in SIDES:
        run.flag_value("orders", "order.side", side)
        return None
    if fill_type not in FILL_TYPES:
        run.flag_value("orders", "fill.type", fill_type)
        return None
    instrument_id = await run.instrument(record, object_at(order, "instrument"))
    if instrument_id is None:
        return None
    fill_id = text_at(fill, "id")
    buy = side == "BUY"
    quantity = number_at(record, fill, "quantity").copy_abs()
    quantity = quantity if buy else -quantity

    unassigned = await run.system_account("unassigned")
    fees: list[tuple[UUID, Decimal, str]] = []
    for tax in objects_at(fill, "walletImpact.taxes"):
        amount = number_at(record, tax, "quantity").copy_abs()
        if amount != 0:
            currency_id = (await run.currency(text_at(tax, "currency"))).id
            fees.append((currency_id, -amount, text_at(tax, "name")))
    net = number_at(record, fill, "walletImpact.netValue")
    cash = (-net if buy else net) if net >= 0 else net
    currency: InstrumentRecord | None = None
    code = text_at(fill, "walletImpact.currency")
    if code:
        currency = await run.currency(code)
    elif cash != 0:
        raise Trading212DataError(f"{record.record_type} lacks fill.walletImpact.currency")
    # The cash besides the fees in its currency: what the shares weigh.
    in_cash = [amount for i, amount, _ in fees if currency is not None and i == currency.id]
    gross = cash - sum(in_cash, Decimal(0))
    occurred_at = (
        moment_at(record, fill, "filledAt")
        if text_at(fill, "filledAt")
        else moment_at(record, order, "createdAt")
    )

    postings: list[PostingInput] = []
    if fill_type == "TRADE":
        if quantity == 0:
            raise Trading212DataError(f"Trading 212 order fill {fill_id} has no quantity")
        price = per_unit(gross.copy_abs(), quantity.copy_abs())
        if currency is None or price == 0:
            # Nothing to weigh the shares by: they come from (or go to) Unassigned.
            postings += [
                PostingInput(run.account, instrument_id, quantity),
                PostingInput(unassigned, instrument_id, -quantity),
            ]
        elif buy:
            postings.append(
                PostingInput(
                    run.account,
                    instrument_id,
                    quantity,
                    cost_per_unit=price,
                    cost_instrument_id=currency.id,
                )
            )
        else:
            postings.append(
                PostingInput(
                    run.account,
                    instrument_id,
                    quantity,
                    price_per_unit=price,
                    price_instrument_id=currency.id,
                )
            )
        if currency is not None and gross != 0:
            postings.append(PostingInput(run.account, currency.id, gross))
        description = f"{side.capitalize()} {_plain(quantity)} {await run.symbol(instrument_id)}"
    else:
        # A corporate action moves shares (and cash, if any) from or to Unassigned; shares
        # moved in free of payment are a transfer's leg (design §5.7).
        cash_postings = 2 if currency is not None and gross != 0 else 0
        others = cash_postings + len(fees) + len({i for i, _, _ in fees})
        if quantity != 0:
            contra = await run.contra(key, others) if fill_type in _TRANSFER_FILLS else unassigned
            postings += [
                PostingInput(run.account, instrument_id, quantity),
                PostingInput(contra, instrument_id, -quantity),
            ]
        if currency is not None and gross != 0:
            postings += [
                PostingInput(run.account, currency.id, gross),
                PostingInput(unassigned, currency.id, -gross),
            ]
        words = fill_type.replace("_", " ").capitalize()
        description = f"{words} {await run.symbol(instrument_id)}"
    postings += _fees(run.account, unassigned, fees)
    if not postings:
        return None
    if fill_type == "TRADE" and currency is not None:
        await _rounding(run, fill_id, postings, unassigned, currency, quantity)
        price = number_at(record, fill, "price")
        if price > 0:
            quote_id, in_quote = await run.price(text_at(order, "instrument.currency"), price)
            run.fill_prices[(instrument_id, quote_id, local_date(occurred_at, LONDON))] = in_quote
    return await run.post(key, occurred_at, description, postings)


async def order(run: Run, record: Record) -> None:
    content = record.content
    key = record_key(record)
    fill = object_at(content, "fill")
    transaction_id = None
    if fill is not None:
        transaction_id = await _post_fill(run, record, key, object_at(content, "order") or {}, fill)
    if transaction_id is None:
        await run.void(key)
    values = await run.typed("orders", "trading212_orders", flat(content))
    parent = {"connection_id": run.connection_id, "external_id": key}
    await run.upsert(
        "trading212_orders",
        parent,
        values | {"transaction_id": transaction_id, "raw_record_id": record.id},
    )
    taxes = objects_at(fill, "walletImpact.taxes")
    for n, tax in enumerate(taxes):
        values = await run.typed(
            "orders", "trading212_order_taxes", flat(tax), "fill.walletImpact.taxes[]."
        )
        await run.upsert(
            "trading212_order_taxes",
            parent | {"item_index": n},
            values | {"raw_record_id": record.id},
        )
    await run.prune("trading212_order_taxes", parent, len(taxes))


def order_time(record: Record) -> tuple[str, str]:
    content = record.content
    return text_at(content, "fill.filledAt") or text_at(content, "order.createdAt"), record_key(
        record
    )


# Dividends and cash movements ------------------------------------------------------------


async def dividend(run: Run, record: Record) -> None:
    content = record.content
    key = record_key(record)
    transaction_id = None
    instrument_id = await run.instrument(record, object_at(content, "instrument"))
    amount = number_at(record, content, "amount")
    if instrument_id is not None and amount != 0:
        currency = await run.currency(text_at(content, "currency"))
        unassigned = await run.system_account("unassigned")
        transaction_id = await run.post(
            key,
            moment_at(record, content, "paidOn"),
            f"Dividend {await run.symbol(instrument_id)}",
            [
                PostingInput(run.account, currency.id, amount),
                PostingInput(unassigned, currency.id, -amount),
            ],
        )
    if transaction_id is None:
        await run.void(key)
    values = await run.typed("dividends", "trading212_dividends", flat(content))
    reference = values.pop("reference")
    await run.upsert(
        "trading212_dividends",
        {"connection_id": run.connection_id, "reference": reference},
        values | {"transaction_id": transaction_id, "raw_record_id": record.id},
    )


async def cash_movement(run: Run, record: Record) -> None:
    content = record.content
    key = record_key(record)
    kind = text_at(content, "type")
    transaction_id = None
    if kind not in TRANSACTIONS:
        run.flag_value("transactions", "type", kind)
    else:
        sign, description = TRANSACTIONS[kind]
        amount = number_at(record, content, "amount")
        if sign is None:
            run.flag(TRANSFER_FLAG)
        else:
            amount = amount.copy_abs() * sign
        if amount != 0:
            currency = await run.currency(text_at(content, "currency"))
            # A TRANSFER is with the sibling account when there is one (accounts.sibling);
            # a deposit or withdrawal names nothing: the card payment pairs with it.
            identifier = await accounts.sibling(run) if kind == TRANSFER else None
            contra = (
                await run.contra(key, 0, identifier)
                if kind in _THROUGH_CONTRA
                else await run.system_account("unassigned")
            )
            transaction_id = await run.post(
                key,
                moment_at(record, content, "dateTime"),
                description,
                [
                    PostingInput(run.account, currency.id, amount),
                    PostingInput(contra, currency.id, -amount),
                ],
                CounterpartyFields(identifier_id=identifier),
            )
    if transaction_id is None:
        await run.void(key)
    values = await run.typed("transactions", "trading212_transactions", flat(content))
    reference = values.pop("reference")
    await run.upsert(
        "trading212_transactions",
        {"connection_id": run.connection_id, "reference": reference},
        values | {"transaction_id": transaction_id, "raw_record_id": record.id},
    )


async def unidentified_transfers(run: Run) -> list[Record]:
    """The connection's stored TRANSFERs posted before the sibling was known: once it is,
    they are posted again, naming it."""
    if await accounts.sibling(run) is None:
        return []
    t, r = models.Transaction, core_models.RawRecord
    keys = set(
        await run.session.scalars(
            select(t.external_id).where(
                t.connection_id == run.connection_id,
                t.external_id.like("transaction:%"),
                t.status != "void",
                t.counterparty_identifier_id.is_(None),
            )
        )
    )
    if not keys:
        return []
    latest: dict[str, Record] = {}
    for record in await stored(
        run.session, run.connection_id, ["transactions"], r.content["type"].astext == TRANSFER
    ):
        if record_key(record) in keys:
            latest[record_key(record)] = record
    return list(latest.values())


# Openings ------------------------------------------------------------------------------


async def _earliest_report(run: Run) -> tuple[Report, list[Record]] | None:
    """The account's first summary sighting, and the positions its sync saw."""
    first = await first_sighting(run.session, run.connection_id, ENDPOINTS["summary"].record_type)
    if first is None:
        return None
    seen = await reports_of(
        run.session, await list_sightings(run.session, run.connection_id, sync_id=first.sync_id)
    )
    [summary] = [r for r in seen if r.record.id == first.raw_record_id]
    return summary, [r.record for r in seen if r.endpoint == "positions"]


async def openings(run: Run) -> None:
    """Set the account's opening adjustments (module doc): its first summary's report, at the
    time it arrived, less what the history posts up to then."""
    earliest = await _earliest_report(run)
    if earliest is None:
        return
    summary, positions = earliest
    found = await reports.reported(run, summary.record, positions, current=False)
    held = await holdings(run.session, run.account, summary.seen_at, not_opening())
    t, p = models.Transaction, models.Posting
    live = t.status != "void"
    prefix = f"{OPENING}{run.trading212_id}:"
    current = {
        UUID(key.removeprefix(prefix))
        for key in await run.session.scalars(
            select(t.external_id).where(
                t.connection_id == run.connection_id, t.external_id.like(f"{prefix}%"), live
            )
        )
        if key is not None
    }
    first = await run.session.scalar(
        select(func.min(t.booking_date))
        .join(p, p.transaction_id == t.id)
        .where(p.account_id == run.account, live, not_opening())
    )
    start = summary.day
    day = (min(start, first) if first is not None else start) - timedelta(days=1)
    occurred_at = datetime.combine(day, time(), LONDON)
    opening_balances = await run.system_account("opening_balances")
    for instrument_id in sorted(set(found) | set(held) | current):
        key = f"{prefix}{instrument_id}"
        difference = found.get(instrument_id, Decimal(0)) - held.get(instrument_id, Decimal(0))
        decimals = (await get_instrument(run.session, instrument_id)).decimals
        if difference.copy_abs() <= tolerance(decimals):
            await run.void(key)
            continue
        await run.post(
            key,
            occurred_at,
            OPENING_DESCRIPTION,
            [
                PostingInput(run.account, instrument_id, difference),
                PostingInput(opening_balances, instrument_id, -difference),
            ],
        )


async def void_unsupported(run: Run, instrument_ids: Sequence[UUID]) -> None:
    """Void what the connection posted on instruments its metadata now says the ledger does
    not hold: their fills, and the dividends paid on them."""
    if not instrument_ids:
        return
    t, p = models.Transaction, models.Posting
    keys = set(
        await run.session.scalars(
            select(t.external_id)
            .join(p, p.transaction_id == t.id)
            .where(
                t.connection_id == run.connection_id,
                t.status != "void",
                p.instrument_id.in_(instrument_ids),
                not_opening(),
            )
        )
    )
    keys |= set(
        await run.session.scalars(
            text(
                "select t.external_id from finance.trading212_dividends d"
                " join finance.transactions t on t.id = d.transaction_id"
                " join finance.instrument_identifiers ii on ii.scheme = 'trading212_ticker'"
                " and ii.value = d.instrument_ticker"
                " where d.connection_id = :c and t.status <> 'void'"
                " and ii.instrument_id = any(cast(:i as uuid[]))"
            ),
            {"c": run.connection_id, "i": list(instrument_ids)},
        )
    )
    for key in sorted(k for k in keys if k is not None):
        await run.void(key)
    # As a row that posts nothing: its typed row links no transaction (a replay's does not).
    for table in ("trading212_orders", "trading212_dividends"):
        await run.session.execute(
            text(
                f"update finance.{table} r set transaction_id = null"
                " from finance.transactions t"
                " where t.id = r.transaction_id and r.connection_id = :c"
                " and t.external_id = any(cast(:k as text[]))"
            ),
            {"c": run.connection_id, "k": sorted(k for k in keys if k is not None)},
        )
