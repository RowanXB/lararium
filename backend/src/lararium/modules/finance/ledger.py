from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import MAX_PREC, Decimal, localcontext
from typing import Literal, cast
from uuid import UUID

from sqlalchemy import (
    Numeric,
    case,
    delete,
    func,
    insert,
    literal,
    or_,
    select,
    true,
    update,
)
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
from lararium.modules.finance.money import require_amount, to_plain_str, tolerance

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
class CounterpartyFields:
    """What a transaction's source says about the other side."""

    identifier_id: UUID | None = None  # its bank account, a finance.account_identifiers row
    name: str | None = None
    reference: str | None = None  # the payment reference
    bank_transfer: bool = False  # the source marks it a payment between bank accounts


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
    connection_id: UUID | None = None
    external_id: str | None = None
    counterparty: CounterpartyFields = CounterpartyFields()


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
    counterparty: CounterpartyFields
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
            require_amount(name, amount)
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
            select(
                t.id,
                t.occurred_at,
                t.booking_date,
                t.description,
                t.status,
                t.origin,
                t.counterparty_identifier_id,
                t.counterparty_name,
                t.payment_reference,
                t.bank_transfer,
            ).where(t.id == transaction_id)
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
        counterparty=CounterpartyFields(
            identifier_id=row.counterparty_identifier_id,
            name=row.counterparty_name,
            reference=row.payment_reference,
            bank_transfer=row.bank_transfer,
        ),
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
        "counterparty": {
            "identifier_id": _optional_str(record.counterparty.identifier_id),
            "name": record.counterparty.name,
            "reference": record.counterparty.reference,
            "bank_transfer": record.counterparty.bank_transfer,
        },
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


def _counterparty_values(fields: CounterpartyFields) -> dict[str, object]:
    return {
        "counterparty_identifier_id": fields.identifier_id,
        "counterparty_name": fields.name,
        "payment_reference": fields.reference,
        "bank_transfer": fields.bank_transfer,
    }


def _publish(ctx: CommandContext, event_type: str, record: TransactionRecord) -> None:
    ctx.emit(event_type, record.id, {"transaction_id": str(record.id)})
    ctx.record_change(f"transaction:{record.id}", _change_data(record))


async def _validate_transaction(session: AsyncSession, tx: TransactionInput) -> date:
    """Everything post_transaction checks before writing; returns the booking date."""
    if tx.occurred_at.utcoffset() is None:
        raise FinanceValidationError("occurred_at needs a timezone")
    if len(tx.postings) < 2:
        raise FinanceValidationError("a transaction needs at least two postings")
    for posting in tx.postings:
        _validate_posting(posting)
    instruments = await _resolve_references(session, tx.postings)
    residuals = compute_residuals(tx.postings)
    for instrument_id, residual in residuals.items():
        # copy_abs is exact; abs() would round the residual to the context's 28 digits.
        if residual.copy_abs() > tolerance(instruments[instrument_id].decimals):
            raise LedgerImbalanceError(residuals)
    if tx.booking_date is not None:
        return tx.booking_date
    return local_date(tx.occurred_at, await get_timezone(session))


async def _insert_postings(
    session: AsyncSession, transaction_id: UUID, postings: Sequence[PostingInput]
) -> None:
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
                for position, posting in enumerate(postings)
            ]
        )
    )


async def post_transaction(ctx: CommandContext, tx: TransactionInput) -> TransactionRecord:
    """Write a transaction whose postings balance per instrument, within that instrument's
    tolerance; otherwise raise LedgerImbalanceError before anything is written."""
    session = ctx.session
    booking_date = await _validate_transaction(session, tx)
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
                connection_id=tx.connection_id,
                external_id=tx.external_id,
                owner_entity_id=await get_self_entity_id(session),
                **_counterparty_values(tx.counterparty),
            )
            .returning(models.Transaction.id)
        )
    ).scalar_one()
    await _insert_postings(session, transaction_id, tx.postings)
    record = await _load_transaction(session, transaction_id)
    _publish(ctx, "finance.transaction.posted", record)
    return record


def _posting_key(
    posting: PostingInput | PostingRecord,
) -> tuple[object, ...]:
    return (
        posting.account_id,
        posting.instrument_id,
        posting.quantity,
        posting.cost_per_unit,
        posting.cost_instrument_id,
        posting.price_per_unit,
        posting.price_instrument_id,
        posting.is_fee,
        posting.memo,
    )


