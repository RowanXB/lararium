"""Which transactions can be a transfer's leg, their stable reference, and which system
account their contra belongs on."""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Literal
from uuid import UUID

from sqlalchemy import (
    ColumnElement,
    DateTime,
    Numeric,
    SQLColumnExpression,
    String,
    Subquery,
    Uuid,
    and_,
    case,
    cast,
    exists,
    func,
    literal,
    select,
)
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from lararium.modules.finance import models
from lararium.modules.finance.errors import TransactionNotFoundError
from lararium.modules.finance.identifiers import identifier_account
from lararium.modules.finance.system_accounts import SystemAccountKey, system_account_ids

# Derived transactions (placeholders, settlements) are never legs themselves.
DERIVED_ORIGINS = ("inference", "rule")


@dataclass(frozen=True)
class OwnLeg:
    account_id: UUID  # the own account the record moves money in or out of
    instrument_id: UUID
    quantity: Decimal  # signed: negative on the sending side
    contra_account_id: UUID  # Unassigned or In transit


def source_columns(
    t: type[models.Transaction],
) -> tuple[ColumnElement[str], ColumnElement[str]]:
    """source_ref as SQL expressions (kind, ref) over a transactions table or alias."""
    connector = and_(t.connection_id.is_not(None), t.external_id.is_not(None))
    kind = case((connector, literal("connection")), else_=literal("transaction"))
    ref = case(
        (connector, func.concat(cast(t.connection_id, String), ":", t.external_id)),
        else_=cast(t.id, String),
    )
    return kind, ref


def eligible_records(system: Mapping[SystemAccountKey, UUID], name: str = "eligible") -> Subquery:
    """Every transaction that can be a transfer's leg (an own record), with its own posting
    and its contra: not void, not derived, and exactly two postings, one on Unassigned or
    In transit and the other on an account that is not a system account.

    Columns: transaction_id, occurred_at, booking_date, account_id, instrument_id, quantity,
    contra_account_id, counterparty_identifier_id, bank_transfer, source_kind, source_ref.
    """
    t = models.Transaction
    own, contra = aliased(models.Posting, name="own"), aliased(models.Posting, name="contra")
    contras = [system[key] for key in ("unassigned", "in_transit") if key in system]
    p = models.Posting
    posting_count = select(func.count()).where(p.transaction_id == t.id).scalar_subquery()
    source_kind, source_ref_ = source_columns(t)
    return (
        select(
            t.id.label("transaction_id"),
            t.occurred_at,
            t.booking_date,
            own.account_id,
            own.instrument_id,
            own.quantity,
            contra.account_id.label("contra_account_id"),
            t.counterparty_identifier_id,
            t.bank_transfer,
            source_kind.label("source_kind"),
            source_ref_.label("source_ref"),
        )
        .join(own, own.transaction_id == t.id)
        .join(contra, contra.transaction_id == t.id)
        .where(
            t.status != "void",
            t.origin.not_in(DERIVED_ORIGINS),
            contra.account_id.in_(contras),
            own.account_id.not_in(list(system.values())),
            posting_count == 2,
        )
        .subquery(name)
    )


def bound_id(value: UUID) -> SQLColumnExpression[UUID]:
    """An id as a bound SQL value, for the queries that also take columns."""
    return literal(value, Uuid())


@dataclass(frozen=True)
class RecordColumns:
    """An own record's facts as SQL expressions: columns of eligible_records, or bound values
    of a record already loaded, so one query serves both."""

    transaction_id: SQLColumnExpression[UUID]
    account_id: SQLColumnExpression[UUID]
    instrument_id: SQLColumnExpression[UUID]
    quantity: SQLColumnExpression[Decimal]
    occurred_at: SQLColumnExpression[datetime]
    source_kind: SQLColumnExpression[str]
    source_ref: SQLColumnExpression[str]


def record_columns(e: Subquery) -> RecordColumns:
    """The columns of an eligible_records subquery."""
    return RecordColumns(
        transaction_id=e.c.transaction_id,
        account_id=e.c.account_id,
        instrument_id=e.c.instrument_id,
        quantity=e.c.quantity,
        occurred_at=e.c.occurred_at,
        source_kind=e.c.source_kind,
        source_ref=e.c.source_ref,
    )


@dataclass(frozen=True)
class OwnRecord:
    """An own record (see eligible_records) with what matching needs."""

    id: UUID
    occurred_at: datetime
    booking_date: date
    account_id: UUID
    instrument_id: UUID
    quantity: Decimal
    contra_account_id: UUID
    counterparty_identifier_id: UUID | None
    bank_transfer: bool
    source: tuple[str, str]

    @property
    def role(self) -> Literal["out", "in"]:
        return "out" if self.quantity < 0 else "in"

    @property
    def amount(self) -> Decimal:
        return self.quantity.copy_abs()

    def columns(self) -> RecordColumns:
        return RecordColumns(
            transaction_id=bound_id(self.id),
            account_id=bound_id(self.account_id),
            instrument_id=bound_id(self.instrument_id),
            quantity=literal(self.quantity, Numeric),
            occurred_at=literal(self.occurred_at, DateTime(timezone=True)),
            source_kind=literal(self.source[0], String),
            source_ref=literal(self.source[1], String),
        )


