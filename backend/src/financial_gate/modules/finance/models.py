from datetime import datetime
from uuid import UUID

from sqlalchemy import DateTime, ForeignKey, Text, func, text
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
