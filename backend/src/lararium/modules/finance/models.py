from datetime import date, datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import DateTime, ForeignKey, Numeric, Text, func, text
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import Mapped, mapped_column

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
    product_type: Mapped[str | None] = mapped_column(Text)
    owner_type: Mapped[str | None] = mapped_column(Text)
    goal_amount: Mapped[Decimal | None] = mapped_column(Numeric)
    locked: Mapped[bool | None]
    round_up: Mapped[bool | None]
    round_up_multiplier: Mapped[Decimal | None] = mapped_column(Numeric)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class AccountIdentifier(Base):
    __tablename__ = "account_identifiers"
    __table_args__ = FINANCE_SCHEMA

    id: Mapped[UUID] = mapped_column(primary_key=True, server_default=UUIDV7)
    kind: Mapped[str] = mapped_column(Text)
    value: Mapped[str] = mapped_column(Text)
    # One of the owner's accounts; NULL for a counterparty's (or not yet known) bank account.
    account_id: Mapped[UUID | None] = mapped_column(ForeignKey("finance.accounts.id"))
    # When the owner said it is not theirs; only on an unassigned row.
    dismissed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # The actor type that registered it (the owner's "user", or a "connector").
    created_by: Mapped[str] = mapped_column(Text)
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
    # What the source says about the other side: its bank account (a registry row), name and
    # payment reference, and whether it marks this as a payment between bank accounts.
    counterparty_identifier_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("finance.account_identifiers.id")
    )
    counterparty_name: Mapped[str | None] = mapped_column(Text)
    payment_reference: Mapped[str | None] = mapped_column(Text)
    bank_transfer: Mapped[bool] = mapped_column(server_default=text("false"))
    # The amount in the source's own currency, signed like the account's posting; both or neither.
    original_quantity: Mapped[Decimal | None] = mapped_column(Numeric)
    original_instrument_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("finance.instruments.id")
    )
    category: Mapped[str | None] = mapped_column(Text)
    labels: Mapped[list[str]] = mapped_column(ARRAY(Text), server_default=text("'{}'"))
    include_in_spending: Mapped[bool | None]
    payment_scheme: Mapped[str | None] = mapped_column(Text)
    place_entity_id: Mapped[UUID | None] = mapped_column(ForeignKey("core.entities.id"))
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class TransactionCategorySplit(Base):
    __tablename__ = "transaction_category_splits"
    __table_args__ = FINANCE_SCHEMA

    transaction_id: Mapped[UUID] = mapped_column(
        ForeignKey("finance.transactions.id", ondelete="CASCADE"), primary_key=True
    )
    position: Mapped[int] = mapped_column(primary_key=True)
    category: Mapped[str] = mapped_column(Text)
    quantity: Mapped[Decimal] = mapped_column(Numeric)


class DeclinedTransaction(Base):
    """A card payment the source declined: recorded for reference, outside the ledger."""

    __tablename__ = "declined_transactions"
    __table_args__ = FINANCE_SCHEMA

    id: Mapped[UUID] = mapped_column(primary_key=True, server_default=UUIDV7)
    connection_id: Mapped[UUID] = mapped_column(ForeignKey("core.connections.id"))
    external_id: Mapped[str] = mapped_column(Text)
    account_id: Mapped[UUID] = mapped_column(ForeignKey("finance.accounts.id"))
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    quantity: Mapped[Decimal] = mapped_column(Numeric)
    instrument_id: Mapped[UUID] = mapped_column(ForeignKey("finance.instruments.id"))
    description: Mapped[str] = mapped_column(Text, server_default=text("''"))
    counterparty_entity_id: Mapped[UUID | None] = mapped_column(ForeignKey("core.entities.id"))
    place_entity_id: Mapped[UUID | None] = mapped_column(ForeignKey("core.entities.id"))
    decline_reason: Mapped[str] = mapped_column(Text)
    owner_entity_id: Mapped[UUID] = mapped_column(ForeignKey("core.entities.id"))
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


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


class Transfer(Base):
    __tablename__ = "transfers"
    __table_args__ = FINANCE_SCHEMA

    id: Mapped[UUID] = mapped_column(primary_key=True, server_default=UUIDV7)
    from_account_id: Mapped[UUID] = mapped_column(ForeignKey("finance.accounts.id"))
    to_account_id: Mapped[UUID] = mapped_column(ForeignKey("finance.accounts.id"))
    instrument_id: Mapped[UUID] = mapped_column(ForeignKey("finance.instruments.id"))
    amount: Mapped[Decimal] = mapped_column(Numeric)  # positive, as sent
    status: Mapped[str] = mapped_column(Text)
    # Why the rule could not decide ("needs review"); NULL when it could.
    review_reason: Mapped[str | None] = mapped_column(Text)
    # The legs; no cascade, so a linked transaction can't be deleted until it is unlinked.
    out_transaction_id: Mapped[UUID | None] = mapped_column(ForeignKey("finance.transactions.id"))
    in_transaction_id: Mapped[UUID | None] = mapped_column(ForeignKey("finance.transactions.id"))
    placeholder_transaction_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("finance.transactions.id")
    )
    settlement_transaction_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("finance.transactions.id")
    )
    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    matched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    notified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class TransferDecision(Base):
    __tablename__ = "transfer_decisions"
    __table_args__ = FINANCE_SCHEMA

    id: Mapped[UUID] = mapped_column(primary_key=True, server_default=UUIDV7)
    kind: Mapped[str] = mapped_column(Text)  # reject | never_pair | pair
    # The source reference of the transaction it concerns (see transfers.contra.source_ref).
    subject_kind: Mapped[str] = mapped_column(Text)
    subject_ref: Mapped[str] = mapped_column(Text)
    # The other transaction, for never_pair and pair; NULL for reject.
    other_kind: Mapped[str | None] = mapped_column(Text)
    other_ref: Mapped[str | None] = mapped_column(Text)
    # The rejected transfer a reject was made on; NULL for the other kinds.
    transfer_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("finance.transfers.id", ondelete="CASCADE")
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
