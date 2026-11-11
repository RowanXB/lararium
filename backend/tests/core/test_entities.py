from uuid import UUID

from financial_gate.core.commands import CommandContext
from financial_gate.core.entities import get_or_create_organization, get_self_entity_id
from financial_gate.core.models import Entity


async def test_organization_matched_case_insensitively(run) -> None:
    first = await run(get_or_create_organization, "Example Bank")
    second = await run(get_or_create_organization, "example bank")
    assert first == second


async def test_self_entity_id_is_stable(session) -> None:
    assert await get_self_entity_id(session) == await get_self_entity_id(session)


async def test_organization_matched_by_alias(run) -> None:
    async def create_with_alias(ctx: CommandContext) -> UUID:
        organization_id = await get_or_create_organization(ctx, "Example Bank plc")
        entity = await ctx.session.get_one(Entity, organization_id)
        entity.aliases = ["EXB"]
        return organization_id

    first = await run(create_with_alias)
    assert await run(get_or_create_organization, "exb") == first
