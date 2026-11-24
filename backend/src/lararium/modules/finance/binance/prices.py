"""Prices of the assets a Binance connection holds (design 5.10, plan Decision 13).

An asset's price on a day is its daily close in EUR, worked out from Binance's own daily bars
(`binance_klines`): its EUR pair, else through another quote asset that has a EUR pair. It is
recorded in EUR from the asset's first held day to yesterday (UTC), source `binance`. Only
closed days are priced: the bar of the day still open is never fetched or stored.

CoinGecko is optional (`Settings.coingecko_api_key`) and has a small client of its own: Binance's
client can send nothing outside the catalogue. It names each asset's CoinGecko id from the
Binance tickers, never from a symbol, and prices an asset Binance has no price for, on the days
it has no `binance` price (source `coingecko`).

Nothing here logs or reports a price, an amount or a key.
"""

import logging
import re
from collections import deque
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from decimal import Context, Decimal, localcontext
from uuid import UUID

import httpx
from sqlalchemy import bindparam, func, select, text, union_all
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from lararium import __version__
from lararium.core.commands import command_context
from lararium.core.connections import get_connection, lock_connection, update_config
from lararium.core.connectors import SyncContext
from lararium.core.errors import DisplayableError
from lararium.core.json_types import JSONValue
from lararium.core.secrets import Secret
from lararium.core.settings_store import get_setting
from lararium.modules.finance import models
from lararium.modules.finance.binance import layout
from lararium.modules.finance.binance.catalogue import ENDPOINTS
from lararium.modules.finance.binance.client import (
    MAX_RETRIES,
    WINDOW_MS,
    BinanceClient,
    BinanceNotPresent,
    Clock,
    Sleep,
    load_json,
    real_sleep,
    retry_after,
    wall_clock,
)
from lararium.modules.finance.binance.fetch import DAY_MS
from lararium.modules.finance.binance.instruments import clash_flag
from lararium.modules.finance.binance.models import CoingeckoId
from lararium.modules.finance.binance.normalize import FLAGS_KEY, Run, publish_flags
from lararium.modules.finance.binance.parse import ApiValue, parse, unknown_flags
from lararium.modules.finance.binance.reconcile import wallets
from lararium.modules.finance.binance.tables import millis, moment, records, store, time_value
from lararium.modules.finance.errors import FinanceValidationError
from lararium.modules.finance.instruments import (
    REPORTING_CURRENCY_KEY,
    InstrumentIdentifier,
    add_identifiers,
    find_by_identifier,
    get_currency,
)
from lararium.modules.finance.pricing import record_price

logger = logging.getLogger(__name__)

EUR = "EUR"
EUR_QUOTES = ("USDT", "USDC", "FDUSD", "BTC", "ETH", "BNB")
SOURCE = "binance"
COINGECKO_SOURCE = "coingecko"
SIGNIFICANT_DIGITS = 12
# Enough that a quotient and a product lose nothing before the rounding to 12 digits.
_WORKING_PRECISION = 60
# One request asks for at most this many bars; the catalogue's span is a day less than it.
MAX_BARS = 1_000

BASE_URL = "https://api.coingecko.com/api/v3"
KEY_HEADER = "x-cg-demo-api-key"
CALLS_PER_MINUTE = 25
# The free plan's history: a range may not start earlier than this many days before now.
HISTORY_DAYS = 365
# CoinGecko lists about two thousand Binance tickers, a hundred a page.
MAX_TICKER_PAGES = 100
# The map of Binance assets to CoinGecko ids is read again this long after it was read, or after
# an attempt that failed or found nothing.
REFRESH_IDS_AFTER = timedelta(days=7)
RETRY_IDS_AFTER = timedelta(hours=24)
# config: the last attempt to read the map, {"at_ms": epoch ms, "ok": whether it found ids}.
IDS_ASKED_KEY = "coingecko_ids_asked"
# config: per symbol, the first and last day already asked for (a range with no bar is asked once).
ASKED_KEY = "bars_asked"
_ID = re.compile(r"[a-z0-9][a-z0-9._-]{0,127}")


def flag_for(asset: str) -> str:
    return f"no price for {asset}"


