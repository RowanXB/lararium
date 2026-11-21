"""Which transactions can be a transfer's leg, their stable reference, and which system
account their contra belongs on."""

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Literal
from uuid import UUID

from sqlalchemy import (
    Boolean,
    Column,
    ColumnElement,
    Date,
    DateTime,
    FromClause,
    MetaData,
    Numeric,
    SQLColumnExpression,
    String,
    Subquery,
    Table,
    Uuid,
    and_,
    case,
    cast,
    distinct,
    exists,
    false,
    func,
    insert,
    literal,
    or_,
    select,
    text,
    true,
    type_coerce,
)
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased
from sqlalchemy.types import TypeEngine

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


# A transaction's stable reference, which the owner's decisions are keyed by (design §5.9):
# (kind, ref). Every kind is defined here once: when it applies to a transaction, its ref,
# and which transaction a ref names. The last kind applies to every transaction the others
# don't. An import source (M2) adds a kind before it.
type SourceRef = tuple[str, str]


@dataclass(frozen=True)
class _SourceKind:
    name: str
    applies: Callable[[type[models.Transaction]], ColumnElement[bool]]
    ref: Callable[[type[models.Transaction]], ColumnElement[str]]
    names: Callable[[type[models.Transaction], str], ColumnElement[bool]]


def _connection_names(t: type[models.Transaction], ref: str) -> ColumnElement[bool]:
    connection_id, external_id = ref.split(":", 1)
    return and_(t.connection_id == UUID(connection_id), t.external_id == external_id)


def _imported(t: type[models.Transaction]) -> ColumnElement[bool]:
    r = models.ImportRow
    return exists().where(r.transaction_id == t.id, r.kind == "statement")


def _import_key(t: type[models.Transaction]) -> ColumnElement[str]:
    r = models.ImportRow
    return (
        select(r.dedupe_key)
        .where(r.transaction_id == t.id, r.kind == "statement")
        .limit(1)
        .scalar_subquery()
    )


def _import_names(t: type[models.Transaction], ref: str) -> ColumnElement[bool]:
    # The live one: a rolled-back row keeps its voided transaction, and the same row
    # imported again (the same key) posts a new one.
    r = models.ImportRow
    return t.id.in_(select(r.transaction_id).where(r.dedupe_key == ref, r.status == "committed"))


_SOURCE_KINDS = (
    _SourceKind(
        name="connection",
        applies=lambda t: and_(t.connection_id.is_not(None), t.external_id.is_not(None)),
        ref=lambda t: func.concat(cast(t.connection_id, String), ":", t.external_id),
        names=_connection_names,
    ),
    # A statement import's row (M2): its dedupe key, which the same row keeps when imported
    # again after a rollback, so the owner's decisions follow it.
    _SourceKind(name="import", applies=_imported, ref=_import_key, names=_import_names),
    _SourceKind(
        name="transaction",
        applies=lambda t: true(),
        ref=lambda t: cast(t.id, String),
        names=lambda t, ref: t.id == UUID(ref),
    ),
)


def source_columns(
    t: type[models.Transaction],
) -> tuple[ColumnElement[str], ColumnElement[str]]:
    """The transaction's source reference as SQL expressions (kind, ref), over a
    transactions table or alias."""
    *specific, otherwise = _SOURCE_KINDS
    kind = case(*((k.applies(t), literal(k.name)) for k in specific), else_=literal(otherwise.name))
    ref = case(*((k.applies(t), k.ref(t)) for k in specific), else_=otherwise.ref(t))
    return kind, ref


def names_source(t: type[models.Transaction], source: SourceRef) -> ColumnElement[bool]:
    """Whether the transaction is the one the reference names (looked up by index), false
    for a kind no transaction has."""
    for k in _SOURCE_KINDS:
        if k.name == source[0]:
            return and_(k.names(t, source[1]), source_columns(t)[0] == k.name)
    return false()


# A transfer's leg is exactly two postings, its own posting and its contra: one with others
# besides (a fee, say) is no leg, so its contra stays on Unassigned. eligible_records and
# transfer_contra both read this.
LEG_POSTINGS = 2


