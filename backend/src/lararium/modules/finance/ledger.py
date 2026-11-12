from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import MAX_PREC, Decimal, localcontext
from typing import Literal, cast
from uuid import UUID

from sqlalchemy import Numeric, case, func, insert, literal, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from lararium.core.commands import CommandContext
from lararium.core.entities import get_self_entity_id
from lararium.core.json_types import JSONValue
from lararium.core.settings_store import get_timezone
from lararium.core.timeutil import local_date
from lararium.modules.finance import models
from lararium.modules.finance.accounts import get_account
from lararium.modules.finance.errors import (
    FinanceValidationError,
    LedgerImbalanceError,
    TransactionNotFoundError,
)
from lararium.modules.finance.instruments import InstrumentRecord, get_instrument
from lararium.modules.finance.money import require_finite, to_plain_str, tolerance

TransactionStatus = Literal["pending", "booked", "inferred", "confirmed", "void"]
TransactionOrigin = Literal["connector", "manual", "inference", "rule"]


@dataclass(frozen=True)
class PostingInput:
    account_id: UUID
    instrument_id: UUID
    quantity: Decimal
    cost_per_unit: Decimal | None = None
    cost_instrument_id: UUID | None = None
    price_per_unit: Decimal | None = None
    price_instrument_id: UUID | None = None
    is_fee: bool = False
    memo: str = ""


@dataclass(frozen=True)
class TransactionInput:
    occurred_at: datetime  # must have a timezone
    description: str
    postings: Sequence[PostingInput]
    booking_date: date | None = None
    status: TransactionStatus = "booked"
    origin: TransactionOrigin = "manual"
    occurred_tz: str | None = None
    value_date: date | None = None
    counterparty_entity_id: UUID | None = None


@dataclass(frozen=True)
class PostingRecord:
    id: UUID
    position: int
    account_id: UUID
    instrument_id: UUID
    quantity: Decimal
    cost_per_unit: Decimal | None
    cost_instrument_id: UUID | None
    price_per_unit: Decimal | None
    price_instrument_id: UUID | None
    is_fee: bool
    memo: str


@dataclass(frozen=True)
class TransactionRecord:
    id: UUID
    occurred_at: datetime
    booking_date: date
    description: str
    status: TransactionStatus
    origin: TransactionOrigin
    postings: tuple[PostingRecord, ...]  # sorted by position


def _weight(posting: PostingInput) -> tuple[UUID, Decimal]:
    if posting.cost_per_unit is not None and posting.cost_instrument_id is not None:
        return posting.cost_instrument_id, posting.quantity * posting.cost_per_unit
    if posting.price_per_unit is not None and posting.price_instrument_id is not None:
        return posting.price_instrument_id, posting.quantity * posting.price_per_unit
    return posting.instrument_id, posting.quantity


def _validate_posting(posting: PostingInput) -> None:
    """Every rule the postings table CHECKs enforce, so breaking one is a validation error
    and the CHECKs stay a backstop."""
    amounts = {
        "quantity": posting.quantity,
        "cost_per_unit": posting.cost_per_unit,
        "price_per_unit": posting.price_per_unit,
    }
    for name, amount in amounts.items():
        if amount is not None:
            require_finite(name, amount)
    if posting.quantity == 0:
        raise FinanceValidationError("quantity must not be zero")
    # A cost or price converts the quantity into another instrument for the balance check.
    # In the posting's own instrument it would rescale the quantity by any factor and so
    # balance anything.
    for leg, instrument_id in (
        ("cost", posting.cost_instrument_id),
        ("price", posting.price_instrument_id),
    ):
        if instrument_id == posting.instrument_id:
            raise FinanceValidationError(
                f"a posting can't be weighed in its own instrument: {leg}_instrument_id"
                " equals instrument_id"
            )