@dataclass
class PriceReport:
    # The prices written in this run (an unchanged price is not written).
    priced: int
    # The held assets that have no price from any source.
    unpriced: list[str] = field(default_factory=list[str])


@dataclass(frozen=True)
class Held:
    instrument_id: UUID
    first_day: date


# The path of an asset to EUR.


def _round(value: Decimal) -> Decimal:
    return Context(prec=SIGNIFICANT_DIGITS).create_decimal(value).normalize()


def _close(closes: Mapping[tuple[str, date], Decimal], symbol: str, day: date) -> Decimal | None:
    value = closes.get((symbol, day))
    return value if value is not None and value > 0 else None


def _rate(closes: Mapping[tuple[str, date], Decimal], a: str, b: str, day: date) -> Decimal | None:
    """What one `a` costs in `b` on the day: the a/b pair's close, else the b/a pair's, inverted."""
    direct = _close(closes, a + b, day)
    if direct is not None:
        return direct
    inverse = _close(closes, b + a, day)
    return None if inverse is None else Decimal(1) / inverse


def eur_path(asset: str, day: date, closes: Mapping[tuple[str, date], Decimal]) -> Decimal | None:
    """The EUR price of an asset on a day, from the closes of the pairs `closes` holds (by symbol
    and open day), or None when no path has a bar on that day.

    In order: the asset's EUR pair; else for each quote in `EUR_QUOTES`, the asset's pair with it
    (either way round) times the quote's own EUR price, which is only its EUR pair (one level
    deep). The first path with a bar on the day wins. Rounded to 12 significant digits.
    """
    if asset == EUR:
        return None
    with localcontext(prec=_WORKING_PRECISION):
        price = _rate(closes, asset, EUR, day)
        if price is None:
            for quote in EUR_QUOTES:
                if quote == asset:
                    continue
                step = _rate(closes, asset, quote, day)
                own = _rate(closes, quote, EUR, day) if step is not None else None
                if step is not None and own is not None:
                    price = step * own
                    break
        return None if price is None else _round(price)


# The CoinGecko client.


class CoinGeckoError(DisplayableError):
    """CoinGecko did not answer well. Never carries the key, a URL or a body."""


def _failed(status: int | None) -> CoinGeckoError:
    shown = "no response" if status is None else f"HTTP {status}"
    return CoinGeckoError(f"CoinGecko request failed ({shown})")


class CoinGeckoClient:
    """The few CoinGecko requests the prices use, at a fixed https base, no redirects, at most
    25 calls a minute. The key is sent in a header only."""

    def __init__(
        self,
        http: httpx.AsyncClient,
        key: Secret,
        *,
        clock: Clock = wall_clock,
        sleep: Sleep = real_sleep,
    ) -> None:
        self._http = http
        self._key = key
        self._clock = clock
        self._sleep = sleep
        self._sent: deque[int] = deque()

    def __repr__(self) -> str:
        return "CoinGeckoClient()"

    async def _wait(self) -> None:
        while True:
            now = self._clock()
            while self._sent and now - self._sent[0] >= WINDOW_MS:
                self._sent.popleft()
            if len(self._sent) < CALLS_PER_MINUTE:
                self._sent.append(now)
                return
            await self._sleep(self._sent[0] + WINDOW_MS - now)

    async def _get(self, path: str, params: Mapping[str, str | int] | None = None) -> ApiValue:
        headers = {KEY_HEADER: self._key.reveal(), "User-Agent": f"Lararium/{__version__}"}
        retries = 0
        while True:
            await self._wait()
            try:
                response = await self._http.get(
                    f"{BASE_URL}/{path}", params=params, headers=headers, follow_redirects=False
                )
            except httpx.HTTPError:
                # httpx's messages can quote the URL.
                raise _failed(None) from None
            status = response.status_code
            if status == 429 and retries < MAX_RETRIES:
                retries += 1
                await self._sleep(retry_after(response) * 1000)
                continue
            if not 200 <= status < 300:
                raise _failed(status)
            try:
                return load_json(response.text)
            except ValueError:
                raise _failed(status) from None

    async def tickers(self, page: int) -> ApiValue:
        """A page of Binance's tickers: each names its base and quote asset and their ids."""
        return await self._get("exchanges/binance/tickers", {"page": page})

    async def supported_currencies(self) -> ApiValue:
        return await self._get("simple/supported_vs_currencies")

    async def market_chart(
        self, coin_id: str, vs_currency: str, from_seconds: int, to_seconds: int
    ) -> ApiValue:
        if not _ID.fullmatch(coin_id):
            raise CoinGeckoError("CoinGecko gave an id that cannot be used")
        return await self._get(
            f"coins/{coin_id}/market_chart/range",
            {"vs_currency": vs_currency, "from": from_seconds, "to": to_seconds},
        )


