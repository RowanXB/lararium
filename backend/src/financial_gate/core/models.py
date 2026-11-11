from datetime import datetime
from uuid import UUID

from sqlalchemy import DateTime, ForeignKey, Text, func, text
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from financial_gate.core.json_types import JSONValue

UUIDV7 = text("uuidv7()")
CORE_SCHEMA = {"schema": "core"}


class Base(DeclarativeBase):
    pass


class Setting(Base):
    __tablename__ = "settings"
    __table_args__ = CORE_SCHEMA

    key: Mapped[str] = mapped_column(Text, primary_key=True)
    value: Mapped[JSONValue] = mapped_column(JSONB, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Entity(Base):
    __tablename__ = "entities"
    __table_args__ = CORE_SCHEMA

    id: Mapped[UUID] = mapped_column(primary_key=True, server_default=UUIDV7)
    kind: Mapped[str] = mapped_column(Text)
    name: Mapped[str] = mapped_column(Text)
    aliases: Mapped[list[str]] = mapped_column(ARRAY(Text), server_default=text("'{}'"))
    external_ids: Mapped[dict[str, JSONValue]] = mapped_column(JSONB, server_default=text("'{}'"))
    is_self: Mapped[bool] = mapped_column(server_default=text("false"))
    owner_entity_id: Mapped[UUID | None] = mapped_column(ForeignKey("core.entities.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Event(Base):
    __tablename__ = "events"
    __table_args__ = CORE_SCHEMA

    id: Mapped[UUID] = mapped_column(primary_key=True, server_default=UUIDV7)
    type: Mapped[str] = mapped_column(Text)
    source: Mapped[str] = mapped_column(Text)
    subject_id: Mapped[UUID | None]
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    data: Mapped[dict[str, JSONValue]] = mapped_column(JSONB)
    version: Mapped[int]
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AuditLogEntry(Base):
    __tablename__ = "audit_log"
    __table_args__ = CORE_SCHEMA

    id: Mapped[UUID] = mapped_column(primary_key=True, server_default=UUIDV7)
    actor_type: Mapped[str] = mapped_column(Text)
    actor_id: Mapped[str] = mapped_column(Text)
    command: Mapped[str] = mapped_column(Text)
    args: Mapped[dict[str, JSONValue]] = mapped_column(JSONB)
    changes: Mapped[dict[str, JSONValue]] = mapped_column(JSONB, server_default=text("'{}'"))
    correlation_id: Mapped[UUID]
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
