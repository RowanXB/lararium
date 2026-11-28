"""BTC prices for a Bitcoin wallet, with or without a Binance connection (design 5.7).

M7's pricer (`binance.prices.update_prices`) runs under the wallet's own connection: Binance's
public daily bars (no key) price BTC in EUR from the first day the wallet's account held it to
yesterday (UTC), source `binance`, on the one BTC instrument Binance's connections price too.
The pairs come from Binance's `exchangeInfo`, read each run and stored nowhere; the days asked
are kept in the wallet's config (`bars_asked`), as a Binance connection keeps its own; the bars
and the prices are shared, so a day already stored is not written again. With the operator's
CoinGecko key, CoinGecko prices the days Binance cannot (its free plan reaches back 365 days).

The flag `no price for BTC` stands while a day from the first held to yesterday has no price
from any source: Binance's EUR markets begin in January 2020, so BTC held before then is
valued `missing_price` on those days (never 0) until the owner enters their prices by hand.

The step commits as it goes through `ctx.session_factory`, its record of the days asked under
the connection's row lock: the caller must not hold that lock in `ctx.command`.
"""

from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime, timedelta
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from lararium.core.connectors import SyncContext
from lararium.modules.finance import models
from lararium.modules.finance.binance.client import (
    BinanceClient,
    BinanceError,
    Clock,
    Sleep,
    real_sleep,
    wall_clock,
)
from lararium.modules.finance.binance.parse import BinanceDataError
from lararium.modules.finance.binance.prices import (
    Held,
    flag_for,
    held_on,
    listed_pairs,
    update_prices,
)

# What a failed price step raises; the sync logs it (`failure_shown`) and goes on.
PRICE_FAILURES = (BinanceError, BinanceDataError)


def failure_shown(error: Exception) -> str:
    """A failure as a log line may show it: an HTTP status at most, never a URL or a body."""
    if isinstance(error, BinanceError):
        return "no response" if error.http_status is None else f"HTTP {error.http_status}"
    return "an answer in an unexpected shape"


def _yesterday(ms: int) -> date:
    return datetime.fromtimestamp(ms // 1000, UTC).date() - timedelta(days=1)


async def _gaps(ctx: SyncContext, held: Mapping[str, Held], yesterday: date) -> list[str]:
    """The held assets with a day, from the first held to yesterday, that no price covers."""
    found: list[str] = []
    async with ctx.session_factory() as session:
        for code, one in sorted(held.items()):
            p = models.Price
            covered = await session.scalar(
                select(func.count(func.distinct(p.as_of_date))).where(
                    p.instrument_id == one.instrument_id,
                    p.as_of_date >= one.first_day,
                    p.as_of_date <= yesterday,
                )
            )
            if (covered or 0) < (yesterday - one.first_day).days + 1:
                found.append(code)
    return found


async def update_btc_prices(
    ctx: SyncContext, account_id: UUID, *, clock: Clock = wall_clock, sleep: Sleep = real_sleep
) -> list[str]:
    """Price what the wallet's account holds; the prices' flags, each once. Asks Binance
    nothing while nothing has been held on a closed day. Raises one of `PRICE_FAILURES` when
    Binance fails; a CoinGecko failure is logged and leaves Binance's prices as they are."""

    async def held(session: AsyncSession, connection_id: UUID) -> dict[str, Held]:
        return await held_on(session, [account_id])

    async with ctx.session_factory() as session:
        holdings = await held(session, ctx.connection.id)
    if not any(one.first_day <= _yesterday(clock()) for one in holdings.values()):
        return []

    client = BinanceClient.public(ctx.http, clock=clock, sleep=sleep)
    # Binance's clock decides the latest closed day: the bar of the day still open is never kept.
    await client.sync_clock()
    listed = await listed_pairs(client)

    async def pairs(session: AsyncSession, connection_id: UUID) -> dict[str, tuple[str, str]]:
        return listed

    flags: list[str] = []

    async def flag(_: SyncContext, raised: Sequence[str]) -> None:
        flags.extend(reason for reason in raised if reason not in flags)

    await update_prices(ctx, client, held=held, pairs=pairs, flag=flag, sleep=sleep)
    yesterday = _yesterday(client.now_ms())
    eligible = {code: one for code, one in holdings.items() if one.first_day <= yesterday}
    await flag(ctx, [flag_for(code) for code in await _gaps(ctx, eligible, yesterday)])
    return flags