# Days.


def _ms(day: date) -> int:
    return millis(datetime.combine(day, time(), UTC))


def _day(ms: int) -> date:
    return moment(ms).date()


def _yesterday(now_ms: int) -> date:
    return _day(now_ms) - timedelta(days=1)


# What is held.


async def _held(session: AsyncSession, connection_id: UUID) -> dict[str, Held]:
    """The crypto assets the connection's accounts have held, with the day of the earliest
    posting or snapshot of each (a void posting is not one)."""
    accounts = list(await wallets(session, connection_id))
    if not accounts:
        return {}
    p, t, s = models.Posting, models.Transaction, models.BalanceSnapshot
    posted = (
        select(p.instrument_id, func.min(t.booking_date).label("day"))
        .join(t, t.id == p.transaction_id)
        .where(p.account_id.in_(accounts), t.status != "void")
        .group_by(p.instrument_id)
    )
    snapped = (
        select(s.instrument_id, func.min(s.as_of_date).label("day"))
        .where(s.account_id.in_(accounts))
        .group_by(s.instrument_id)
    )
    both = union_all(posted, snapped).subquery()
    i, n = models.InstrumentIdentifier, models.Instrument
    rows = await session.execute(
        select(i.value, both.c.instrument_id, func.min(both.c.day))
        .join(i, i.instrument_id == both.c.instrument_id)
        .join(n, n.id == both.c.instrument_id)
        .where(i.scheme == "binance_asset", n.kind == "crypto")
        .group_by(i.value, both.c.instrument_id)
    )
    return {code: Held(instrument_id, day) for code, instrument_id, day in rows}


async def _pairs(session: AsyncSession, connection_id: UUID) -> dict[str, tuple[str, str]]:
    """Symbol to (base, quote) of every pair the newest `exchangeInfo` lists, whatever its status:
    a halted pair still has its history."""
    rows = await session.execute(
        text(
            "select symbol, base_asset, quote_asset"
            " from finance.binance_exchange_info_symbols"
            " where connection_id = :c and snapshot_at = (select max(snapshot_at)"
            "  from finance.binance_exchange_info where connection_id = :c)"
        ),
        {"c": connection_id},
    )
    return {row.symbol: (row.base_asset, row.quote_asset) for row in rows}


def _needs(held: Mapping[str, Held], pairs: Mapping[str, tuple[str, str]]) -> dict[str, date]:
    """The symbols whose bars the paths of the held assets use, with the first day each is
    needed from."""
    needs: dict[str, date] = {}

    def want(base: str, quote: str, day: date) -> bool:
        symbol = base + quote
        if pairs.get(symbol) != (base, quote):
            return False
        needs[symbol] = min(day, needs.get(symbol, day))
        return True

    for code, one in held.items():
        day = one.first_day
        want(code, EUR, day)
        want(EUR, code, day)
        for quote in EUR_QUOTES:
            if quote == code:
                continue
            # The two orders are both asked for: `want` first, so neither is skipped.
            listed = [want(code, quote, day), want(quote, code, day)]
            if any(listed):
                want(quote, EUR, day)
                want(EUR, quote, day)
    return needs


# Bars.


async def _stored_span(
    session: AsyncSession, symbols: Sequence[str]
) -> dict[str, tuple[date, date]]:
    if not symbols:
        return {}
    rows = await session.execute(
        text(
            "select symbol, min(open_time) as low, max(open_time) as high"
            " from finance.binance_klines where symbol in :s group by symbol"
        ).bindparams(bindparam("s", expanding=True)),
        {"s": list(symbols)},
    )
    return {
        row.symbol: (row.low.astimezone(UTC).date(), row.high.astimezone(UTC).date())
        for row in rows
    }