async def load_own_record(
    session: AsyncSession, system: Mapping[SystemAccountKey, UUID], transaction_id: UUID
) -> OwnRecord | None:
    """The transaction as an own record, or None when it can't be a transfer's leg."""
    e = eligible_records(system)
    row = (
        await session.execute(select(e).where(e.c.transaction_id == transaction_id))
    ).one_or_none()
    if row is None:
        return None
    return OwnRecord(
        id=row.transaction_id,
        occurred_at=row.occurred_at,
        booking_date=row.booking_date,
        account_id=row.account_id,
        instrument_id=row.instrument_id,
        quantity=row.quantity,
        contra_account_id=row.contra_account_id,
        counterparty_identifier_id=row.counterparty_identifier_id,
        bank_transfer=row.bank_transfer,
        source=(row.source_kind, row.source_ref),
    )


async def own_leg(session: AsyncSession, transaction_id: UUID) -> OwnLeg | None:
    """The transaction's own posting and contra, or None when it can't be a transfer's leg."""
    e = eligible_records(await system_account_ids(session))
    row = (
        await session.execute(
            select(e.c.account_id, e.c.instrument_id, e.c.quantity, e.c.contra_account_id).where(
                e.c.transaction_id == transaction_id
            )
        )
    ).one_or_none()
    if row is None:
        return None
    return OwnLeg(row.account_id, row.instrument_id, row.quantity, row.contra_account_id)


async def source_ref(session: AsyncSession, transaction_id: UUID) -> tuple[str, str]:
    """The transaction's stable reference, which the owner's decisions are keyed by:
    ("connection", "<connection_id>:<external_id>") for a connector's row, else
    ("transaction", "<id>")."""
    t = models.Transaction
    row = (
        await session.execute(select(t.connection_id, t.external_id).where(t.id == transaction_id))
    ).one_or_none()
    if row is None:
        raise TransactionNotFoundError(str(transaction_id))
    if row.connection_id is not None and row.external_id is not None:
        return "connection", f"{row.connection_id}:{row.external_id}"
    return "transaction", str(transaction_id)


async def is_rejected(session: AsyncSession, source: tuple[str, str]) -> bool:
    """Whether the owner said the transaction with this reference is not a transfer."""
    d = models.TransferDecision
    return bool(
        await session.scalar(
            select(
                exists().where(
                    d.kind == "reject", d.subject_kind == source[0], d.subject_ref == source[1]
                )
            )
        )
    )


async def _transaction_of(session: AsyncSession, source: tuple[str, str]) -> UUID | None:
    kind, ref = source
    t = models.Transaction
    if kind == "connection":
        connection_id, external_id = ref.split(":", 1)
        return await session.scalar(
            select(t.id).where(t.connection_id == UUID(connection_id), t.external_id == external_id)
        )
    if kind == "transaction":
        return UUID(ref)
    return None


async def is_active_leg(session: AsyncSession, transaction_id: UUID) -> bool:
    """Whether the transaction is an own record of a transfer that is not rejected."""
    tr = models.Transfer
    return bool(
        await session.scalar(
            select(
                exists().where(
                    tr.status != "rejected",
                    (tr.out_transaction_id == transaction_id)
                    | (tr.in_transaction_id == transaction_id),
                )
            )
        )
    )


async def transfer_contra(
    session: AsyncSession,
    *,
    source: tuple[str, str] | None,
    own_account_id: UUID,
    counterparty_identifier_id: UUID | None,
) -> UUID:
    """The system account a record's contra belongs on: In transit for a transfer between
    own accounts, Unassigned otherwise (design §5.6).

    In order: the owner's reject decision for `source` wins (Unassigned); a leg of a transfer
    that is not rejected stays In transit; a counterparty bank account registered to another
    own account makes it In transit. `source` is None for a transaction not yet posted, which
    has neither. Both system accounts must exist already (get_or_create_system_account).
    """
    system = await system_account_ids(session)
    missing = [key for key in ("unassigned", "in_transit") if key not in system]
    if missing:
        raise LookupError(f"the system accounts {', '.join(missing)} have not been created yet")
    if source is not None:
        if await is_rejected(session, source):
            return system["unassigned"]
        transaction_id = await _transaction_of(session, source)
        if transaction_id is not None and await is_active_leg(session, transaction_id):
            return system["in_transit"]
    if counterparty_identifier_id is not None:
        holder = await identifier_account(session, counterparty_identifier_id)
        if holder is not None and holder != own_account_id:
            return system["in_transit"]
    return system["unassigned"]
