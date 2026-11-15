from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Literal
from uuid import UUID

from sqlalchemy import Boolean, func, literal_column, tuple_
from sqlalchemy.dialects.postgresql import insert

from lararium.core.commands import CommandContext
from lararium.core.entities import get_self_entity_id
from lararium.modules.finance.models import DeclinedTransaction


@dataclass(frozen=True)
class DeclinedInput:
    connection_id: UUID
    external_id: str
    account_id: UUID
    occurred_at: datetime
    quantity: Decimal
    instrument_id: UUID
    description: str
    counterparty_entity_id: UUID | None
    place_entity_id: UUID | None
    decline_reason: str


_VALUE_COLUMNS = (
    "account_id",
    "occurred_at",
    "quantity",
    "instrument_id",
    "description",
    "counterparty_entity_id",
    "place_entity_id",
    "decline_reason",
)


async def upsert_declined(
    ctx: CommandContext, d: DeclinedInput
) -> Literal["created", "updated", "unchanged"]:
    """Record a declined payment once per (connection, external id), updating it if it changed."""
    t = DeclinedTransaction
    values = {name: getattr(d, name) for name in _VALUE_COLUMNS}
    statement = insert(t).values(
        connection_id=d.connection_id,
        external_id=d.external_id,
        owner_entity_id=await get_self_entity_id(ctx.session),
        **values,
    )
    excluded = statement.excluded
    statement = statement.on_conflict_do_update(
        index_elements=[t.connection_id, t.external_id],
        set_={**{name: excluded[name] for name in _VALUE_COLUMNS}, "updated_at": func.now()},
        where=tuple_(*(t.__table__.c[name] for name in _VALUE_COLUMNS)).is_distinct_from(
            tuple_(*(excluded[name] for name in _VALUE_COLUMNS))
        ),
    ).returning(t.id, literal_column("(xmax = 0)", Boolean).label("inserted"))
    row = (await ctx.session.execute(statement)).one_or_none()
    if row is None:
        return "unchanged"
    ctx.record_change(
        f"declined_transaction:{row.id}",
        {
            "id": str(row.id),
            "connection_id": str(d.connection_id),
            "external_id": d.external_id,
            "account_id": str(d.account_id),
            "occurred_at": d.occurred_at.isoformat(),
            "quantity": str(d.quantity),
            "instrument_id": str(d.instrument_id),
            "description": d.description,
            "counterparty_entity_id": None
            if d.counterparty_entity_id is None
            else str(d.counterparty_entity_id),
            "place_entity_id": None if d.place_entity_id is None else str(d.place_entity_id),
            "decline_reason": d.decline_reason,
        },
    )
    return "created" if row.inserted else "updated"