def _asked(config: Mapping[str, JSONValue]) -> dict[str, tuple[date, date]]:
    raw = config.get(ASKED_KEY)
    found: dict[str, tuple[date, date]] = {}
    for symbol, span in (raw if isinstance(raw, dict) else {}).items():
        if isinstance(span, list) and len(span) == 2:
            try:
                found[symbol] = (date.fromisoformat(str(span[0])), date.fromisoformat(str(span[1])))
            except ValueError:
                continue
    return found


def _joined(
    stored: tuple[date, date] | None, asked: tuple[date, date] | None
) -> tuple[date, date] | None:
    """The days a symbol is known for: stored bars and days already asked for. Both are built
    outwards from one another, so together they are one run of days."""
    spans = [span for span in (stored, asked) if span is not None]
    if not spans:
        return None
    return min(low for low, _ in spans), max(high for _, high in spans)


async def _mark_asked(ctx: SyncContext, symbol: str, first: date, last: date) -> None:
    args = {"connection_id": str(ctx.connection.id), "symbol": symbol}
    async with (
        ctx.session_factory() as session,
        command_context(session, ctx.command.actor, "binance.bars_asked", args) as cmd,
    ):
        config = (await lock_connection(cmd, ctx.connection.id)).config
        asked = _asked(config)
        low, high = asked.get(symbol, (first, last))
        asked[symbol] = (min(low, first), max(high, last))
        value: dict[str, JSONValue] = {
            name: [span[0].isoformat(), span[1].isoformat()] for name, span in sorted(asked.items())
        }
        await update_config(cmd, ctx.connection.id, {ASKED_KEY: value})


def _to_fetch(
    need_from: date, last: date, stored: tuple[date, date] | None
) -> list[tuple[date, date]]:
    """The day ranges of a symbol that are not stored yet: before what is stored and after it."""
    if stored is None:
        return [(need_from, last)] if need_from <= last else []
    low, high = stored
    found: list[tuple[date, date]] = []
    if need_from < low:
        found.append((need_from, low - timedelta(days=1)))
    if high < last:
        found.append((high + timedelta(days=1), last))
    return found


async def _fetch_bars(
    ctx: SyncContext, client: BinanceClient, symbol: str, start: date, end: date
) -> None:
    """The closed days from `start` to `end` of a symbol, at most 1,000 bars a request, each
    request's bars stored in a short transaction of their own."""
    endpoint = ENDPOINTS[layout.BARS_LABEL]
    first = start
    while first <= end:
        last = min(end, first + timedelta(days=MAX_BARS - 1))
        try:
            body = await client.call(
                endpoint,
                {
                    "symbol": symbol,
                    "interval": "1d",
                    "startTime": _ms(first),
                    "endTime": _ms(last) + DAY_MS - 1,
                    "limit": MAX_BARS,
                },
            )
        except BinanceNotPresent:
            return
        parsed = parse(layout.BARS_LABEL, body)
        await _flag(ctx, unknown_flags(layout.BARS_LABEL, parsed))
        bars = [
            row
            for row in records(layout.BARS_LABEL, parsed)
            # The day still open is never kept, whatever was asked for.
            if first <= time_value(layout.BARS_LABEL, "[][0]", row["0"]).date() <= last
        ]
        if bars:
            args = {"connection_id": str(ctx.connection.id), "symbol": symbol}
            async with (
                ctx.session_factory() as session,
                command_context(session, ctx.command.actor, "binance.klines", args) as cmd,
            ):
                for row in bars:
                    await store(cmd, layout.BARS_LABEL, {"symbol": symbol}, row)
        await _mark_asked(ctx, symbol, first, last)
        first = last + timedelta(days=1)


async def _closes(session: AsyncSession, symbols: Sequence[str]) -> dict[tuple[str, date], Decimal]:
    if not symbols:
        return {}
    rows = await session.execute(
        text(
            "select symbol, open_time, close from finance.binance_klines where symbol in :s"
        ).bindparams(bindparam("s", expanding=True)),
        {"s": list(symbols)},
    )
    return {(row.symbol, row.open_time.astimezone(UTC).date()): row.close for row in rows}


# Prices.