async def upsert_connector_transaction(
    ctx: CommandContext, tx: TransactionInput
) -> tuple[TransactionRecord, Literal["created", "updated", "unchanged"]]:
    """Post a connector's transaction, or replace the fields and postings of the one it
    already posted under the same (connection_id, external_id), keeping its id. An
    identical re-sync writes nothing."""
    if tx.origin != "connector" or tx.connection_id is None or tx.external_id is None:
        raise FinanceValidationError(
            "a connector transaction needs origin 'connector', a connection_id and an external_id"
        )
    session = ctx.session
    t = models.Transaction
    existing = (
        await session.execute(
            select(
                t.id,
                t.origin,
                t.occurred_at,
                t.occurred_tz,
                t.booking_date,
                t.value_date,
                t.description,
                t.counterparty_entity_id,
                t.status,
                t.voided_by,
                t.counterparty_identifier_id,
                t.counterparty_name,
                t.payment_reference,
                t.bank_transfer,
            ).where(t.connection_id == tx.connection_id, t.external_id == tx.external_id)
        )
    ).one_or_none()
    if existing is None:
        return await post_transaction(ctx, tx), "created"
    if existing.origin != "connector":
        raise FinanceValidationError(
            f"transaction {existing.id} was not created by a connector; refusing to replace it"
        )
    before = await _load_transaction(session, existing.id)
    if existing.status == "void" and tx.status != "void" and existing.voided_by != "connector":
        # The owner's void is a decision that survives re-syncs. A connector's own void (of a
        # reversed transaction, say) gives way to what the upstream reports now.
        return before, "unchanged"
    booking_date = await _validate_transaction(session, tx)
    fields_unchanged = (
        existing.occurred_at == tx.occurred_at
        and existing.occurred_tz == tx.occurred_tz
        and existing.booking_date == booking_date
        and existing.value_date == tx.value_date
        and existing.description == tx.description
        and existing.counterparty_entity_id == tx.counterparty_entity_id
        and existing.status == tx.status
        and existing.counterparty_identifier_id == tx.counterparty.identifier_id
        and existing.counterparty_name == tx.counterparty.name
        and existing.payment_reference == tx.counterparty.reference
        and existing.bank_transfer == tx.counterparty.bank_transfer
    )
    # Postings compare by position: the connector emits them in a stable order.
    if fields_unchanged and [_posting_key(p) for p in before.postings] == [
        _posting_key(p) for p in tx.postings
    ]:
        return before, "unchanged"
    # The decision above was made on an unlocked read: the UPDATE checks it again, so a void
    # the owner commits meanwhile isn't overwritten (no row is updated, and nothing changes).
    replaceable = or_(t.status != "void", t.voided_by == "connector")
    updated = await session.scalar(
        update(t)
        .where(t.id == existing.id, replaceable if tx.status != "void" else true())
        .values(
            occurred_at=tx.occurred_at,
            occurred_tz=tx.occurred_tz,
            booking_date=booking_date,
            value_date=tx.value_date,
            description=tx.description,
            counterparty_entity_id=tx.counterparty_entity_id,
            status=tx.status,
            voided_by=ctx.actor.type if tx.status == "void" else None,
            **_counterparty_values(tx.counterparty),
        )
        .returning(t.id)
    )
    if updated is None:
        return await _load_transaction(session, existing.id), "unchanged"
    await session.execute(
        delete(models.Posting).where(models.Posting.transaction_id == existing.id)
    )
    await _insert_postings(session, existing.id, tx.postings)
    record = await _load_transaction(session, existing.id)
    _publish_update(ctx, before, record)
    return record, "updated"


def _publish_update(
    ctx: CommandContext, before: TransactionRecord, after: TransactionRecord
) -> None:
    ctx.emit("finance.transaction.updated", after.id, {"transaction_id": str(after.id)})
    ctx.record_change(
        f"transaction:{after.id}", {"before": _change_data(before), "after": _change_data(after)}
    )