def eligible_records(system: Mapping[SystemAccountKey, UUID], name: str = "eligible") -> Subquery:
    """Every transaction that can be a transfer's leg (an own record), with its own posting
    and its contra: not void, not derived, and exactly two postings, one on Unassigned or
    In transit and the other on an account that is not a system account.

    Columns: transaction_id, occurred_at, booking_date, account_id, instrument_id, quantity,
    contra_account_id, counterparty_identifier_id, holder (the own account the record names:
    the one its counterparty bank account is registered to, else the one registered under
    the account number its import rows print alone, else the main account of a manual
    sub-account for a bank transfer naming no bank account), names_account (it names a bank
    account, or its import rows print part of one), bank_transfer, source_kind, source_ref.
    """
    t, i = models.Transaction, models.AccountIdentifier
    p = aliased(models.Posting, name="leg_posting")
    contras = [system[key] for key in ("unassigned", "in_transit") if key in system]
    is_own = p.account_id.not_in(list(system.values()))
    is_contra = p.account_id.in_(contras)

    def only(
        column: SQLColumnExpression[Any], item: TypeEngine[Any], where: ColumnElement[bool]
    ) -> ColumnElement[Any]:
        """The column (of SQL type `item`) of the one posting that `where` holds for."""
        return type_coerce(func.array_agg(column).filter(where), ARRAY(item))[1]

    # The transaction's postings, summarised by a lateral aggregate: for each transaction
    # an index lookup of its own postings, whatever the planner estimates. A self-join of
    # postings, which this replaces, turns quadratic when the estimates are wrong: with
    # statistics taken while a bulk insert was uncommitted, the tables look empty.
    legs = (
        select(
            only(p.account_id, Uuid(), is_own).label("account_id"),
            only(p.instrument_id, Uuid(), is_own).label("instrument_id"),
            only(p.quantity, Numeric(), is_own).label("quantity"),
            only(p.account_id, Uuid(), is_contra).label("contra_account_id"),
        )
        .where(p.transaction_id == t.id)
        .having(
            and_(
                func.count() == LEG_POSTINGS,
                func.count().filter(is_own) == 1,
                func.count().filter(is_contra) == 1,
            )
        )
        .lateral("legs")
    )
    # What the transaction's import rows print of their counterparty's bank account: its
    # committed row, and the rows of committed batches that duplicate it (a later file
    # printing what the first did not). Another lateral aggregate, one index lookup per
    # record (import_rows_transaction, import_rows_duplicate_of).
    c, r = (aliased(models.ImportRow, name=n) for n in ("committed_row", "printed_row"))
    batch = aliased(models.ImportBatch, name="printed_batch")
    # The column holds digits only (a CHECK), so the replace below only guards.
    only_number = and_(
        r.counterparty_account_number.is_not(None), r.counterparty_sort_code.is_(None)
    )
    only_sort_code = and_(
        r.counterparty_sort_code.is_not(None), r.counterparty_account_number.is_(None)
    )
    evidence = (
        select(
            type_coerce(
                func.array_agg(
                    distinct(func.regexp_replace(r.counterparty_account_number, r"\D", "", "g"))
                ).filter(only_number),
                ARRAY(String()),
            ).label("numbers"),
            type_coerce(
                func.array_agg(
                    distinct(func.regexp_replace(r.counterparty_sort_code, r"\D", "", "g"))
                ).filter(only_sort_code),
                ARRAY(String()),
            ).label("sort_codes"),
            func.bool_or(
                or_(
                    r.counterparty_sort_code.is_not(None),
                    r.counterparty_account_number.is_not(None),
                    r.counterparty_account_ending.is_not(None),
                )
            ).label("partial"),
        )
        .select_from(c)
        .join(r, or_(r.id == c.id, r.duplicate_of == c.id))
        .join(batch, batch.id == r.batch_id)
        .where(
            c.transaction_id == t.id,
            c.status == "committed",
            c.kind == "statement",
            r.kind == "statement",
            batch.status == "committed",
        )
        .lateral("evidence")
    )
    # The one own account registered under an account number alone.
    n = aliased(models.AccountIdentifier, name="number_identifier")
    number_holder = (
        select(type_coerce(func.array_agg(distinct(n.account_id)), ARRAY(Uuid()))[1])
        .where(
            n.kind == "uk_bank_account",
            n.account_id.is_not(None),
            func.split_part(n.value, ":", 2) == evidence.c.numbers[1],
        )
        .having(func.count(distinct(n.account_id)) == 1)
        .scalar_subquery()
    )
    # The record's own account, looked up per record too, not joined, for the same reason.
    a = aliased(models.Account, name="own_account")
    own_account = (
        select(a.source_kind, a.parent_account_id).where(a.id == legs.c.account_id).lateral("own")
    )
    names_nothing = and_(
        t.counterparty_identifier_id.is_(None),
        func.coalesce(func.cardinality(evidence.c.numbers), 0) == 0,
        ~func.coalesce(evidence.c.partial, false()),
    )
    holder = case(
        (
            t.counterparty_identifier_id.is_not(None),
            select(i.account_id).where(i.id == t.counterparty_identifier_id).scalar_subquery(),
        ),
        (func.cardinality(evidence.c.numbers) == 1, number_holder),
        # A savings sub-account moves money only to and from its main account.
        (
            and_(
                names_nothing,
                t.bank_transfer,
                own_account.c.source_kind == "manual",
                own_account.c.parent_account_id.is_not(None),
            ),
            own_account.c.parent_account_id,
        ),
        else_=None,
    )
    source_kind, source_ref_ = source_columns(t)
    return (
        select(
            t.id.label("transaction_id"),
            t.occurred_at,
            t.booking_date,
            legs.c.account_id,
            legs.c.instrument_id,
            legs.c.quantity,
            legs.c.contra_account_id,
            t.counterparty_identifier_id,
            holder.label("holder"),
            or_(
                t.counterparty_identifier_id.is_not(None),
                func.coalesce(evidence.c.partial, false()),
            ).label("names_account"),
            t.bank_transfer,
            source_kind.label("source_kind"),
            source_ref_.label("source_ref"),
        )
        .join(legs, true())
        .join(evidence, true())
        .join(own_account, true())
        .where(t.status != "void", t.origin.not_in(DERIVED_ORIGINS))
        .subquery(name)
    )