async def _record(
    ctx: SyncContext,
    code: str,
    instrument_id: UUID,
    quote_id: UUID,
    wanted: Mapping[date, Decimal],
    source: str,
) -> int:
    """Record the prices that are not stored yet (or differ); how many were written. A run that
    has nothing to write opens no transaction."""
    async with ctx.session_factory() as session:
        p = models.Price
        stored = {
            row.as_of_date: row.price
            for row in await session.execute(
                select(p.as_of_date, p.price).where(
                    p.instrument_id == instrument_id,
                    p.quote_instrument_id == quote_id,
                    p.source == source,
                )
            )
        }
    changed = {day: price for day, price in wanted.items() if stored.get(day) != price}
    if not changed:
        return 0
    written = 0
    args = {"connection_id": str(ctx.connection.id), "asset": code, "source": source}
    async with (
        ctx.session_factory() as session,
        command_context(session, ctx.command.actor, "binance.prices", args) as cmd,
    ):
        for day in sorted(changed):
            try:
                await record_price(
                    cmd,
                    instrument_id=instrument_id,
                    quote_instrument_id=quote_id,
                    price=changed[day],
                    as_of_date=day,
                    source=source,
                )
            except FinanceValidationError:
                continue  # a price too small or large to keep
            written += 1
    return written


async def _priced(session: AsyncSession, instruments: Sequence[UUID], day: date) -> set[UUID]:
    """The instruments with a price, from any source, dated `day`."""
    if not instruments:
        return set()
    found = await session.scalars(
        select(models.Price.instrument_id)
        .where(models.Price.instrument_id.in_(list(instruments)), models.Price.as_of_date == day)
        .distinct()
    )
    return set(found)


async def _eligible(ctx: SyncContext, yesterday: date) -> dict[str, Held]:
    """The held assets with a closed day to price: held on or before yesterday."""
    async with ctx.session_factory() as session:
        held = await _held(session, ctx.connection.id)
    return {code: one for code, one in held.items() if one.first_day <= yesterday}


async def _unpriced(ctx: SyncContext, held: Mapping[str, Held], day: date) -> list[str]:
    """The held assets with no price from any source on `day`, the latest closed day."""
    async with ctx.session_factory() as session:
        priced = await _priced(session, [one.instrument_id for one in held.values()], day)
    return sorted(code for code, one in held.items() if one.instrument_id not in priced)


async def price_assets(ctx: SyncContext, client: BinanceClient) -> PriceReport:
    """Fetch the bars the held assets' paths need (only the closed days not stored yet) and
    record each asset's EUR price from its first held day to yesterday; a day no path has a bar
    for is left without one. `unpriced` is every held asset with no price on yesterday."""
    yesterday = _yesterday(client.now_ms())
    held = await _eligible(ctx, yesterday)
    if not held:
        return PriceReport(priced=0)
    async with ctx.session_factory() as session:
        pairs = await _pairs(session, ctx.connection.id)
        eur = (await get_currency(session, EUR)).id
    needs = _needs(held, pairs)
    async with ctx.session_factory() as session:
        spans = await _stored_span(session, list(needs))
        asked = _asked((await get_connection(session, ctx.connection.id)).config)
    for symbol in sorted(needs):
        known = _joined(spans.get(symbol), asked.get(symbol))
        for start, end in _to_fetch(needs[symbol], yesterday, known):
            await _fetch_bars(ctx, client, symbol, start, end)
    async with ctx.session_factory() as session:
        closes = await _closes(session, list(needs))
    priced = 0
    for code, one in sorted(held.items()):
        wanted: dict[date, Decimal] = {}
        day = one.first_day
        while day <= yesterday:
            if (price := eur_path(code, day, closes)) is not None:
                wanted[day] = price
            day += timedelta(days=1)
        priced += await _record(ctx, code, one.instrument_id, eur, wanted, SOURCE)
    return PriceReport(priced=priced, unpriced=await _unpriced(ctx, held, yesterday))


# CoinGecko.


