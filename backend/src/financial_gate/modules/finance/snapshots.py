from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Literal, cast
from uuid import UUID

from sqlalchemy import insert, select
from sqlalchemy.ext.asyncio import AsyncSession

from financial_gate.core.commands import CommandContext
from financial_gate.core.entities import get_self_entity_id
from financial_gate.core.json_types import JSONValue
from financial_gate.core.settings_store import get_timezone
from financial_gate.core.timeutil import local_date
from financial_gate.modules.finance import models
from financial_gate.modules.finance.accounts import get_account
from financial_gate.modules.finance.instruments import get_instrument
from financial_gate.modules.finance.money import require_finite, to_plain_str

SnapshotKind = Literal["reported", "manual"]


@dataclass(frozen=True)
class SnapshotRecord:
    id: UUID
    account_id: UUID
    instrument_id: UUID
    quantity: Decimal  # signed: negative for liabilities
    as_of: datetime
    as_of_date: date
    kind: SnapshotKind


async def _load(session: AsyncSession, snapshot_id: UUID) -> SnapshotRecord:
    # Select columns, not ORM objects, so a stale identity map can't hide an update.
    s = models.BalanceSnapshot
    row = (
        await session.execute(
            select(
                s.id, s.account_id, s.instrument_id, s.quantity, s.as_of, s.as_of_date, s.kind
            ).where(s.id == snapshot_id)
        )
    ).one()
    return SnapshotRecord(
        id=row.id,
        account_id=row.account_id,
        instrument_id=row.instrument_id,
        quantity=row.quantity,
        as_of=row.as_of,
        as_of_date=row.as_of_date,
        kind=cast(SnapshotKind, row.kind),
    )


def _change_data(record: SnapshotRecord) -> dict[str, JSONValue]:
    return {
        "id": str(record.id),
        "account_id": str(record.account_id),
        "instrument_id": str(record.instrument_id),
        "quantity": to_plain_str(record.quantity),
        "as_of": record.as_of.isoformat(),
        "as_of_date": record.as_of_date.isoformat(),
        "kind": record.kind,
    }


async def record_snapshot(
    ctx: CommandContext,
    *,
    account_id: UUID,
    instrument_id: UUID,
    quantity: Decimal,
    as_of: datetime,
    kind: SnapshotKind = "manual",
) -> SnapshotRecord:
    """Record an observed balance of one instrument in one account at a moment."""
    if as_of.utcoffset() is None:
        raise ValueError("as_of needs a timezone")
    require_finite("quantity", quantity)
    session = ctx.session
    await get_account(session, account_id)  # raises AccountNotFoundError if missing
    await get_instrument(session, instrument_id)  # raises InstrumentNotFoundError if missing
    snapshot_id = (
        await session.execute(
            insert(models.BalanceSnapshot)
            .values(
                account_id=account_id,
                instrument_id=instrument_id,
                quantity=quantity,
                as_of=as_of,
                as_of_date=local_date(as_of, await get_timezone(session)),
                kind=kind,
                owner_entity_id=await get_self_entity_id(session),
            )
            .returning(models.BalanceSnapshot.id)
        )
    ).scalar_one()
    record = await _load(session, snapshot_id)
    ctx.emit("finance.snapshot.recorded", record.id, {"snapshot_id": str(record.id)})
    ctx.record_change(f"snapshot:{record.id}", _change_data(record))
    return record
