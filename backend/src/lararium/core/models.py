from datetime import datetime
from uuid import UUID

from sqlalchemy import DateTime, ForeignKey, LargeBinary, Text, func, text
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from lararium.core.json_types import JSONValue

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


class Connection(Base):
    __tablename__ = "connections"
    __table_args__ = CORE_SCHEMA

    id: Mapped[UUID] = mapped_column(primary_key=True, server_default=UUIDV7)
    connector: Mapped[str] = mapped_column(Text)
    display_name: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text)
    config: Mapped[dict[str, JSONValue]] = mapped_column(JSONB, server_default=text("'{}'"))
    # none_as_null: a None cursor is SQL NULL, not the JSON value null.
    cursor: Mapped[JSONValue] = mapped_column(JSONB(none_as_null=True), nullable=True)
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    consecutive_failures: Mapped[int] = mapped_column(server_default=text("0"))
    credential_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    warnings: Mapped[list[str]] = mapped_column(JSONB, server_default=text("'[]'"))
    owner_entity_id: Mapped[UUID] = mapped_column(ForeignKey("core.entities.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Credential(Base):
    __tablename__ = "credentials"
    __table_args__ = CORE_SCHEMA

    connection_id: Mapped[UUID] = mapped_column(
        ForeignKey("core.connections.id", ondelete="CASCADE"), primary_key=True
    )
    purpose: Mapped[str] = mapped_column(Text, primary_key=True)
    nonce: Mapped[bytes] = mapped_column(LargeBinary)
    ciphertext: Mapped[bytes] = mapped_column(LargeBinary)
    key_version: Mapped[int] = mapped_column(server_default=text("1"))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class RawRecord(Base):
    __tablename__ = "raw_records"
    __table_args__ = CORE_SCHEMA

    id: Mapped[UUID] = mapped_column(primary_key=True, server_default=UUIDV7)
    connection_id: Mapped[UUID] = mapped_column(ForeignKey("core.connections.id"))
    sync_id: Mapped[UUID]
    record_type: Mapped[str] = mapped_column(Text)
    request: Mapped[dict[str, JSONValue]] = mapped_column(JSONB)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    content: Mapped[JSONValue] = mapped_column(JSONB)
    content_hash: Mapped[str] = mapped_column(Text)
    connector_version: Mapped[str] = mapped_column(Text)


class WebhookInboxEntry(Base):
    __tablename__ = "webhook_inbox"
    __table_args__ = CORE_SCHEMA

    id: Mapped[UUID] = mapped_column(primary_key=True, server_default=UUIDV7)
    source: Mapped[str] = mapped_column(Text)
    body: Mapped[JSONValue] = mapped_column(JSONB)
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    outcome: Mapped[str | None] = mapped_column(Text)
