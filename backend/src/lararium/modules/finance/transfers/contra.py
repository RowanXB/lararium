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
    any_,
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
    the account number its import rows print alone (or its connector gives alone), else the
    main account of a manual sub-account for a bank transfer naming no bank account),
    weak_holder (apart from holder:
    for a record naming no bank account in full and no account number, the one own account
    registered under the sort code its import rows print alone), names_account (it names a
    bank account, or its import rows print part of one; a card merchant is none), holders
    (the own accounts the card merchant it names pays into, NULL for any other record: such
    evidence never identifies it, so holder stays NULL and it pairs only with a record that
    is there, pairs.card_tier),
    bank_transfer, source_kind, source_ref.
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
    # printing what the first did not). Another lateral aggregate, its rows found as
    # queries._in_ids finds them: their ids gathered first, by index (its rows by
    # import_rows_transaction, their duplicates by import_rows_duplicate_of), then each row
    # by its primary key and its batch by its own. Joined instead, rows and batches leave
    # the plan to the planner's estimates, and with wrong ones (statistics taken while a
    # bulk insert was uncommitted) it read every row of a batch for each record.
    c, d, r = (
        aliased(models.ImportRow, name=n) for n in ("own_row", "duplicate_row", "printed_row")
    )
    batch = aliased(models.ImportBatch, name="printed_batch")
    # By transaction alone, so the planner has one index to take (with the status too, it
    # may AND in an index of every committed row). A transaction that isn't void has no
    # other rows than its committed ones: a rollback voids its rows' transactions. Each is
    # correlated with the transaction explicitly: nested in the lateral, it would otherwise
    # look for it in the lateral alone, and gather every transaction's rows.
    own_rows = cast(
        select(func.array_agg(c.id))
        .where(c.transaction_id == t.id)
        .correlate_except(c)
        .scalar_subquery(),
        ARRAY(Uuid()),
    )
    duplicates = cast(
        select(func.array_agg(d.id))
        .where(d.duplicate_of.is_not(None), d.duplicate_of == any_(own_rows))
        .correlate_except(d)
        .scalar_subquery(),
        ARRAY(Uuid()),
    )
    batch_status = select(batch.status).where(batch.id == r.batch_id).scalar_subquery()
    # A duplicate speaks for its original only when it is surely that row: matched by its
    # key, or the one committed row of its account on that day with that instrument and
    # amount. Otherwise it was matched by day and amount alone (Decision 16), in file order,
    # and among alike rows it may be another's: a later file's savings transfer marked the
    # duplicate of a shop payment the same day would make the payment a transfer. (A row
    # of its own file alike to it is committed, or the duplicate of another such row.)
    o, s = (aliased(models.ImportRow, name=n) for n in ("original_row", "alike_row"))
    surely_its_original = or_(
        r.status == "committed",
        exists().where(o.id == r.duplicate_of, o.dedupe_key == r.dedupe_key),
        ~exists().where(
            s.account_id == r.account_id,
            s.status == "committed",
            s.booking_date == r.booking_date,
            s.kind == "statement",
            s.instrument_id == r.instrument_id,
            s.quantity == r.quantity,
            s.id != r.duplicate_of,
        ),
    )
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
        .where(
            r.id == any_(func.array_cat(own_rows, duplicates)),
            r.kind == "statement",
            r.status.in_(("committed", "duplicate")),
            batch_status == "committed",
            surely_its_original,
        )
        .lateral("evidence")
    )

    def registered_alone(part: int, printed: ColumnElement[str]) -> ColumnElement[UUID]:
        """The one own account registered under a UK bank account whose part (1, its sort
        code; 2, its account number) is the printed one; NULL when none is, or several."""
        n = aliased(models.AccountIdentifier, name=f"part_{part}_identifier")
        return (
            select(type_coerce(func.array_agg(distinct(n.account_id)), ARRAY(Uuid()))[1])
            .where(
                n.kind == "uk_bank_account",
                n.account_id.is_not(None),
                func.split_part(n.value, ":", part) == printed,
            )
            .having(func.count(distinct(n.account_id)) == 1)
            .scalar_subquery()
        )

    # A connector's record gives the account number alone on the transaction itself (eToro
    # Money's bank transfers, design §5.7): the same evidence as an import row's, and counted
    # with theirs.
    own_number = t.counterparty_account_number
    printed = func.coalesce(func.cardinality(evidence.c.numbers), 0)
    number_count = case(
        (own_number.is_(None), printed),
        (own_number == any_(evidence.c.numbers), printed),
        else_=printed + 1,
    )
    number_holder = registered_alone(2, func.coalesce(own_number, evidence.c.numbers[1]))
    # The record's own account, looked up per record too, not joined, for the same reason.
    a = aliased(models.Account, name="own_account")
    own_account = (
        select(a.source_kind, a.parent_account_id).where(a.id == legs.c.account_id).lateral("own")
    )
    names_nothing = and_(
        t.counterparty_identifier_id.is_(None),
        number_count == 0,
        ~func.coalesce(evidence.c.partial, false()),
    )
    holder = case(
        (
            t.counterparty_identifier_id.is_not(None),
            select(i.account_id).where(i.id == t.counterparty_identifier_id).scalar_subquery(),
        ),
        (number_count == 1, number_holder),
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
    # Weak evidence, apart from holder, which only pairs.weak_tier reads: the import rows
    # print the counterparty's sort code alone, and one own account has it.
    weak_holder = case(
        (
            and_(
                t.counterparty_identifier_id.is_(None),
                number_count == 0,
                func.cardinality(evidence.c.sort_codes) == 1,
            ),
            registered_alone(1, evidence.c.sort_codes[1]),
        ),
        else_=None,
    )
    # The card merchants, each with its holders (NULL for none), aggregated once and joined:
    # a handful of rows, where a lookup per record would run for every record naming any
    # identifier.
    m = aliased(models.AccountIdentifier, name="merchant")
    h = aliased(models.AccountIdentifierHolder, name="merchant_holder")
    merchants = (
        select(
            m.id.label("identifier_id"),
            type_coerce(
                func.array_agg(h.account_id).filter(h.account_id.is_not(None)), ARRAY(Uuid())
            ).label("holders"),
        )
        .select_from(m)
        .outerjoin(h, h.identifier_id == m.id)
        .where(m.kind == "card_merchant")
        .group_by(m.id)
        .subquery("card_merchants")
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
            weak_holder.label("weak_holder"),
            or_(
                # A card merchant is no bank account.
                and_(
                    t.counterparty_identifier_id.is_not(None), merchants.c.identifier_id.is_(None)
                ),
                own_number.is_not(None),
                func.coalesce(evidence.c.partial, false()),
            ).label("names_account"),
            merchants.c.holders,
            t.bank_transfer,
            source_kind.label("source_kind"),
            source_ref_.label("source_ref"),
        )
        .select_from(t)
        .join(legs, true())
        .join(evidence, true())
        .join(own_account, true())
        .outerjoin(merchants, merchants.c.identifier_id == t.counterparty_identifier_id)
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
    Column("weak_holder", Uuid),
    Column("names_account", Boolean, nullable=False),
    Column("holders", ARRAY(Uuid)),
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


@dataclass(frozen=True)
class LegFee:
    """A transfer leg's conversion fee, as its connector gives it (finance.transfer_leg_fees,
    eToro design §5.12): only connectors write it, and the engine reads it into the
    conversion it posts."""

    instrument_id: UUID
    amount: Decimal  # not negative


def leg_fee_columns(
    transaction_id: SQLColumnExpression[UUID],
) -> tuple[ColumnElement[UUID], ColumnElement[Decimal]]:
    """The (instrument_id, amount) of the transaction's leg fee, NULL when it has none: a
    lookup by its primary key for each record."""
    f = aliased(models.TransferLegFee, name="leg_fee")
    return (
        select(f.instrument_id).where(f.transaction_id == transaction_id).scalar_subquery(),
        select(f.amount).where(f.transaction_id == transaction_id).scalar_subquery(),
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
    transit. A card merchant (registered with holders, not to one account) never does: its
    record stays on Unassigned until it is paired (eToro design §5.12, as amended in Task
    15). `source` is None for a transaction not yet posted, which has neither. Both
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