# eligible_records copied for one statement that reads it several times (heal), with a
# primary key and statistics of its own, so the planner knows its size whatever it knows of
# the ledger's tables. Temporary: each connection has its own, gone at the end of the
# transaction.
_OWN_RECORDS = Table(
    "transfer_own_records",
    MetaData(),
    Column("transaction_id", Uuid, primary_key=True),
    Column("occurred_at", DateTime(timezone=True), nullable=False),
    Column("booking_date", Date, nullable=False),
    Column("account_id", Uuid, nullable=False),
    Column("instrument_id", Uuid, nullable=False),
    Column("quantity", Numeric, nullable=False),
    Column("contra_account_id", Uuid, nullable=False),
    Column("counterparty_identifier_id", Uuid),
    Column("holder", Uuid),
    Column("names_account", Boolean, nullable=False),
    Column("bank_transfer", Boolean, nullable=False),
    Column("source_kind", String, nullable=False),
    Column("source_ref", String, nullable=False),
    prefixes=["TEMPORARY"],
    postgresql_on_commit="DROP",
)


async def snapshot_own_records(
    session: AsyncSession, system: Mapping[SystemAccountKey, UUID]
) -> Table:
    """eligible_records as the ledger stands now, as a temporary table with the same
    columns, replacing the previous snapshot. Matching changes contras, so a snapshot is
    stale once anything is matched."""
    await session.execute(text(f"DROP TABLE IF EXISTS pg_temp.{_OWN_RECORDS.name}"))
    await session.run_sync(lambda sync: _OWN_RECORDS.create(sync.connection()))
    e = eligible_records(system)
    columns = [c.name for c in _OWN_RECORDS.c]
    await session.execute(
        insert(_OWN_RECORDS).from_select(columns, select(*(e.c[name] for name in columns)))
    )
    # The session's role owns its temporary tables, so it may analyze them (not the
    # ledger's).
    await session.execute(text(f"ANALYZE pg_temp.{_OWN_RECORDS.name}"))
    return _OWN_RECORDS


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
    booking_date: SQLColumnExpression[date]
    source_kind: SQLColumnExpression[str]
    source_ref: SQLColumnExpression[str]


