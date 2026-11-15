from dataclasses import asdict, dataclass
from decimal import Decimal
from uuid import UUID

from sqlalchemy import exists, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from lararium.core.commands import CommandContext
from lararium.core.json_types import JSONValue
from lararium.core.models import Entity


async def get_self_entity_id(session: AsyncSession) -> UUID:
    return (await session.execute(select(Entity.id).where(Entity.is_self))).scalar_one()


async def get_or_create_organization(
    ctx: CommandContext, name: str, *, external_id: tuple[str, str] | None = None
) -> UUID:
    """Return the organization whose name or alias matches (ignoring case), creating it if none.

    With an external id (scheme, value), an organization already carrying it wins over a name
    match, and the one returned is tagged with it unless it carries another for that scheme.

    Concurrent callers are serialised on the name, so only one creates it; call it in a short
    transaction, as the lock is held until it commits.
    """
    await ctx.session.execute(
        select(func.pg_advisory_xact_lock(func.hashtext("organization:" + name.lower())))
    )
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


@dataclass(frozen=True)
class PlaceDetails:
    """What a source says about a place (a merchant location); None means it says nothing."""

    address_street: str | None = None
    address_city: str | None = None
    address_region: str | None = None
    address_postcode: str | None = None
    address_country: str | None = None
    address_formatted: str | None = None
    latitude: Decimal | None = None
    longitude: Decimal | None = None
    location_approximate: bool | None = None
    online: bool | None = None
    mcc: str | None = None
    website: str | None = None
    card_acceptor_website: str | None = None
    logo_url: str | None = None


def _place_change(place: Entity) -> dict[str, JSONValue]:
    data: dict[str, JSONValue] = {"id": str(place.id), "name": place.name}
    for field, value in asdict(_details_of(place)).items():
        data[field] = str(value) if isinstance(value, Decimal) else value
    return data


def _details_of(place: Entity) -> PlaceDetails:
    return PlaceDetails(**{field: getattr(place, field) for field in asdict(PlaceDetails())})


async def get_or_create_place(
    ctx: CommandContext,
    name: str | None,
    *,
    external_id: tuple[str, str],
    details: PlaceDetails,
) -> UUID:
    """Return the place carrying the external id (scheme, value), creating it if none.

    The name and the details the source gives are brought in line with what it now says,
    recording a change when they differ; a name or detail given as None keeps the stored
    value (a new place without a name is named by its external id). A place is identified by
    its external id alone: never matched to an organization, or by name.

    Concurrent callers are serialised on the external id; call it in a short transaction, as
    the lock is held until it commits.
    """
    scheme, value = external_id
    given = {field: new for field, new in asdict(details).items() if new is not None}
    await ctx.session.execute(
        select(func.pg_advisory_xact_lock(func.hashtext(f"place:{scheme}:{value}")))
    )
    place = (
        await ctx.session.execute(
            select(Entity)
            .where(Entity.kind == "place", Entity.external_ids[scheme].astext == value)
            .order_by(Entity.id)
            .limit(1)
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if place is None:
        place = Entity(
            kind="place",
            name=name if name is not None else value,
            external_ids={scheme: value},
            owner_entity_id=await get_self_entity_id(ctx.session),
            **asdict(details),
        )
        ctx.session.add(place)
        await ctx.session.flush()
    elif (name is not None and place.name != name) or any(
        getattr(place, field) != new for field, new in given.items()
    ):
        if name is not None:
            place.name = name
        for field, new in given.items():
            setattr(place, field, new)
        await ctx.session.flush()
    else:
        return place.id
    ctx.record_change(f"entity:{place.id}", _place_change(place))
    return place.id