async def _id_map(client: CoinGeckoClient) -> dict[str, str | None]:
    """The CoinGecko id of every Binance asset the tickers list (base and target side); an asset
    two ids claim has none. Never worked out from a symbol."""
    found: dict[str, str] = {}
    claimed_twice: set[str] = set()
    for page in range(1, MAX_TICKER_PAGES + 1):
        body = await client.tickers(page)
        tickers = body.get("tickers") if isinstance(body, dict) else None
        if not isinstance(tickers, list) or not tickers:
            break
        for ticker in tickers:
            if not isinstance(ticker, dict):
                continue
            for asset, coin in (
                (ticker.get("base"), ticker.get("coin_id")),
                (ticker.get("target"), ticker.get("target_coin_id")),
            ):
                if not (isinstance(asset, str) and isinstance(coin, str)):
                    continue
                if not _ID.fullmatch(coin):
                    continue
                if found.setdefault(asset.upper(), coin) != coin:
                    claimed_twice.add(asset.upper())
    return {code: None if code in claimed_twice else coin for code, coin in found.items()}


async def refresh_coingecko_ids(
    ctx: SyncContext,
    *,
    client: CoinGeckoClient | None = None,
    clock: Clock = wall_clock,
    sleep: Sleep = real_sleep,
) -> None:
    """With a CoinGecko key: read Binance's tickers into `binance_coingecko_ids`, for every asset
    they list, 7 days after the last read found ids (`config["coingecko_ids_asked"]`), or 24
    hours after one that failed or found nothing. Each asset whose id is new or changed is
    upserted; an asset the tickers no longer list keeps its row, and an identical map writes no
    row. Raises `CoinGeckoError` when CoinGecko fails; the table is then left as it was."""
    key = ctx.coingecko_api_key
    if key is None:
        return
    now_ms = clock()
    async with ctx.session_factory() as session:
        asked = (await get_connection(session, ctx.connection.id)).config.get(IDS_ASKED_KEY)
        known = {
            row.asset: row.coingecko_id
            for row in await session.execute(select(CoingeckoId.asset, CoingeckoId.coingecko_id))
        }
    if not _ids_due(asked, now_ms):
        return
    try:
        found = await _id_map(client or CoinGeckoClient(ctx.http, key, clock=clock, sleep=sleep))
    except CoinGeckoError:
        await _ids_asked(ctx, now_ms, {})
        raise
    # An asset two ids claim is mapped to None (`_id_map`), which may clear an id it had.
    changed = {
        code: coin for code, coin in found.items() if code not in known or known[code] != coin
    }
    await _ids_asked(ctx, now_ms, changed, ok=bool(found))


def _ids_due(asked: JSONValue, now_ms: int) -> bool:
    if not isinstance(asked, dict):
        return True
    at, ok = asked.get("at_ms"), asked.get("ok")
    if not isinstance(at, int) or isinstance(at, bool):
        return True
    wait = REFRESH_IDS_AFTER if ok is True else RETRY_IDS_AFTER
    return moment(now_ms) - moment(at) >= wait


async def _ids_asked(
    ctx: SyncContext, now_ms: int, changed: Mapping[str, str | None], *, ok: bool = False
) -> None:
    """Upsert the changed ids and remember the attempt, in one transaction."""
    name = "binance.coingecko_ids" if changed else "binance.coingecko_ids_asked"
    args = {"connection_id": str(ctx.connection.id)}
    async with (
        ctx.session_factory() as session,
        command_context(session, ctx.command.actor, name, args) as cmd,
    ):
        if changed:
            rows = [
                {"asset": code, "coingecko_id": coin, "fetched_at": moment(now_ms)}
                for code, coin in sorted(changed.items())
            ]
            upsert = insert(CoingeckoId).values(rows)
            await cmd.session.execute(
                upsert.on_conflict_do_update(
                    index_elements=[CoingeckoId.asset],
                    set_={
                        "coingecko_id": upsert.excluded.coingecko_id,
                        "fetched_at": upsert.excluded.fetched_at,
                    },
                )
            )
        marker: dict[str, JSONValue] = {"at_ms": now_ms, "ok": ok}
        await update_config(cmd, ctx.connection.id, {IDS_ASKED_KEY: marker})


async def refresh_ids_quietly(
    ctx: SyncContext, *, clock: Clock = wall_clock, sleep: Sleep = real_sleep
) -> None:
    """`refresh_coingecko_ids` for the start of a sync: a CoinGecko failure is logged (its
    status only) and the sync goes on."""
    try:
        await refresh_coingecko_ids(ctx, clock=clock, sleep=sleep)
    except CoinGeckoError as error:
        logger.warning("CoinGecko ids not refreshed: %s", error)


