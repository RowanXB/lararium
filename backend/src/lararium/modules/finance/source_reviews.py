"""Review items between two sources of one event, and the owner's decisions (design §5.7,
Decision 14).

When two sources of an event (eToro's API and its statement) disagree on a value that makes a
posting or its date, the normalizer opens a review item: both values, open until the owner
takes one. Taking one is also a decision, kept by the event's key and the field, which the
normalizer reads (`decision_for`), so regeneration replays it. A disagreement the owner has
decided, with the same values, opens nothing; a later change in either value does. An open
item whose sources come to agree closes on its own (`close_agreed`), decided by no one.

Values are kept as text, as the normalizer compares them; they are the owner's data, so no
message here names one.
"""

from collections.abc import Collection
from typing import Literal, cast
from uuid import UUID

from sqlalchemy import func, or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from lararium.core.commands import CommandContext
from lararium.modules.finance import models
from lararium.modules.finance.errors import ReviewNotFoundError, ReviewResolvedError
from lararium.modules.finance.schemas import ReviewOut

type Take = Literal["api", "statement"]
# Who closed an item whose two sources came to agree: no one decided it.
AGREED = "sources agree"


async def open_review(
    ctx: CommandContext,
    connection_id: UUID,
    transaction_id: UUID | None,
    event_key: str,
    field: str,
    api_value: str,
    statement_value: str,
) -> bool:
    """Open a review item for the disagreement, or bring the open one's values up to date.
    False when there is nothing new for the owner: an identical item is open, or the latest
    resolved one has these very values (the owner decided them)."""
    r = models.SourceReview
    same = (r.connection_id == connection_id, r.event_key == event_key, r.field == field)
    rows = await ctx.session.execute(
        select(r.id, r.api_value, r.statement_value, r.resolved_at)
        # An item closed because the sources agreed was decided by no one.
        .where(*same, or_(r.resolved_at.is_(None), r.resolved_by != AGREED))
        .order_by(r.resolved_at.desc().nulls_first(), r.opened_at.desc())
        .limit(1)
        .with_for_update()
    )
    latest = rows.one_or_none()
    values = (api_value, statement_value)
    if latest is not None and (latest.api_value, latest.statement_value) == values:
        return False
    if latest is not None and latest.resolved_at is None:
        await ctx.session.execute(
            update(r)
            .where(r.id == latest.id)
            .values(
                api_value=api_value,
                statement_value=statement_value,
                transaction_id=transaction_id,
                opened_at=func.now(),
            )
        )
        ctx.record_change(f"source_review:{latest.id}", {"event_key": event_key, "field": field})
        return True
    review_id = await ctx.session.scalar(
        insert(r)
        .values(
            connection_id=connection_id,
            transaction_id=transaction_id,
            event_key=event_key,
            field=field,
            api_value=api_value,
            statement_value=statement_value,
        )
        # A run opening the same item meanwhile has opened it: nothing new to tell.
        .on_conflict_do_nothing(
            index_elements=[r.connection_id, r.event_key, r.field],
            index_where=r.resolved_at.is_(None),
        )
        .returning(r.id)
    )
    if review_id is None:
        return False
    ctx.record_change(f"source_review:{review_id}", {"event_key": event_key, "field": field})
    return True


async def close_agreed(
    ctx: CommandContext, connection_id: UUID, event_key: str, fields: Collection[str]
) -> None:
    """Close the event's open items on these fields, whose two sources now agree: resolved
    `api` by AGREED, no decision recorded, no one told."""
    if not fields:
        return
    r = models.SourceReview
    closed = await ctx.session.scalars(
        update(r)
        .where(
            r.connection_id == connection_id,
            r.event_key == event_key,
            r.field.in_(sorted(fields)),
            r.resolved_at.is_(None),
        )
        .values(resolved_at=func.now(), resolution="api", resolved_by=AGREED)
        .returning(r.id)
    )
    for review_id in closed:
        ctx.record_change(f"source_review:{review_id}", {"resolution": "api", "by": AGREED})


async def resolve_review(ctx: CommandContext, review_id: UUID, take: Take) -> None:
    """Close the item, taking one source's value, and record that as the owner's decision."""
    r, d = models.SourceReview, models.SourceDecision
    row = (
        await ctx.session.execute(
            select(r.connection_id, r.event_key, r.field, r.resolved_at)
            .where(r.id == review_id)
            .with_for_update()
        )
    ).one_or_none()
    if row is None:
        raise ReviewNotFoundError(str(review_id))
    if row.resolved_at is not None:
        raise ReviewResolvedError(str(review_id))
    decided_by = ctx.actor.type
    await ctx.session.execute(
        update(r)
        .where(r.id == review_id)
        .values(resolved_at=func.now(), resolution=take, resolved_by=decided_by)
    )
    await ctx.session.execute(
        insert(d)
        .values(
            connection_id=row.connection_id,
            event_key=row.event_key,
            field=row.field,
            take=take,
            decided_by=decided_by,
        )
        .on_conflict_do_update(
            index_elements=[d.connection_id, d.event_key, d.field],
            set_={"take": take, "decided_at": func.now(), "decided_by": decided_by},
        )
    )
    ctx.record_change(f"source_review:{review_id}", {"resolution": take})
    ctx.record_change(
        f"source_decision:{row.connection_id}:{row.event_key}:{row.field}", {"take": take}
    )


async def decision_for(
    session: AsyncSession, connection_id: UUID, event_key: str, field: str
) -> Take | None:
    """The owner's take on the event's field, if any."""
    d = models.SourceDecision
    take = await session.scalar(
        select(d.take).where(
            d.connection_id == connection_id, d.event_key == event_key, d.field == field
        )
    )
    return cast(Take | None, take)


async def list_reviews(session: AsyncSession, connection_id: UUID) -> list[ReviewOut]:
    """The connection's review items, open ones first, newest first within each."""
    r = models.SourceReview
    rows = await session.execute(
        select(r)
        .where(r.connection_id == connection_id)
        .order_by(r.resolved_at.desc().nulls_first(), r.opened_at.desc(), r.id)
    )
    return [
        ReviewOut(
            id=item.id,
            connection_id=item.connection_id,
            transaction_id=item.transaction_id,
            event_key=item.event_key,
            field=item.field,
            api_value=item.api_value,
            statement_value=item.statement_value,
            opened_at=item.opened_at,
            resolved_at=item.resolved_at,
            resolution=cast(Take | None, item.resolution),
            resolved_by=item.resolved_by,
        )
        for item in rows.scalars()
    ]
