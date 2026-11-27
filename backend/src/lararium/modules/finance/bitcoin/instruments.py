"""Bitcoin, the one instrument a wallet holds (design 5.4).

Bitcoin is one instrument wherever it is held, so the wallet looks for the one another source
made before making its own: by `chain_native` `bitcoin`, then Binance's `binance_asset` `BTC`,
then CoinGecko's `coingecko` `bitcoin`, then the one crypto instrument with the symbol `BTC`
that no source tags as something else. Whichever it finds gains the three identifiers it
lacks, so Binance's finder and the price sources find the same one. Several untagged `BTC`
instruments are not guessed between: a new one is made and the owner is asked to merge them.
"""

from collections.abc import Callable
from uuid import UUID

from sqlalchemy import func, select

from lararium.core.commands import CommandContext, command_context
from lararium.core.connectors import SyncContext
from lararium.modules.finance import models
from lararium.modules.finance.instruments import (
    IdentifierScheme,
    InstrumentIdentifier,
    add_identifiers,
    create_instrument,
    find_by_identifier,
)

SYMBOL = "BTC"
NAME = "Bitcoin"
DECIMALS = 8
IDENTIFIERS = (
    InstrumentIdentifier("chain_native", "bitcoin"),
    InstrumentIdentifier("binance_asset", SYMBOL),
    InstrumentIdentifier("coingecko", "bitcoin"),
)
SEVERAL_FLAG = "several BTC instruments: merge them"
# A BTC instrument carrying one of these names what that source says it is, which the finder
# did not match: it is not taken for Bitcoin by its symbol.
_TAGS: tuple[IdentifierScheme, ...] = ("chain_native", "binance_asset", "coingecko")


async def _same_symbol(cmd: CommandContext) -> list[UUID]:
    tagged = select(models.InstrumentIdentifier.instrument_id).where(
        models.InstrumentIdentifier.scheme.in_(_TAGS)
    )
    rows = await cmd.session.scalars(
        select(models.Instrument.id)
        .where(
            models.Instrument.kind == "crypto",
            func.upper(models.Instrument.symbol) == SYMBOL,
            models.Instrument.id.not_in(tagged),
        )
        .order_by(models.Instrument.id)
    )
    return list(rows)


async def _find_or_create(cmd: CommandContext, flag: Callable[[str], None] | None) -> UUID:
    for identifier in IDENTIFIERS:
        found = await find_by_identifier(cmd.session, identifier.scheme, identifier.value)
        if found is not None:
            await add_identifiers(cmd, found.id, IDENTIFIERS)
            return found.id
    same = await _same_symbol(cmd)
    if len(same) == 1:
        await add_identifiers(cmd, same[0], IDENTIFIERS)
        return same[0]
    if same and flag is not None:
        flag(SEVERAL_FLAG)
    created = await create_instrument(
        cmd,
        kind="crypto",
        symbol=SYMBOL,
        name=NAME,
        decimals=DECIMALS,
        quote_instrument_id=None,
        identifiers=IDENTIFIERS,
    )
    return created.id


async def bitcoin(ctx: SyncContext, flag: Callable[[str], None] | None = None) -> UUID:
    """The Bitcoin instrument, found or made (module docstring).

    In a transaction of its own, committed at once, as `ConnectorRun.shared` does: the
    instrument is a row other connections share, whose lock must not be held until the sync
    commits. `flag` is told what the owner should look at."""
    async with (
        ctx.session_factory() as session,
        command_context(session, ctx.command.actor, "finance.shared_row", {}) as cmd,
    ):
        return await _find_or_create(cmd, flag)
