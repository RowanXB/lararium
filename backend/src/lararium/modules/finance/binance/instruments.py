"""Finding or creating the instrument of a Binance asset code (design 5.5).

An asset is an instrument already known by its `binance_asset` identifier (whatever
`coin-config` says later), the existing fiat instrument when Binance marks it as legal money
(none, and flagged, when Lararium has no currency of that code: its decimals cannot be
guessed), the one existing crypto instrument with that
symbol (which then gains the identifier), or a new crypto instrument. An `LD`-prefixed code that
mirrors another asset is never an instrument. Every asset resolved is recorded in
`binance_assets_seen`, which decides the trade symbols a later sync asks for.
"""

from collections.abc import Callable
from uuid import UUID

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from lararium.core.commands import CommandContext
from lararium.modules.finance import models
from lararium.modules.finance.binance.models import CoingeckoId
from lararium.modules.finance.instruments import (
    InstrumentIdentifier,
    add_identifiers,
    create_instrument,
    find_by_identifier,
    normalize_identifier,
)

DECIMALS = 8
# The prefix of a code that mirrors another asset in Earn (`LDAAA` stands for `AAA`).
MIRROR = "LD"


async def assets_seen(session: AsyncSession, connection_id: UUID) -> set[str]:
    """Every asset the connection has shown, in a balance or a record."""
    rows = await session.scalars(
        text("select asset from finance.binance_assets_seen where connection_id = :c"),
        {"c": connection_id},
    )
    return set(rows)


async def _see(session: AsyncSession, connection_id: UUID, code: str) -> None:
    await session.execute(
        text(
            "insert into finance.binance_assets_seen (connection_id, asset) values (:c, :a)"
            " on conflict do nothing"
        ),
        {"c": connection_id, "a": code},
    )


async def _coin(session: AsyncSession, connection_id: UUID, code: str) -> tuple[str | None, bool]:
    """The name and legal-money flag of the newest `coin-config` row of the code; none seen is
    (None, False)."""
    row = (
        await session.execute(
            text(
                "select name, is_legal_money from finance.binance_coin_config"
                " where connection_id = :c and coin = :a order by snapshot_at desc limit 1"
            ),
            {"c": connection_id, "a": code},
        )
    ).first()
    return (None, False) if row is None else (row.name, row.is_legal_money is True)


async def _known(session: AsyncSession, connection_id: UUID, code: str) -> bool:
    """Whether the code is an asset: seen, an instrument of its own, or listed by coin-config."""
    if code in await assets_seen(session, connection_id):
        return True
    if await find_by_identifier(session, "binance_asset", code) is not None:
        return True
    return (
        await session.scalar(
            text(
                "select exists (select 1 from finance.binance_coin_config"
                " where connection_id = :c and coin = :a)"
            ),
            {"c": connection_id, "a": code},
        )
        is True
    )


async def _same_symbol(session: AsyncSession, code: str) -> list[UUID]:
    """The crypto instruments with this symbol (any case) that are no other Binance asset."""
    tagged = select(models.InstrumentIdentifier.instrument_id).where(
        models.InstrumentIdentifier.scheme == "binance_asset"
    )
    rows = await session.scalars(
        select(models.Instrument.id)
        .where(
            models.Instrument.kind == "crypto",
            func.upper(models.Instrument.symbol) == code,
            models.Instrument.id.not_in(tagged),
        )
        .order_by(models.Instrument.id)
    )
    return list(rows)


def uncounted_flag(code: str) -> str:
    return f"{code} is a currency Lararium does not have: it is not counted"


async def _settled(
    session: AsyncSession, connection_id: UUID, code: str
) -> tuple[UUID | None, bool]:
    """What already decides a code: the instrument it resolves to, and whether it is legal
    money Lararium has no currency of. A code with a `binance_asset` instrument keeps it,
    whatever `coin-config` says now (a holding never drops out of a report); only a code with
    none is the fiat of its ISO code when Binance marks it legal money, or not counted."""
    found = await find_by_identifier(session, "binance_asset", code)
    if found is not None:
        return found.id, False
    _, legal_money = await _coin(session, connection_id, code)
    if not legal_money:
        return None, False
    currency = await find_by_identifier(session, "iso4217", code)
    return (None, True) if currency is None else (currency.id, False)


async def uncounted(session: AsyncSession, connection_id: UUID, code: str) -> bool:
    """Whether the code is legal money Lararium has no currency of: no instrument, and not
    counted (`asset` gives it none)."""
    code = normalize_identifier("binance_asset", code)
    return (await _settled(session, connection_id, code))[1]


def clash_flag(code: str) -> str:
    return f"{code} may be the same coin as another instrument"


async def _by_coingecko_id(session: AsyncSession, code: str) -> tuple[UUID | None, bool]:
    """The crypto instrument that has the asset's CoinGecko id (the map of `prices` lists it),
    and whether one was found but already stands for another Binance asset (not taken)."""
    coin = await session.scalar(select(CoingeckoId.coingecko_id).where(CoingeckoId.asset == code))
    if coin is None:
        return None, False
    found = await find_by_identifier(session, "coingecko", coin)
    if found is None or found.kind != "crypto":
        return None, False
    if any(i.scheme == "binance_asset" for i in found.identifiers):
        return None, True
    return found.id, False


async def asset(
    ctx: CommandContext,
    connection_id: UUID,
    code: str,
    *,
    flag: Callable[[str], None] | None = None,
) -> UUID | None:
    """The instrument of an asset code, created if new; None for the mirror of another asset
    and for legal money Lararium has no currency of (`uncounted`, flagged).

    `flag` is told what the owner should look at (`several instruments named <code>`).
    """
    session = ctx.session
    code = normalize_identifier("binance_asset", code)
    if code.startswith(MIRROR) and await _known(session, connection_id, code[len(MIRROR) :]):
        return None
    await _see(session, connection_id, code)
    found, missing = await _settled(session, connection_id, code)
    if found is not None:
        return found
    if missing:
        if flag is not None:
            flag(uncounted_flag(code))
        return None
    name, _ = await _coin(session, connection_id, code)
    identifier = InstrumentIdentifier("binance_asset", code)
    by_id, clash = await _by_coingecko_id(session, code)
    if clash and flag is not None:
        flag(clash_flag(code))
    if by_id is not None:
        await add_identifiers(ctx, by_id, [identifier])
        return by_id
    same = await _same_symbol(session, code)
    if len(same) == 1:
        await add_identifiers(ctx, same[0], [identifier])
        return same[0]
    if len(same) > 1 and flag is not None:
        flag(f"several instruments named {code}")
    created = await create_instrument(
        ctx,
        kind="crypto",
        symbol=code,
        name=name or code,
        decimals=DECIMALS,
        quote_instrument_id=None,
        identifiers=[identifier],
    )
    return created.id