async def _resolve_references(
    session: AsyncSession, postings: Sequence[PostingInput]
) -> dict[UUID, InstrumentRecord]:
    """Look up every account and instrument the postings name, so a missing one is a
    not-found error rather than a foreign key failure. Returns the instruments by id."""
    for account_id in dict.fromkeys(posting.account_id for posting in postings):
        await get_account(session, account_id)  # raises AccountNotFoundError if missing
    instrument_ids = dict.fromkeys(
        instrument_id
        for posting in postings
        for instrument_id in (
            posting.instrument_id,
            posting.cost_instrument_id,
            posting.price_instrument_id,
        )
        if instrument_id is not None
    )
    # get_instrument raises InstrumentNotFoundError if one is missing.
    return {i: await get_instrument(session, i) for i in instrument_ids}


def compute_residuals(postings: Sequence[PostingInput]) -> dict[UUID, Decimal]:
    """Per-instrument sum of posting weights, keeping only the non-zero sums."""
    sums: defaultdict[UUID, Decimal] = defaultdict(Decimal)
    # Unbounded precision makes every product and sum exact instead of rounding at 28 digits.
    with localcontext(prec=MAX_PREC):
        for posting in postings:
            instrument_id, weight = _weight(posting)
            sums[instrument_id] += weight
    return {instrument_id: total for instrument_id, total in sums.items() if total != 0}


async def _load_transaction(session: AsyncSession, transaction_id: UUID) -> TransactionRecord:
    # Select columns, not ORM objects, so a stale identity map can't hide an update.
    t, p = models.Transaction, models.Posting
    row = (
        await session.execute(
            select(t.id, t.occurred_at, t.booking_date, t.description, t.status, t.origin).where(
                t.id == transaction_id
            )
        )
    ).one_or_none()
    if row is None:
        raise TransactionNotFoundError(str(transaction_id))
    posting_rows = await session.execute(
        select(
            p.id,
            p.position,
            p.account_id,
            p.instrument_id,
            p.quantity,
            p.cost_per_unit,
            p.cost_instrument_id,
            p.price_per_unit,
            p.price_instrument_id,
            p.is_fee,
            p.memo,
        )
        .where(p.transaction_id == transaction_id)
        .order_by(p.position)
    )
    return TransactionRecord(
        id=row.id,
        occurred_at=row.occurred_at,
        booking_date=row.booking_date,
        description=row.description,
        status=cast(TransactionStatus, row.status),
        origin=cast(TransactionOrigin, row.origin),
        postings=tuple(
            PostingRecord(
                id=r.id,
                position=r.position,
                account_id=r.account_id,
                instrument_id=r.instrument_id,
                quantity=r.quantity,
                cost_per_unit=r.cost_per_unit,
                cost_instrument_id=r.cost_instrument_id,
                price_per_unit=r.price_per_unit,
                price_instrument_id=r.price_instrument_id,
                is_fee=r.is_fee,
                memo=r.memo,
            )
            for r in posting_rows
        ),
    )


def _optional_str(value: object | None) -> str | None:
    if value is None:
        return None
    if isinstance(value, Decimal):
        return to_plain_str(value)
    return str(value)


def _change_data(record: TransactionRecord) -> dict[str, JSONValue]:
    return {
        "id": str(record.id),
        "occurred_at": record.occurred_at.isoformat(),
        "booking_date": record.booking_date.isoformat(),
        "description": record.description,
        "status": record.status,
        "origin": record.origin,
        "postings": [
            {
                "id": str(posting.id),
                "position": posting.position,
                "account_id": str(posting.account_id),
                "instrument_id": str(posting.instrument_id),
                "quantity": to_plain_str(posting.quantity),
                "cost_per_unit": _optional_str(posting.cost_per_unit),
                "cost_instrument_id": _optional_str(posting.cost_instrument_id),
                "price_per_unit": _optional_str(posting.price_per_unit),
                "price_instrument_id": _optional_str(posting.price_instrument_id),
                "is_fee": posting.is_fee,
                "memo": posting.memo,
            }
            for posting in record.postings
        ],
    }


def _publish(ctx: CommandContext, event_type: str, record: TransactionRecord) -> None:
    ctx.emit(event_type, record.id, {"transaction_id": str(record.id)})
    ctx.record_change(f"transaction:{record.id}", _change_data(record))