async def _tag_instruments(
    ctx: SyncContext, codes: Sequence[str], held: Mapping[str, Held]
) -> None:
    """Give each held asset with a stored id that id as its instrument's `coingecko` identifier
    (an instrument that has one keeps it); an id another instrument holds is flagged, not moved.
    Opens a transaction only when one is missing."""
    async with ctx.session_factory() as session:
        ids = {
            row.asset: row.coingecko_id
            for row in await session.execute(
                select(CoingeckoId.asset, CoingeckoId.coingecko_id).where(
                    CoingeckoId.asset.in_(list(codes)), CoingeckoId.coingecko_id.is_not(None)
                )
            )
        }
        tagged = set(
            await session.scalars(
                select(models.InstrumentIdentifier.instrument_id).where(
                    models.InstrumentIdentifier.scheme == "coingecko"
                )
            )
        )
        missing: dict[str, str] = {}
        clashes: list[str] = []
        for code, coin in ids.items():
            if held[code].instrument_id in tagged or not coin:
                continue
            owner = await find_by_identifier(session, "coingecko", coin)
            if owner is None:
                missing[code] = coin
            elif owner.id != held[code].instrument_id:
                clashes.append(code)
    # Another instrument holds the id: both stay as they are, and the owner is told.
    await _flag(ctx, [clash_flag(code) for code in sorted(clashes)])
    if not missing:
        return
    args = {"connection_id": str(ctx.connection.id)}
    async with (
        ctx.session_factory() as session,
        command_context(session, ctx.command.actor, "binance.coingecko_identifiers", args) as cmd,
    ):
        for code, coin in sorted(missing.items()):
            await add_identifiers(
                cmd, held[code].instrument_id, [InstrumentIdentifier("coingecko", coin)]
            )


async def _vs_currency(client: CoinGeckoClient, code: str) -> str:
    """The reporting currency when CoinGecko supports it, else USD."""
    listed = await client.supported_currencies()
    supported: set[str] = set()
    if isinstance(listed, list):
        supported = {item for item in listed if isinstance(item, str)}
    return code.lower() if code.lower() in supported else "usd"


async def _chart_prices(
    client: CoinGeckoClient, coin: str, vs: str, first: date, now_ms: int
) -> dict[date, Decimal]:
    """The price of each day D from the chart's point at 00:00 UTC of D + 1 (the first point
    within an hour after midnight)."""
    body = await client.market_chart(
        coin, vs, _ms(first + timedelta(days=1)) // 1000, now_ms // 1000
    )
    points = body.get("prices") if isinstance(body, dict) else None
    best: dict[date, tuple[int, Decimal]] = {}
    for point in points if isinstance(points, list) else []:
        if not (isinstance(point, list) and len(point) == 2):
            continue
        when, value = point
        if isinstance(when, bool) or not isinstance(when, int | Decimal):
            continue
        if isinstance(value, bool) or not isinstance(value, int | Decimal) or value <= 0:
            continue
        past_midnight = int(when) % DAY_MS
        if past_midnight >= 3_600_000:
            continue
        day = _day(int(when)) - timedelta(days=1)
        if day not in best or past_midnight < best[day][0]:
            best[day] = (past_midnight, Decimal(value))
    return {day: _round(value) for day, (_, value) in best.items()}


