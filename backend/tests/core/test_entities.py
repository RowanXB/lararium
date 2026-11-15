from decimal import Decimal
from uuid import UUID

from lararium.core.commands import CommandContext
from lararium.core.entities import (
    PlaceDetails,
    get_or_create_organization,
    get_or_create_place,
    get_self_entity_id,
)
from lararium.core.models import Entity


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


async def test_organization_found_and_tagged_by_external_id(session, run) -> None:
    tagged = await run(get_or_create_organization, "Test Grocer", external_id=("shop", "m_1"))
    entity = await session.get_one(Entity, tagged)
    await session.refresh(entity)
    assert entity.external_ids == {"shop": "m_1"}
    # The external id wins over the name, so a renamed merchant stays the same entity.
    assert await run(get_or_create_organization, "Renamed", external_id=("shop", "m_1")) == tagged
    # A name match without the id is tagged with it, keeping the first id it was given.
    other = await run(get_or_create_organization, "test grocer", external_id=("shop", "m_2"))
    assert other == tagged
    await session.refresh(entity)
    assert entity.external_ids == {"shop": "m_1"}


async def test_place_created_then_found_by_external_id(session, run) -> None:
    details = PlaceDetails(address_city="Testville", latitude=Decimal("51.5"), online=False)
    first = await run(get_or_create_place, "Test Cafe", external_id=("loc", "p_1"), details=details)
    again = await run(get_or_create_place, "Test Cafe", external_id=("loc", "p_1"), details=details)
    assert first == again
    entity = await session.get_one(Entity, first)
    await session.refresh(entity)
    assert (entity.kind, entity.external_ids) == ("place", {"loc": "p_1"})
    assert (entity.address_city, entity.latitude, entity.online) == (
        "Testville",
        Decimal("51.5"),
        False,
    )
    assert entity.owner_entity_id == await get_self_entity_id(session)


async def test_place_details_updated_when_changed(session, run) -> None:
    place = await run(
        get_or_create_place,
        "Test Cafe",
        external_id=("loc", "p_1"),
        details=PlaceDetails(address_city="Testville", mcc="5814"),
    )
    await run(
        get_or_create_place,
        "Test Cafe 2",
        external_id=("loc", "p_1"),
        details=PlaceDetails(address_city="Otherton", longitude=Decimal("-0.25")),
    )
    entity = await session.get_one(Entity, place)
    await session.refresh(entity)
    assert entity.name == "Test Cafe 2"
    assert (entity.address_city, entity.longitude, entity.mcc) == (
        "Otherton",
        Decimal("-0.25"),
        None,
    )


async def test_place_not_confused_with_organization(run) -> None:
    organization = await run(get_or_create_organization, "Same", external_id=("loc", "x"))
    place = await run(get_or_create_place, "Same", external_id=("loc", "x"), details=PlaceDetails())
    assert organization != place
    assert await run(get_or_create_organization, "Same", external_id=("loc", "x")) == organization
    assert (
        await run(get_or_create_place, "Same", external_id=("loc", "x"), details=PlaceDetails())
        == place
    )