def record_columns(e: FromClause) -> RecordColumns:
    """The columns of an eligible_records subquery or snapshot."""
    return RecordColumns(
        transaction_id=e.c.transaction_id,
        account_id=e.c.account_id,
        instrument_id=e.c.instrument_id,
        quantity=e.c.quantity,
        occurred_at=e.c.occurred_at,
        booking_date=e.c.booking_date,
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
    holder: UUID | None = None  # the own account it names (see eligible_records)

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
            booking_date=literal(self.booking_date, Date),
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
        holder=row.holder,
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


async def source_ref(session: AsyncSession, transaction_id: UUID) -> SourceRef:
    """The transaction's stable reference (source_columns): ("connection",
    "<connection_id>:<external_id>") for a connector's row, ("import", "<dedupe key>") for
    a statement import's row, else ("transaction", "<id>")."""
    t = models.Transaction
    kind, ref = source_columns(t)
    row = (await session.execute(select(kind, ref).where(t.id == transaction_id))).one_or_none()
    if row is None:
        raise TransactionNotFoundError(str(transaction_id))
    return row[0], row[1]


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


async def rejection_of(session: AsyncSession, source: tuple[str, str]) -> UUID | None:
    """The rejected transfer the owner's reject decision for this reference names, if
    there is one."""
    d = models.TransferDecision
    return await session.scalar(
        select(d.transfer_id).where(
            d.kind == "reject", d.subject_kind == source[0], d.subject_ref == source[1]
        )
    )


def rejoins(
    record_id: SQLColumnExpression[UUID],
    quantity: SQLColumnExpression[Decimal],
    account_id: SQLColumnExpression[UUID],
) -> ColumnElement[bool]:
    """Whether a rejected record goes back into the rejected transfer (models.Transfer)
    its reject decision names: it is no own record of it yet, and the side for its role
    is free and on its account. Matching (engine._rejoin) and heal read this one rule."""
    tr = models.Transfer
    return and_(
        tr.status == "rejected",
        tr.out_transaction_id.is_distinct_from(record_id),
        tr.in_transaction_id.is_distinct_from(record_id),
        or_(
            and_(quantity < 0, tr.out_transaction_id.is_(None), tr.from_account_id == account_id),
            and_(quantity > 0, tr.in_transaction_id.is_(None), tr.to_account_id == account_id),
        ),
    )


async def _transaction_of(session: AsyncSession, source: SourceRef) -> UUID | None:
    t = models.Transaction
    return await session.scalar(select(t.id).where(names_source(t, source)))


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
    other_postings: int,
) -> UUID:
    """The system account a record's contra belongs on: In transit for a transfer between
    own accounts, Unassigned otherwise (design §5.6).

    In order: the owner's reject decision for `source` wins (Unassigned); a transaction
    with `other_postings` (postings besides its own and the contra, a fee say) can't be a
    leg (LEG_POSTINGS), so Unassigned; a leg of a transfer that is not rejected stays In
    transit; a counterparty bank account registered to another own account makes it In
    transit. `source` is None for a transaction not yet posted, which has neither. Both
    system accounts must exist already (get_or_create_system_account).
    """
    system = await system_account_ids(session)
    missing = [key for key in ("unassigned", "in_transit") if key not in system]
    if missing:
        raise LookupError(f"the system accounts {', '.join(missing)} have not been created yet")
    if source is not None and await is_rejected(session, source):
        return system["unassigned"]
    if 2 + other_postings != LEG_POSTINGS:  # its own posting and the contra, and the others
        return system["unassigned"]
    if source is not None:
        transaction_id = await _transaction_of(session, source)
        if transaction_id is not None and await is_active_leg(session, transaction_id):
            return system["in_transit"]
    if counterparty_identifier_id is not None:
        holder = await identifier_account(session, counterparty_identifier_id)
        if holder is not None and holder != own_account_id:
            return system["in_transit"]
    return system["unassigned"]
