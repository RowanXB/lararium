"""SQLAlchemy models of the `finance.binance_*` tables.

The typed tables are built from `layout`, the one rule that also decides `columns_for`; the
migrations hold the same tables as frozen SQL, and a test compares the two. `model_of(table)`
gives a table's model; `models_of(label)` the models of a label, the root first.
"""

from datetime import datetime
from typing import Any, cast
from uuid import UUID

from sqlalchemy import (
    ARRAY,
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Integer,
    Numeric,
    Text,
    func,
)
from sqlalchemy import Uuid as UuidType
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import TypeEngine

from lararium.core.models import Base
from lararium.modules.finance.binance import layout

FINANCE = "finance"
FINANCE_SCHEMA = {"schema": FINANCE}


def _type(sql: str) -> TypeEngine[Any]:
    if sql.endswith("[]"):
        return ARRAY(_type(sql[:-2]))
    return {
        "uuid": UuidType(),
        "text": Text(),
        "bigint": BigInteger(),
        "integer": Integer(),
        "numeric": Numeric(),
        "boolean": Boolean(),
        "timestamptz": DateTime(timezone=True),
    }[sql]


class AssetsSeen(Base):
    """Every asset a connection has shown, in a balance or a record (it decides which trade
    symbols a later sync asks for)."""

    __tablename__ = "binance_assets_seen"
    __table_args__ = FINANCE_SCHEMA

    connection_id: Mapped[UUID] = mapped_column(ForeignKey("core.connections.id"), primary_key=True)
    asset: Mapped[str] = mapped_column(Text, primary_key=True)
    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


def _class_name(table: str) -> str:
    return "".join(part.capitalize() for part in table.split("_"))


def _build(table: layout.Table) -> type[Base]:
    attributes: dict[str, object] = {"__tablename__": table.name}
    constraints: list[object] = []
    if table.parent is not None:
        parent = table.parent
        constraints.append(
            ForeignKeyConstraint(
                list(parent.key),
                [f"{FINANCE}.{parent.name}.{name}" for name in parent.key],
                ondelete="CASCADE",
            )
        )
    constraints.append({"schema": FINANCE})
    attributes["__table_args__"] = tuple(constraints)
    for column in table.columns:
        arguments: list[Any] = [_type(column.sql)]
        if column.name == "connection_id" and table.parent is None:
            arguments.append(ForeignKey("core.connections.id"))
        if column.name == "transaction_id":
            arguments.append(ForeignKey("finance.transactions.id", ondelete="SET NULL"))
        attributes[column.name] = mapped_column(
            *arguments,
            primary_key=column.name in table.key,
            nullable=column.nullable,
        )
    return cast(type[Base], type(_class_name(table.name), (Base,), attributes))


_MODELS: dict[str, type[Base]] = {}
for _label in layout.TABLE_LABELS:
    for _table in layout.layout(_label):
        _MODELS[_table.name] = _build(_table)


def model_of(table: str) -> type[Base]:
    return _MODELS[table]


def models_of(label: str) -> list[type[Base]]:
    return [_MODELS[table.name] for table in layout.layout(label)]