async def post_transaction(ctx: CommandContext, tx: TransactionInput) -> TransactionRecord:
    """Write a transaction whose postings balance per instrument, within that instrument's
    tolerance; otherwise raise LedgerImbalanceError before anything is written."""
    if tx.occurred_at.utcoffset() is None:
        raise FinanceValidationError("occurred_at needs a timezone")
    if len(tx.postings) < 2:
        raise FinanceValidationError("a transaction needs at least two postings")
    for posting in tx.postings:
        _validate_posting(posting)
    session = ctx.session
    instruments = await _resolve_references(session, tx.postings)
    residuals = compute_residuals(tx.postings)
    for instrument_id, residual in residuals.items():
        # copy_abs is exact; abs() would round the residual to the context's 28 digits.
        if residual.copy_abs() > tolerance(instruments[instrument_id].decimals):
            raise LedgerImbalanceError(residuals)
    booking_date = tx.booking_date
    if booking_date is None:
        booking_date = local_date(tx.occurred_at, await get_timezone(session))
    transaction_id = (
        await session.execute(
            insert(models.Transaction)
            .values(
                occurred_at=tx.occurred_at,
                occurred_tz=tx.occurred_tz,
                booking_date=booking_date,
                value_date=tx.value_date,
                description=tx.description,
                counterparty_entity_id=tx.counterparty_entity_id,
                status=tx.status,
                origin=tx.origin,
                owner_entity_id=await get_self_entity_id(session),
            )
            .returning(models.Transaction.id)
        )
    ).scalar_one()
    await session.execute(
        insert(models.Posting).values(
            [
                {
                    "transaction_id": transaction_id,
                    "position": position,
                    "account_id": posting.account_id,
                    "instrument_id": posting.instrument_id,
                    "quantity": posting.quantity,
                    "cost_per_unit": posting.cost_per_unit,
                    "cost_instrument_id": posting.cost_instrument_id,
                    "price_per_unit": posting.price_per_unit,
                    "price_instrument_id": posting.price_instrument_id,
                    "is_fee": posting.is_fee,
                    "memo": posting.memo,
                }
                for position, posting in enumerate(tx.postings)
            ]
        )
    )
    record = await _load_transaction(session, transaction_id)
    _publish(ctx, "finance.transaction.posted", record)
    return record


async def void_transaction(ctx: CommandContext, transaction_id: UUID) -> TransactionRecord:
    voided = await ctx.session.scalar(
        update(models.Transaction)
        .where(models.Transaction.id == transaction_id)
        .values(status="void")
        .returning(models.Transaction.id)
    )
    if voided is None:
        raise TransactionNotFoundError(str(transaction_id))
    record = await _load_transaction(ctx.session, transaction_id)
    _publish(ctx, "finance.transaction.voided", record)
    return record


async def find_unbalanced_transactions(session: AsyncSession) -> list[UUID]:
    """Ids of non-void transactions whose postings don't balance, by the same rules as
    post_transaction, recomputed in the database."""
    t, p, i = models.Transaction, models.Posting, models.Instrument
    with_cost, with_price = p.cost_per_unit.is_not(None), p.price_per_unit.is_not(None)
    weight_instrument = case(
        (with_cost, p.cost_instrument_id),
        (with_price, p.price_instrument_id),
        else_=p.instrument_id,
    )
    weight = case(
        (with_cost, p.quantity * p.cost_per_unit),
        (with_price, p.quantity * p.price_per_unit),
        else_=p.quantity,
    )
    weights = (
        select(p.transaction_id, weight_instrument.label("instrument_id"), weight.label("weight"))
        .join(t, t.id == p.transaction_id)
        .where(t.status != "void")
        .subquery()
    )
    # |sum| > 10^-decimals / 2, rewritten with a positive exponent so NUMERIC stays exact.
    # The explicit cast matters: a bare 10 is bound as an integer, and power(int, int)
    # resolves to the double precision variant, which rounds the comparison.
    ten = literal(10).cast(Numeric)
    unbalanced = func.abs(func.sum(weights.c.weight)) * func.power(ten, i.decimals) * 2 > 1
    rows = await session.scalars(
        select(weights.c.transaction_id)
        .join(i, i.id == weights.c.instrument_id)
        .group_by(weights.c.transaction_id, i.id)
        .having(unbalanced)
        .distinct()
        .order_by(weights.c.transaction_id)
    )
    return list(rows)