async def coingecko_prices(
    ctx: SyncContext,
    assets: Sequence[str],
    *,
    id_only: Sequence[str] = (),
    clock: Clock = wall_clock,
    sleep: Sleep = real_sleep,
) -> int:
    """With a CoinGecko key set (`SyncContext.coingecko_api_key`): the id of each of `assets` and
    `id_only` (from Binance's tickers, stored as the instrument's `coingecko` identifier), then
    the prices of `assets` for the days within the last 365 that have no `binance` or
    `coingecko` price (an asset whose newest `binance` price is yesterday is skipped). A no-op
    without a key. Returns the prices written."""
    key = ctx.coingecko_api_key
    if key is None:
        return 0
    client = CoinGeckoClient(ctx.http, key, clock=clock, sleep=sleep)
    now_ms = clock()
    yesterday = _yesterday(now_ms)
    async with ctx.session_factory() as session:
        held = await _held(session, ctx.connection.id)
    wanted = sorted({*assets, *id_only} & held.keys())
    if not wanted:
        return 0
    await refresh_coingecko_ids(ctx, client=client, clock=clock, sleep=sleep)
    await _tag_instruments(ctx, wanted, held)

    written = 0
    vs: str | None = None
    earliest = _day(now_ms) - timedelta(days=HISTORY_DAYS - 1)
    for code in sorted(set(assets) & held.keys()):
        one = held[code]
        async with ctx.session_factory() as session:
            coin = await session.scalar(
                select(CoingeckoId.coingecko_id).where(CoingeckoId.asset == code)
            )
            newest = await session.scalar(
                select(func.max(models.Price.as_of_date)).where(
                    models.Price.instrument_id == one.instrument_id,
                    models.Price.source == SOURCE,
                )
            )
            if newest == yesterday:
                continue  # Binance prices it up to the latest closed day
            covered = set(
                await session.scalars(
                    select(models.Price.as_of_date).where(
                        models.Price.instrument_id == one.instrument_id,
                        models.Price.source.in_([SOURCE, COINGECKO_SOURCE]),
                    )
                )
            )
        first = max(one.first_day, earliest)
        missing = [
            first + timedelta(days=n)
            for n in range((yesterday - first).days + 1)
            if first + timedelta(days=n) not in covered
        ]
        if coin is None or not missing:
            continue
        async with ctx.session_factory() as session:
            reporting = await get_setting(session, REPORTING_CURRENCY_KEY)
        if vs is None:
            vs = await _vs_currency(client, str(reporting))
        async with ctx.session_factory() as session:
            quote = (await get_currency(session, vs.upper())).id
        chart = await _chart_prices(client, coin, vs, missing[0], now_ms)
        wanted_prices = {day: price for day, price in chart.items() if day in missing}
        written += await _record(
            ctx, code, one.instrument_id, quote, wanted_prices, COINGECKO_SOURCE
        )
    return written


# The sync's step.


async def _flag(ctx: SyncContext, flags: Sequence[str]) -> None:
    """Name each flag once: the flags are kept in the connection's config, shown in its warnings
    and queued for the sync's one message (`publish_flags`). A flag stays once raised, also
    when its cause is gone. A run that has none new writes nothing."""
    if not flags:
        return
    async with ctx.session_factory() as session:
        connection = await get_connection(session, ctx.connection.id)
    known = connection.config.get(FLAGS_KEY)
    if isinstance(known, list) and all(flag in known for flag in flags):
        return
    args = {"connection_id": str(ctx.connection.id)}
    async with (
        ctx.session_factory() as session,
        command_context(session, ctx.command.actor, "binance.flags", args) as cmd,
    ):
        run = await Run.start(ctx, cmd, await get_connection(session, ctx.connection.id))
        for flag in flags:
            run.flag(flag)
        await publish_flags(run)


async def update_prices(
    ctx: SyncContext, client: BinanceClient, *, sleep: Sleep = real_sleep
) -> PriceReport:
    """Binance's prices, then (with a key) CoinGecko's for what Binance has none for, then a
    one-time flag for each asset still without a price. A CoinGecko failure leaves the Binance
    prices as they are and the sync going."""
    report = await price_assets(ctx, client)
    unpriced = report.unpriced
    if ctx.coingecko_api_key is not None:
        yesterday = _yesterday(client.now_ms())
        held = await _eligible(ctx, yesterday)
        try:
            # Every held asset: the days Binance has no price for (a halted pair, an asset with
            # no market) are filled, and each asset gets its id.
            report.priced += await coingecko_prices(
                ctx, list(held), clock=client.now_ms, sleep=sleep
            )
        except CoinGeckoError as error:
            # The message holds a status only, never the key or a URL.
            logger.warning("CoinGecko prices failed: %s", error)
        unpriced = await _unpriced(ctx, held, yesterday)
    await _flag(ctx, [flag_for(code) for code in unpriced])
    return PriceReport(priced=report.priced, unpriced=unpriced)
