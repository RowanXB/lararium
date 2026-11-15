from datetime import date, datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import DateTime, ForeignKey, Numeric, Text, func, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from lararium.core.json_types import JSONValue
from lararium.core.models import UUIDV7, Base

FINANCE_SCHEMA = {"schema": "finance"}


class Instrument(Base):
    __tablename__ = "instruments"
    __table_args__ = FINANCE_SCHEMA

    id: Mapped[UUID] = mapped_column(primary_key=True, server_default=UUIDV7)
    kind: Mapped[str] = mapped_column(Text)
    symbol: Mapped[str] = mapped_column(Text)
    name: Mapped[str] = mapped_column(Text)
    decimals: Mapped[int]
    quote_instrument_id: Mapped[UUID | None] = mapped_column(ForeignKey("finance.instruments.id"))
    hidden: Mapped[bool] = mapped_column(server_default=text("false"))
    owner_entity_id: Mapped[UUID] = mapped_column(ForeignKey("core.entities.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class InstrumentIdentifier(Base):
    __tablename__ = "instrument_identifiers"
    __table_args__ = FINANCE_SCHEMA

    instrument_id: Mapped[UUID] = mapped_column(
        ForeignKey("finance.instruments.id", ondelete="CASCADE")
    )
    scheme: Mapped[str] = mapped_column(Text, primary_key=True)
    value: Mapped[str] = mapped_column(Text, primary_key=True)


class Account(Base):
    __tablename__ = "accounts"
    __table_args__ = FINANCE_SCHEMA

    id: Mapped[UUID] = mapped_column(primary_key=True, server_default=UUIDV7)
    name: Mapped[str] = mapped_column(Text)
    ledger_type: Mapped[str] = mapped_column(Text)
    subtype: Mapped[str] = mapped_column(Text)
    institution_entity_id: Mapped[UUID | None] = mapped_column(ForeignKey("core.entities.id"))
    owner_entity_id: Mapped[UUID] = mapped_column(ForeignKey("core.entities.id"))
    tax_wrapper: Mapped[str] = mapped_column(Text, server_default=text("'none'"))
    parent_account_id: Mapped[UUID | None] = mapped_column(ForeignKey("finance.accounts.id"))
    balance_mode: Mapped[str] = mapped_column(Text)
    source_kind: Mapped[str] = mapped_column(Text, server_default=text("'manual'"))
    source_connection_id: Mapped[UUID | None]
    status: Mapped[str] = mapped_column(Text, server_default=text("'open'"))
    opened_on: Mapped[date | None]
    closed_on: Mapped[date | None]
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Transaction(Base):
    __tablename__ = "transactions"
    __table_args__ = FINANCE_SCHEMA

    id: Mapped[UUID] = mapped_column(primary_key=True, server_default=UUIDV7)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    occurred_tz: Mapped[str | None] = mapped_column(Text)
    booking_date: Mapped[date]
    value_date: Mapped[date | None]
    description: Mapped[str] = mapped_column(Text, server_default=text("''"))
    counterparty_entity_id: Mapped[UUID | None] = mapped_column(ForeignKey("core.entities.id"))
    status: Mapped[str] = mapped_column(Text)
    # Who voided it (an actor type), when it is void; NULL for voids before this was kept.
    voided_by: Mapped[str | None] = mapped_column(Text)
    origin: Mapped[str] = mapped_column(Text)
    owner_entity_id: Mapped[UUID] = mapped_column(ForeignKey("core.entities.id"))
    connection_id: Mapped[UUID | None] = mapped_column(ForeignKey("core.connections.id"))
    external_id: Mapped[str | None] = mapped_column(Text)
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class Posting(Base):
    __tablename__ = "postings"
    __table_args__ = FINANCE_SCHEMA

    id: Mapped[UUID] = mapped_column(primary_key=True, server_default=UUIDV7)
    transaction_id: Mapped[UUID] = mapped_column(
        ForeignKey("finance.transactions.id", ondelete="CASCADE")
    )
    position: Mapped[int]
    account_id: Mapped[UUID] = mapped_column(ForeignKey("finance.accounts.id"))
    instrument_id: Mapped[UUID] = mapped_column(ForeignKey("finance.instruments.id"))
    quantity: Mapped[Decimal] = mapped_column(Numeric)
    cost_per_unit: Mapped[Decimal | None] = mapped_column(Numeric)
    cost_instrument_id: Mapped[UUID | None] = mapped_column(ForeignKey("finance.instruments.id"))
    price_per_unit: Mapped[Decimal | None] = mapped_column(Numeric)
    price_instrument_id: Mapped[UUID | None] = mapped_column(ForeignKey("finance.instruments.id"))
    is_fee: Mapped[bool] = mapped_column(server_default=text("false"))
    memo: Mapped[str] = mapped_column(Text, server_default=text("''"))


class BalanceSnapshot(Base):
    __tablename__ = "balance_snapshots"
    __table_args__ = FINANCE_SCHEMA

    id: Mapped[UUID] = mapped_column(primary_key=True, server_default=UUIDV7)
    account_id: Mapped[UUID] = mapped_column(ForeignKey("finance.accounts.id"))
    instrument_id: Mapped[UUID] = mapped_column(ForeignKey("finance.instruments.id"))
    quantity: Mapped[Decimal] = mapped_column(Numeric)
    as_of: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    as_of_date: Mapped[date]
    kind: Mapped[str] = mapped_column(Text)
    owner_entity_id: Mapped[UUID] = mapped_column(ForeignKey("core.entities.id"))
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class Price(Base):
    __tablename__ = "prices"
    __table_args__ = FINANCE_SCHEMA

    id: Mapped[UUID] = mapped_column(primary_key=True, server_default=UUIDV7)
    instrument_id: Mapped[UUID] = mapped_column(ForeignKey("finance.instruments.id"))
    quote_instrument_id: Mapped[UUID] = mapped_column(ForeignKey("finance.instruments.id"))
    price: Mapped[Decimal] = mapped_column(Numeric)
    as_of_date: Mapped[date]
    source: Mapped[str] = mapped_column(Text)
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class FxRate(Base):
    __tablename__ = "fx_rates"
    __table_args__ = FINANCE_SCHEMA

    id: Mapped[UUID] = mapped_column(primary_key=True, server_default=UUIDV7)
    base_instrument_id: Mapped[UUID] = mapped_column(ForeignKey("finance.instruments.id"))
    quote_instrument_id: Mapped[UUID] = mapped_column(ForeignKey("finance.instruments.id"))
    rate: Mapped[Decimal] = mapped_column(Numeric)
    as_of_date: Mapped[date]
    source: Mapped[str] = mapped_column(Text)
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class ImportProfile(Base):
    __tablename__ = "import_profiles"
    __table_args__ = FINANCE_SCHEMA

    id: Mapped[UUID] = mapped_column(primary_key=True, server_default=UUIDV7)
    name: Mapped[str] = mapped_column(Text)
    format: Mapped[str] = mapped_column(Text)
    config: Mapped[dict[str, JSONValue]] = mapped_column(JSONB)
    version: Mapped[int] = mapped_column(server_default=text("1"))
    owner_entity_id: Mapped[UUID] = mapped_column(ForeignKey("core.entities.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ImportBatch(Base):
    __tablename__ = "import_batches"
    __table_args__ = FINANCE_SCHEMA

    id: Mapped[UUID] = mapped_column(primary_key=True, server_default=UUIDV7)
    account_id: Mapped[UUID] = mapped_column(ForeignKey("finance.accounts.id"))
    profile_id: Mapped[UUID] = mapped_column(ForeignKey("finance.import_profiles.id"))
    profile_version: Mapped[int]
    profile_config: Mapped[dict[str, JSONValue]] = mapped_column(JSONB)
    parser_version: Mapped[str] = mapped_column(Text)
    document_id: Mapped[UUID] = mapped_column(ForeignKey("core.documents.id"))
    filename: Mapped[str] = mapped_column(Text)
    default_instrument_id: Mapped[UUID | None] = mapped_column(ForeignKey("finance.instruments.id"))
    status: Mapped[str] = mapped_column(Text, server_default=text("'staged'"))
    warnings: Mapped[list[str]] = mapped_column(JSONB, server_default=text("'[]'"))
    covered_from: Mapped[date | None]
    covered_to: Mapped[date | None]
    owner_entity_id: Mapped[UUID] = mapped_column(ForeignKey("core.entities.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    committed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    rolled_back_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ImportStatement(Base):
    """What a batch's file says at statement level (Decision 18)."""

    __tablename__ = "import_statements"
    __table_args__ = FINANCE_SCHEMA

    batch_id: Mapped[UUID] = mapped_column(
        ForeignKey("finance.import_batches.id", ondelete="CASCADE"), primary_key=True
    )
    period_from: Mapped[date | None]
    period_to: Mapped[date | None]
    currency: Mapped[str | None] = mapped_column(Text)
    opening_balance: Mapped[Decimal | None] = mapped_column(Numeric)
    closing_balance: Mapped[Decimal | None] = mapped_column(Numeric)
    closing_balance_date: Mapped[date | None]
    money_in: Mapped[Decimal | None] = mapped_column(Numeric)
    money_out: Mapped[Decimal | None] = mapped_column(Numeric)
    payments_in: Mapped[int | None]
    payments_out: Mapped[int | None]
    statement_reference: Mapped[str | None] = mapped_column(Text)
    document_date: Mapped[date | None]
    account_type: Mapped[str | None] = mapped_column(Text)
    account_name: Mapped[str | None] = mapped_column(Text)
    sort_code: Mapped[str | None] = mapped_column(Text)
    account_number: Mapped[str | None] = mapped_column(Text)
    iban: Mapped[str | None] = mapped_column(Text)
    bic: Mapped[str | None] = mapped_column(Text)
    customer_number: Mapped[str | None] = mapped_column(Text)
    clearing_account_number: Mapped[str | None] = mapped_column(Text)
    branch: Mapped[str | None] = mapped_column(Text)
    overdraft_limit: Mapped[Decimal | None] = mapped_column(Numeric)
    overdraft_limit_date: Mapped[date | None]
    available_balance: Mapped[Decimal | None] = mapped_column(Numeric)
    available_balance_date: Mapped[date | None]


class ImportStatementRate(Base):
    __tablename__ = "import_statement_rates"
    __table_args__ = FINANCE_SCHEMA

    id: Mapped[UUID] = mapped_column(primary_key=True, server_default=UUIDV7)
    batch_id: Mapped[UUID] = mapped_column(
        ForeignKey("finance.import_statements.batch_id", ondelete="CASCADE")
    )
    position: Mapped[int]
    kind: Mapped[str] = mapped_column(Text)
    band_from: Mapped[Decimal | None] = mapped_column(Numeric)
    band_to: Mapped[Decimal | None] = mapped_column(Numeric)
    rate: Mapped[Decimal | None] = mapped_column(Numeric)
    ear: Mapped[Decimal | None] = mapped_column(Numeric)


class ImportRow(Base):
    __tablename__ = "import_rows"
    __table_args__ = FINANCE_SCHEMA

    id: Mapped[UUID] = mapped_column(primary_key=True, server_default=UUIDV7)
    batch_id: Mapped[UUID] = mapped_column(
        ForeignKey("finance.import_batches.id", ondelete="CASCADE")
    )
    account_id: Mapped[UUID] = mapped_column(ForeignKey("finance.accounts.id"))
    position: Mapped[int]
    kind: Mapped[str] = mapped_column(Text, server_default=text("'statement'"))
    status: Mapped[str] = mapped_column(Text)
    booking_date: Mapped[date | None]
    occurred_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    occurred_tz: Mapped[str | None] = mapped_column(Text)
    description: Mapped[str] = mapped_column(Text, server_default=text("''"))
    quantity: Mapped[Decimal | None] = mapped_column(Numeric)
    fee: Mapped[Decimal] = mapped_column(Numeric, server_default=text("0"))
    instrument_id: Mapped[UUID | None] = mapped_column(ForeignKey("finance.instruments.id"))
    balance: Mapped[Decimal | None] = mapped_column(Numeric)
    external_id: Mapped[str | None] = mapped_column(Text)
    bank_type: Mapped[str | None] = mapped_column(Text)
    value_date: Mapped[date | None]
    payee: Mapped[str | None] = mapped_column(Text)
    memo: Mapped[str | None] = mapped_column(Text)
    check_number: Mapped[str | None] = mapped_column(Text)
    counterparty_name: Mapped[str | None] = mapped_column(Text)
    counterparty_sort_code: Mapped[str | None] = mapped_column(Text)
    counterparty_account_number: Mapped[str | None] = mapped_column(Text)
    counterparty_bank: Mapped[str | None] = mapped_column(Text)
    payment_reference: Mapped[str | None] = mapped_column(Text)
    payment_kind: Mapped[str | None] = mapped_column(Text)
    bank_reference: Mapped[str | None] = mapped_column(Text)
    purpose_code: Mapped[str | None] = mapped_column(Text)
    remittance_info: Mapped[str | None] = mapped_column(Text)
    dedupe_key: Mapped[str | None] = mapped_column(Text)
    error: Mapped[str | None] = mapped_column(Text)
    note: Mapped[str | None] = mapped_column(Text)
    raw: Mapped[dict[str, str]] = mapped_column(JSONB)
    transaction_id: Mapped[UUID | None] = mapped_column(ForeignKey("finance.transactions.id"))
