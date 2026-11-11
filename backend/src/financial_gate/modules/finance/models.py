from datetime import date, datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import DateTime, ForeignKey, Numeric, Text, func, text
from sqlalchemy.orm import Mapped, mapped_column

from financial_gate.core.models import UUIDV7, Base

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
    origin: Mapped[str] = mapped_column(Text)
    owner_entity_id: Mapped[UUID] = mapped_column(ForeignKey("core.entities.id"))
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
