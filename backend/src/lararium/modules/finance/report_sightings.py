"""When a sync saw each report it stored: any connector's current figures (balances,
positions), as opposed to its history.

A raw record is stored once per content, first fetched by the sync that stored it. A report
holds at the time it was seen, and its content may come back (A, then B, then A again): the
raw record alone cannot say when. So a sync records, for every report record it stores or
finds stored, a sighting: the sync and the time the response arrived. A report is dated by
its sighting, and a normalizer replaying every record reproduces the same dates from them.
"""

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from lararium.core import models as core_models
from lararium.core.commands import CommandContext
from lararium.modules.finance import models


@dataclass(frozen=True)
class Sighting:
    raw_record_id: UUID
    sync_id: UUID
    seen_at: datetime


async def record_sighting(
    ctx: CommandContext,
    *,
    connection_id: UUID,
    raw_record_id: UUID,
    sync_id: UUID,
    seen_at: datetime,
) -> None:
    """Record that the sync saw the record at `seen_at`; once per sync and record."""
    if seen_at.utcoffset() is None:
        raise ValueError("seen_at needs a timezone")
    s = models.ReportSighting
    inserted = await ctx.session.scalar(
        insert(s)
        .values(
            connection_id=connection_id,
            raw_record_id=raw_record_id,
            sync_id=sync_id,
            seen_at=seen_at,
        )
        .on_conflict_do_nothing(index_elements=[s.connection_id, s.raw_record_id, s.sync_id])
        .returning(s.raw_record_id)
    )
    if inserted is not None:
        ctx.record_change(
            f"report_sighting:{sync_id}:{raw_record_id}", {"seen_at": seen_at.isoformat()}
        )


async def list_sightings(
    session: AsyncSession, connection_id: UUID, *, sync_id: UUID | None = None
) -> list[Sighting]:
    """The connection's sightings (one sync's, when given), in the order they were seen."""
    s = models.ReportSighting
    query = select(s.raw_record_id, s.sync_id, s.seen_at).where(s.connection_id == connection_id)
    if sync_id is not None:
        query = query.where(s.sync_id == sync_id)
    rows = await session.execute(query.order_by(s.seen_at, s.sync_id, s.raw_record_id))
    return [Sighting(row.raw_record_id, row.sync_id, row.seen_at) for row in rows]


async def first_sighting(
    session: AsyncSession, connection_id: UUID, record_type: str
) -> Sighting | None:
    """The connection's earliest sighting of a record of the type."""
    s, r = models.ReportSighting, core_models.RawRecord
    row = (
        await session.execute(
            select(s.raw_record_id, s.sync_id, s.seen_at)
            .join(r, r.id == s.raw_record_id)
            .where(s.connection_id == connection_id, r.record_type == record_type)
            .order_by(s.seen_at, s.sync_id, s.raw_record_id)
            .limit(1)
        )
    ).one_or_none()
    return None if row is None else Sighting(row.raw_record_id, row.sync_id, row.seen_at)
