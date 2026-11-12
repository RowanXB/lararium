"""Raw records: what a connector fetched, kept as received and never changed."""

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from lararium.core import models
from lararium.core.commands import CommandContext
from lararium.core.json_types import JSONValue


@dataclass(frozen=True)
class RawRecord:
    id: UUID
    connection_id: UUID
    sync_id: UUID
    record_type: str
    request: dict[str, JSONValue]
    fetched_at: datetime
    content: JSONValue
    content_hash: str
    connector_version: str


def _content_hash(content: JSONValue) -> str:
    canonical = json.dumps(content, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


async def store_raw_record(
    ctx: CommandContext,
    *,
    connection_id: UUID,
    record_type: str,
    request: dict[str, JSONValue],
    content: JSONValue,
    connector_version: str,
    sync_id: UUID,
) -> UUID:
    """Store a fetched record; if the same content is already stored, return that one's id."""
    r = models.RawRecord
    digest = _content_hash(content)
    inserted = await ctx.session.scalar(
        insert(r)
        .values(
            connection_id=connection_id,
            sync_id=sync_id,
            record_type=record_type,
            request=request,
            content=content,
            content_hash=digest,
            connector_version=connector_version,
        )
        .on_conflict_do_nothing(index_elements=[r.connection_id, r.record_type, r.content_hash])
        .returning(r.id)
    )
    if inserted is not None:
        ctx.record_change(
            f"raw_record:{inserted}", {"record_type": record_type, "content_hash": digest}
        )
        return inserted
    return (
        await ctx.session.execute(
            select(r.id).where(
                r.connection_id == connection_id,
                r.record_type == record_type,
                r.content_hash == digest,
            )
        )
    ).scalar_one()


async def list_raw_records(session: AsyncSession, sync_id: UUID) -> list[RawRecord]:
    """The records first stored by this sync, in the order they were stored."""
    r = models.RawRecord
    rows = await session.execute(
        select(
            r.id,
            r.connection_id,
            r.sync_id,
            r.record_type,
            r.request,
            r.fetched_at,
            r.content,
            r.content_hash,
            r.connector_version,
        )
        .where(r.sync_id == sync_id)
        .order_by(r.id)
    )
    return [
        RawRecord(
            id=row.id,
            connection_id=row.connection_id,
            sync_id=row.sync_id,
            record_type=row.record_type,
            request=row.request,
            fetched_at=row.fetched_at,
            content=row.content,
            content_hash=row.content_hash,
            connector_version=row.connector_version,
        )
        for row in rows
    ]
