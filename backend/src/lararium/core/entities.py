from uuid import UUID

from sqlalchemy import exists, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from lararium.core.commands import CommandContext
from lararium.core.models import Entity


async def get_self_entity_id(session: AsyncSession) -> UUID:
    return (await session.execute(select(Entity.id).where(Entity.is_self))).scalar_one()


async def get_or_create_organization(
    ctx: CommandContext, name: str, *, external_id: tuple[str, str] | None = None
) -> UUID:
    """Return the organization whose name or alias matches (ignoring case), creating it if none.

    With an external id (scheme, value), an organization already carrying it wins over a name
    match, and the one returned is tagged with it unless it carries another for that scheme.
    """
    if external_id is not None:
        scheme, value = external_id
        tagged = await ctx.session.scalar(
            select(Entity.id)
            .where(Entity.kind == "organization", Entity.external_ids[scheme].astext == value)
            .order_by(Entity.id)
            .limit(1)
        )
        if tagged is not None:
            return tagged
    wanted = func.lower(name)
    alias = func.unnest(Entity.aliases).table_valued("value").render_derived()
    alias_matches = exists().select_from(alias).where(func.lower(alias.c.value) == wanted)
    existing = await ctx.session.scalar(
        select(Entity.id)
        .where(Entity.kind == "organization")
        .where(or_(func.lower(Entity.name) == wanted, alias_matches))
        .order_by(Entity.id)
        .limit(1)
    )
    if existing is None:
        organization = Entity(
            kind="organization",
            name=name,
            owner_entity_id=await get_self_entity_id(ctx.session),
        )
        ctx.session.add(organization)
        await ctx.session.flush()
        existing = organization.id
    if external_id is not None:
        scheme, value = external_id
        await ctx.session.execute(
            update(Entity)
            .where(Entity.id == existing, ~Entity.external_ids.has_key(scheme))
            .values(
                external_ids=Entity.external_ids.op("||")(func.jsonb_build_object(scheme, value))
            )
        )
    return existing
