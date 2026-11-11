from uuid import UUID

from sqlalchemy import exists, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from financial_gate.core.commands import CommandContext
from financial_gate.core.models import Entity


async def get_self_entity_id(session: AsyncSession) -> UUID:
    return (await session.execute(select(Entity.id).where(Entity.is_self))).scalar_one()


async def get_or_create_organization(ctx: CommandContext, name: str) -> UUID:
    """Return the organization whose name or alias matches (ignoring case), creating it if none."""
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
    if existing is not None:
        return existing
    organization = Entity(
        kind="organization",
        name=name,
        owner_entity_id=await get_self_entity_id(ctx.session),
    )
    ctx.session.add(organization)
    await ctx.session.flush()
    return organization.id