async def set_counterparty_fields(
    ctx: CommandContext, transaction_id: UUID, fields: CounterpartyFields
) -> bool:
    """Set what the transaction's source says about the other side, writing only when it
    differs from what is stored. Returns whether it wrote."""
    before = await _load_transaction(ctx.session, transaction_id)
    if before.counterparty == fields:
        return False
    await ctx.session.execute(
        update(models.Transaction)
        .where(models.Transaction.id == transaction_id)
        .values(**_counterparty_values(fields))
    )
    _publish_update(ctx, before, await _load_transaction(ctx.session, transaction_id))
    return True


async def repoint_contra(
    ctx: CommandContext, transaction_id: UUID, from_account_id: UUID, to_account_id: UUID
) -> bool:
    """Move the transaction's single posting on from_account_id to to_account_id, keeping
    its quantity, so the transaction still balances. Returns False when it has no posting
    there (or a concurrent upsert replaced the postings meanwhile); several there are
    ambiguous and refused."""
    if from_account_id == to_account_id:
        raise FinanceValidationError("a contra is re-pointed to a different account")
    session = ctx.session
    before = await _load_transaction(session, transaction_id)
    await get_account(session, to_account_id)  # raises AccountNotFoundError if missing
    on_from = [p for p in before.postings if p.account_id == from_account_id]
    if not on_from:
        return False
    if len(on_from) > 1:
        raise FinanceValidationError(
            f"transaction {transaction_id} has {len(on_from)} postings on account"
            f" {from_account_id}; which one to re-point is ambiguous"
        )
    # Conditional on the posting still being there: a concurrent upsert that replaced the
    # postings since they were read leaves nothing to update.
    moved = await session.scalar(
        update(models.Posting)
        .where(models.Posting.id == on_from[0].id, models.Posting.account_id == from_account_id)
        .values(account_id=to_account_id)
        .returning(models.Posting.id)
    )
    if moved is None:
        return False
    _publish_update(ctx, before, await _load_transaction(session, transaction_id))
    return True


_DERIVED_ORIGINS: tuple[TransactionOrigin, ...] = ("inference", "rule")


async def delete_derived_transaction(ctx: CommandContext, transaction_id: UUID) -> None:
    """Delete a transaction the ledger derived (a transfer's placeholder or settlement), so
    replacing or dissolving one leaves no void rows behind. A recorded transaction (from a
    connector, an import or the owner) is never deleted: it is voided instead."""
    session = ctx.session
    record = await _load_transaction(session, transaction_id)
    if record.origin not in _DERIVED_ORIGINS:
        raise FinanceValidationError(
            f"only derived transactions can be deleted; {transaction_id} has origin"
            f" {record.origin!r}"
        )
    tr = models.Transfer
    transfer_id = await session.scalar(
        select(tr.id).where(
            or_(
                tr.out_transaction_id == transaction_id,
                tr.in_transaction_id == transaction_id,
                tr.placeholder_transaction_id == transaction_id,
                tr.settlement_transaction_id == transaction_id,
            )
        )
    )
    if transfer_id is not None:
        raise FinanceValidationError(
            f"transaction {transaction_id} is still part of transfer {transfer_id}"
        )
    await session.execute(delete(models.Transaction).where(models.Transaction.id == transaction_id))
    ctx.emit("finance.transaction.deleted", record.id, {"transaction_id": str(record.id)})
    ctx.record_change(f"transaction:{record.id}", {"before": _change_data(record), "after": None})


async def void_transaction(ctx: CommandContext, transaction_id: UUID) -> TransactionRecord:
    """Void the transaction, recording who did. One already void is left as it is: the first
    void stands, so a connector acting on an older reading can't make the owner's void its
    own (and so let a later sync undo it).

    A transaction the ledger derived (a transfer's placeholder or settlement) is refused: it
    follows its transfer, which deletes it when replaced or dissolved."""
    t = models.Transaction
    origin = await ctx.session.scalar(select(t.origin).where(t.id == transaction_id))
    if origin is None:
        raise TransactionNotFoundError(str(transaction_id))
    if origin in _DERIVED_ORIGINS:
        raise FinanceValidationError(
            f"transaction {transaction_id} was derived by the ledger for a transfer, so it"
            " can't be voided; it goes when its transfer is rejected or its record voided"
        )
    voided = await ctx.session.scalar(
        update(t)
        .where(t.id == transaction_id, t.status != "void")
        .values(status="void", voided_by=ctx.actor.type)
        .returning(t.id)
    )
    if voided is None:
        return await _load_transaction(ctx.session, transaction_id)
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
