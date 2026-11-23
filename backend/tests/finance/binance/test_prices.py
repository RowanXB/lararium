"""Prices from Binance's daily bars through its EUR market, and CoinGecko for what it cannot
price (design 5.10, plan Decision 13)."""

import itertools
import json
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy import text

from lararium.core.connections import get_connection
from lararium.core.connectors import SyncContext
from lararium.core.secrets import Secret
from lararium.modules.finance.binance import instruments, notices
from lararium.modules.finance.binance.prices import (
    CoinGeckoClient,
    CoinGeckoError,
    PriceReport,
    coingecko_prices,
    eur_path,
    price_assets,
    update_prices,
)
from lararium.modules.finance.instruments import (
    InstrumentIdentifier,
    create_instrument,
    find_by_identifier,
    get_currency,
)
from lararium.modules.finance.pricing import record_fx_rate
from lararium.modules.finance.valuation import holdings_asof

from .fixtures import DAY_MS, NOW_MS, FakeBinance, build_row
from .normalizing import ingest, list_pairs, run_normalize
from .syncing import (
    SETTINGS,
    Clock,
    binance_connection,
    client_for,
    context,
    later_config,
    run_binance_sync,
)

KEY = "cg-TEST-do-not-log-8901"
FIRST, LAST = date(2026, 11, 18), date(2026, 11, 22)  # the day Clock reads is 2026-11-23
DAYS = [FIRST + timedelta(days=n) for n in range(5)]


def _ms(day: date) -> int:
    return int(datetime(day.year, day.month, day.day, tzinfo=UTC).timestamp()) * 1000


def _utc(day: date, hour: int = 10) -> datetime:
    return datetime(day.year, day.month, day.day, hour, tzinfo=UTC)


def _bar(day: date, close: str | Decimal) -> dict[str, Any]:
    return build_row("klines", **{"0": _ms(day), "4": str(close), "6": _ms(day) + DAY_MS - 1})


def _closes(**by_symbol: Mapping[date, str]) -> dict[tuple[str, date], Decimal]:
    return {(s, d): Decimal(c) for s, days in by_symbol.items() for d, c in days.items()}


def _all(close: str, days: Sequence[date] = DAYS) -> dict[date, Decimal]:
    return dict.fromkeys(days, Decimal(close))


# The path of an asset to EUR (Decision 13), as a pure rule.

D = date(2026, 11, 20)


def test_direct_eur_pair() -> None:
    assert eur_path("AAA", D, _closes(AAAEUR={D: "2.5"})) == Decimal("2.5")


def test_the_eur_pair_is_inverted_when_eur_is_the_base() -> None:
    assert eur_path("AAA", D, _closes(EURAAA={D: "4"})) == Decimal("0.25")


def test_the_direct_pair_comes_before_the_inverted_one_and_before_any_quote() -> None:
    closes = _closes(AAAEUR={D: "2"}, EURAAA={D: "4"}, AAAUSDT={D: "9"}, EURUSDT={D: "1"})
    assert eur_path("AAA", D, closes) == Decimal("2")
    closes = _closes(EURAAA={D: "4"}, AAAUSDT={D: "9"}, EURUSDT={D: "1"})
    assert eur_path("AAA", D, closes) == Decimal("0.25")


def test_path_through_usdt() -> None:
    closes = _closes(AAAUSDT={D: "3"}, EURUSDT={D: "1.2"})
    assert eur_path("AAA", D, closes) == Decimal("2.5")


def test_path_through_another_quote() -> None:
    # AAA has only AAABTC; BTC has its own EUR market.
    closes = _closes(AAABTC={D: "0.5"}, BTCEUR={D: "40000"})
    assert eur_path("AAA", D, closes) == Decimal("20000")


def test_the_quote_may_be_the_base_of_the_pair() -> None:
    closes = _closes(BNBAAA={D: "4"}, BNBEUR={D: "500"})
    assert eur_path("AAA", D, closes) == Decimal("125")


def test_the_quotes_are_tried_in_order_and_need_their_own_eur_price() -> None:
    both = _closes(AAAUSDT={D: "3"}, AAABTC={D: "0.5"}, BTCEUR={D: "40000"}, EURUSDT={D: "1.2"})
    assert eur_path("AAA", D, both) == Decimal("2.5")  # USDT comes before BTC
    # USDT has no EUR market on the day: the next quote that can price it.
    no_usdt = _closes(AAAUSDT={D: "3"}, AAABTC={D: "0.5"}, BTCEUR={D: "40000"})
    assert eur_path("AAA", D, no_usdt) == Decimal("20000")


def test_a_quote_is_one_level_deep() -> None:
    # BTC itself is priced through USDT only: AAA through BTC does not follow that.
    closes = _closes(AAABTC={D: "0.5"}, BTCUSDT={D: "50000"}, EURUSDT={D: "1.25"})
    assert eur_path("AAA", D, closes) is None


def test_a_stablecoin_is_priced_by_its_market() -> None:
    closes = _closes(STBUSDT={D: "0.998"}, EURUSDT={D: "1.1"})
    assert eur_path("STB", D, closes) == Decimal("0.907272727273")  # not 1


def test_the_price_has_twelve_significant_digits() -> None:
    assert eur_path("AAA", D, _closes(EURAAA={D: "3"})) == Decimal("0.333333333333")
    assert eur_path("AAA", D, _closes(AAAEUR={D: "12345.67890123456"})) == Decimal("12345.6789012")


def test_a_day_without_a_bar_has_no_price_and_eur_has_none() -> None:
    assert eur_path("AAA", D, _closes(AAAEUR={D + timedelta(days=1): "2"})) is None
    assert eur_path("EUR", D, _closes(EURUSDT={D: "1.1"})) is None
    assert eur_path("AAA", D, {}) is None


def test_a_bar_that_closed_at_nothing_is_not_a_price() -> None:
    assert eur_path("AAA", D, _closes(AAAEUR={D: "0"})) is None


# A scenario: deposits make the holdings, exchange-info lists the pairs, the fake has the bars.


def _deposit(seq: int, day: date, amount: str, coin: str) -> dict[str, Any]:
    at = _ms(day) + 10 * 3_600_000
    return build_row(
        "deposits",
        seq,
        coin=coin,
        amount=amount,
        status=1,
        walletType=0,
        insertTime=at,
        completeTime=at,
    )


async def _scenario(
    session,
    run,
    fake: FakeBinance,
    *,
    holds: Mapping[str, date],
    pairs: Sequence[tuple[str, str, str]] = (),
    bars: Mapping[str, Mapping[date, str]] = {},
):
    connection = await binance_connection(run, config=later_config())
    rows = [_deposit(i, day, "5", coin) for i, (coin, day) in enumerate(holds.items())]
    await ingest(session, connection.id, "deposits", rows)
    await run_normalize(session, connection.id)
    await list_pairs(run, connection, *pairs)
    for symbol, closes in bars.items():
        fake.add("klines", *[_bar(d, c) for d, c in closes.items()], when={"symbol": symbol})
    return connection


def _route(
    fake: FakeBinance, gecko: "FakeGecko | None"
) -> Callable[[httpx.Request], httpx.Response]:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.coingecko.com":
            assert gecko is not None, "CoinGecko was called without a key"
            return gecko.handler(request)
        return fake.handler(request)

    return handler


@asynccontextmanager
async def _world(
    session,
    connection_id,
    fake: FakeBinance,
    clock: Clock,
    *,
    gecko: "FakeGecko | None" = None,
    key: str | None = None,
) -> AsyncIterator[tuple[SyncContext, Any]]:
    async with httpx.AsyncClient(transport=httpx.MockTransport(_route(fake, gecko))) as http:
        ctx = await context(session, connection_id, http)
        if key is not None:
            ctx = replace(ctx, coingecko_api_key=Secret(key))
        client = client_for(clock, http)
        await client.sync_clock()
        yield ctx, client


async def _prices(session, code: str, source: str = "binance") -> dict[date, Decimal]:
    rows = await session.execute(
        text(
            "select p.as_of_date, p.price from finance.prices p"
            " join finance.instrument_identifiers i on i.instrument_id = p.instrument_id"
            " where i.scheme = 'binance_asset' and i.value = :a and p.source = :s"
            " and p.quote_instrument_id = (select instrument_id from"
            "  finance.instrument_identifiers where scheme = 'iso4217' and value = :q)"
        ),
        {"a": code, "s": source, "q": "EUR" if source == "binance" else "GBP"},
    )
    return {r.as_of_date: r.price for r in rows}


async def _price_rows(session) -> list[Any]:
    rows = await session.execute(text("select id, recorded_at from finance.prices order by id"))
    return list(rows)


# Binance's bars


async def test_a_direct_pair_prices_every_day_from_the_first_held_day(
    session, run, fake, clock
) -> None:
    connection = await _scenario(
        session,
        run,
        fake,
        holds={"AAA": FIRST},
        pairs=[("AAAEUR", "AAA", "EUR")],
        bars={"AAAEUR": {d: str(2 + n) for n, d in enumerate(DAYS)}},
    )
    async with _world(session, connection.id, fake, clock) as (ctx, client):
        report = await price_assets(ctx, client)

    assert report == PriceReport(priced=5, unpriced=[])
    assert await _prices(session, "AAA") == {d: Decimal(2 + n) for n, d in enumerate(DAYS)}
    [aaa] = await session.execute(
        text("select source, quote_instrument_id from finance.prices limit 1")
    )
    assert aaa.source == "binance"
    assert aaa.quote_instrument_id == (await get_currency(session, "EUR")).id
    # The bars are kept typed, and only the closed days (up to yesterday).
    stored = await session.execute(
        text("select open_time, close, trades from finance.binance_klines order by open_time")
    )
    assert [r.open_time.astimezone(UTC).date() for r in stored] == DAYS


async def test_through_usdt(session, run, fake, clock) -> None:
    connection = await _scenario(
        session,
        run,
        fake,
        holds={"AAA": FIRST},
        pairs=[("AAAUSDT", "AAA", "USDT"), ("EURUSDT", "EUR", "USDT")],
        bars={"AAAUSDT": _all("3"), "EURUSDT": _all("1.2")},
    )
    async with _world(session, connection.id, fake, clock) as (ctx, client):
        await price_assets(ctx, client)

    assert await _prices(session, "AAA") == _all("2.5")


async def test_through_another_quote(session, run, fake, clock) -> None:
    connection = await _scenario(
        session,
        run,
        fake,
        holds={"AAA": FIRST},
        pairs=[("AAABTC", "AAA", "BTC"), ("BTCEUR", "BTC", "EUR")],
        bars={"AAABTC": _all("0.5"), "BTCEUR": _all("40000")},
    )
    async with _world(session, connection.id, fake, clock) as (ctx, client):
        await price_assets(ctx, client)

    assert await _prices(session, "AAA") == _all("20000")
    # Only the pairs the path needs were asked for.
    assert {s.params["symbol"] for s in fake.calls("klines")} == {"AAABTC", "BTCEUR"}


async def test_stablecoin_is_priced_by_its_market(session, run, fake, clock) -> None:
    connection = await _scenario(
        session,
        run,
        fake,
        holds={"STB": FIRST},
        pairs=[("STBUSDT", "STB", "USDT"), ("EURUSDT", "EUR", "USDT")],
        bars={"STBUSDT": _all("0.998"), "EURUSDT": _all("1.1")},
    )
    async with _world(session, connection.id, fake, clock) as (ctx, client):
        await price_assets(ctx, client)

    assert await _prices(session, "STB") == _all("0.907272727273")


async def test_halted_pair_leaves_the_day_unpriced(session, run, fake, clock, gbp, eur) -> None:
    connection = await _scenario(
        session,
        run,
        fake,
        holds={"AAA": FIRST},
        pairs=[("AAAEUR", "AAA", "EUR")],
        # Halted after the 19th: the pair keeps its history and has no later bar.
        bars={"AAAEUR": {DAYS[0]: "2", DAYS[1]: "3"}},
    )
    await run(
        record_fx_rate,
        base_instrument_id=eur.id,
        quote_instrument_id=gbp.id,
        rate=Decimal("0.8"),
        as_of_date=date(2026, 11, 1),
    )
    async with _world(session, connection.id, fake, clock) as (ctx, client):
        report = await price_assets(ctx, client)

    assert report == PriceReport(priced=2, unpriced=[])
    assert set(await _prices(session, "AAA")) == set(DAYS[:2])
    # Valuation on a day with no price uses the latest earlier one.
    [holding] = [h for h in await holdings_asof(session, LAST) if h.symbol == "AAA"]
    assert (holding.unit_price, holding.status) == (Decimal("3"), "ok")


async def test_prices_start_at_the_first_held_day(session, run, fake, clock) -> None:
    earlier = [FIRST - timedelta(days=n) for n in range(1, 6)]
    connection = await _scenario(
        session,
        run,
        fake,
        holds={"AAA": DAYS[2]},
        pairs=[("AAAEUR", "AAA", "EUR")],
        bars={"AAAEUR": {d: "2" for d in [*earlier, *DAYS]}},
    )
    async with _world(session, connection.id, fake, clock) as (ctx, client):
        await price_assets(ctx, client)

    assert set(await _prices(session, "AAA")) == set(DAYS[2:])
    [asked] = fake.calls("klines")
    assert asked.params["startTime"] == str(_ms(DAYS[2]))
    assert asked.params["interval"] == "1d" and asked.params["limit"] == "1000"
    first = await session.scalar(text("select min(open_time) from finance.binance_klines"))
    assert first.astimezone(UTC).date() == DAYS[2]


async def test_the_day_still_open_is_never_priced_or_stored(session, run, fake, clock) -> None:
    today = LAST + timedelta(days=1)
    connection = await _scenario(
        session,
        run,
        fake,
        holds={"AAA": FIRST},
        pairs=[("AAAEUR", "AAA", "EUR")],
        bars={"AAAEUR": {**_all("2"), today: "99"}},
    )
    async with _world(session, connection.id, fake, clock) as (ctx, client):
        await price_assets(ctx, client)

    assert set(await _prices(session, "AAA")) == set(DAYS)
    count = await session.scalar(text("select count(*) from finance.binance_klines"))
    assert count == 5


async def test_an_asset_held_only_today_waits_for_its_first_closed_day(
    session, run, fake, clock
) -> None:
    connection = await _scenario(
        session, run, fake, holds={"AAA": LAST + timedelta(days=1)}, pairs=[]
    )
    async with _world(session, connection.id, fake, clock) as (ctx, client):
        assert await price_assets(ctx, client) == PriceReport(priced=0, unpriced=[])
    assert fake.calls("klines") == []


async def test_more_than_a_thousand_days_come_in_several_requests(
    session, run, fake, clock
) -> None:
    start = date(2023, 11, 1)
    span = (LAST - start).days + 1
    assert span > 1_000
    connection = await _scenario(
        session,
        run,
        fake,
        holds={"AAA": start},
        pairs=[("AAAEUR", "AAA", "EUR")],
        bars={"AAAEUR": {start + timedelta(days=n): "2" for n in range(span)}},
    )
    async with _world(session, connection.id, fake, clock) as (ctx, client):
        report = await price_assets(ctx, client)

    assert report.priced == span
    assert len(fake.calls("klines")) == 2


async def test_later_runs_fetch_only_the_days_not_stored(session, run, fake, clock) -> None:
    connection = await _scenario(
        session,
        run,
        fake,
        holds={"AAA": FIRST},
        pairs=[("AAAEUR", "AAA", "EUR")],
        bars={"AAAEUR": {d: "2" for d in DAYS[:3]}},
    )
    async with _world(session, connection.id, fake, clock) as (ctx, client):
        await price_assets(ctx, client)
    fake.add("klines", *[_bar(d, "4") for d in DAYS[3:]], when={"symbol": "AAAEUR"})
    clock.ms += DAY_MS
    fake.requests.clear()

    async with _world(session, connection.id, fake, clock) as (ctx, client):
        report = await price_assets(ctx, client)

    [asked] = fake.calls("klines")
    assert asked.params["startTime"] == str(_ms(DAYS[3]))
    assert report.priced == 2
    assert await _prices(session, "AAA") == {**_all("2", DAYS[:3]), **_all("4", DAYS[3:])}


async def test_rerun_writes_nothing(session, run, fake, clock) -> None:
    connection = await _scenario(
        session,
        run,
        fake,
        holds={"AAA": FIRST},
        pairs=[("AAAEUR", "AAA", "EUR")],
        bars={"AAAEUR": _all("2")},
    )
    async with _world(session, connection.id, fake, clock) as (ctx, client):
        first = await update_prices(ctx, client)
    assert first.priced == 5
    rows = await _price_rows(session)
    audit = await session.scalar(text("select count(*) from core.audit_log"))
    events = await session.scalar(text("select count(*) from core.events"))
    fake.requests.clear()

    async with _world(session, connection.id, fake, clock) as (ctx, client):
        again = await update_prices(ctx, client)

    assert again.priced == 0
    assert await _price_rows(session) == rows
    assert await session.scalar(text("select count(*) from core.audit_log")) == audit
    assert await session.scalar(text("select count(*) from core.events")) == events
    # Nothing is stored for the closed days already held: no bar is asked for again.
    assert fake.calls("klines") == []


# What nothing can price


async def test_asset_with_no_market_and_no_key_is_flagged_once(
    session, run, fake, clock, gbp, eur
) -> None:
    connection = await _scenario(
        session, run, fake, holds={"NOC": FIRST}, pairs=[("AAAEUR", "AAA", "EUR")]
    )
    await run(
        record_fx_rate,
        base_instrument_id=eur.id,
        quote_instrument_id=gbp.id,
        rate=Decimal("0.8"),
        as_of_date=date(2026, 11, 1),
    )
    for _ in range(2):
        async with _world(session, connection.id, fake, clock) as (ctx, client):
            report = await update_prices(ctx, client)
        assert report == PriceReport(priced=0, unpriced=["NOC"])

    after = await get_connection(session, connection.id)
    assert after.config["flags"] == ["no price for NOC"]
    assert after.config[notices.PENDING_FLAGS_KEY] == ["no price for NOC"]
    assert notices.FLAG_PREFIX + "no price for NOC" in after.warnings
    told = notices.message("Binance", after.config)
    assert told is not None and told.count("NOC") == 1
    # No price, no amount, nothing but the code.
    assert "5" not in told.replace("Binance", "")
    [holding] = [h for h in await holdings_asof(session, LAST) if h.symbol == "NOC"]
    assert (holding.status, holding.value) == ("missing_price", None)


async def test_an_asset_with_a_price_is_not_flagged(session, run, fake, clock) -> None:
    connection = await _scenario(
        session,
        run,
        fake,
        holds={"AAA": FIRST},
        pairs=[("AAAEUR", "AAA", "EUR")],
        bars={"AAAEUR": {DAYS[0]: "2"}},
    )
    async with _world(session, connection.id, fake, clock) as (ctx, client):
        report = await update_prices(ctx, client)
    assert report.unpriced == []
    assert "flags" not in (await get_connection(session, connection.id)).config


async def test_an_asset_priced_by_hand_is_not_flagged(session, run, fake, clock, eur) -> None:
    from lararium.modules.finance.pricing import record_price

    connection = await _scenario(session, run, fake, holds={"NOC": FIRST})
    noc = await find_by_identifier(session, "binance_asset", "NOC")
    assert noc is not None
    await run(
        record_price,
        instrument_id=noc.id,
        quote_instrument_id=eur.id,
        price=Decimal("3"),
        as_of_date=FIRST,
    )
    async with _world(session, connection.id, fake, clock) as (ctx, client):
        assert (await update_prices(ctx, client)).unpriced == []


# CoinGecko


class FakeGecko:
    """CoinGecko's three endpoints, checking the key is sent as a header and nowhere else."""

    def __init__(
        self,
        *,
        tickers: Sequence[tuple[str, str]] = (),
        charts: Mapping[str, Sequence[tuple[int, str]]] = {},
        currencies: Sequence[str] = ("usd", "eur", "gbp"),
        clock: Clock | None = None,
    ) -> None:
        self.tickers = [
            {"base": base, "target": "USDT", "coin_id": coin, "target_coin_id": "tether"}
            for base, coin in tickers
        ]
        self.charts = charts
        self.currencies = list(currencies)
        self.requests: list[httpx.Request] = []
        self._clock = clock
        self.times: list[int] = []

    def calls(self, path: str) -> list[httpx.Request]:
        return [r for r in self.requests if r.url.path == f"/api/v3/{path}"]

    def handler(self, request: httpx.Request) -> httpx.Response:
        assert request.url.scheme == "https" and request.method == "GET"
        assert request.headers["x-cg-demo-api-key"] == KEY
        assert KEY not in str(request.url) and KEY not in request.url.query.decode()
        self.requests.append(request)
        if self._clock is not None:
            self.times.append(self._clock.ms)
        params = dict(request.url.params)
        path = request.url.path.removeprefix("/api/v3/")
        if path == "exchanges/binance/tickers":
            page = int(params.get("page", "1"))
            body = {"name": "Binance", "tickers": self.tickers if page == 1 else []}
            return httpx.Response(200, content=json.dumps(body).encode())
        if path == "simple/supported_vs_currencies":
            return httpx.Response(200, content=json.dumps(self.currencies).encode())
        if path.startswith("coins/") and path.endswith("/market_chart/range"):
            coin = path.removeprefix("coins/").removesuffix("/market_chart/range")
            low, high = int(params["from"]) * 1000, int(params["to"]) * 1000
            points = [
                f"[{ms},{price}]" for ms, price in self.charts.get(coin, []) if low <= ms <= high
            ]
            return httpx.Response(200, content=f'{{"prices":[{",".join(points)}]}}'.encode())
        raise AssertionError(f"a request outside the plan: {path}")


def _midnights(days: Sequence[date], prices: Sequence[str]) -> list[tuple[int, str]]:
    """The 00:00 UTC points of the days after `days`."""
    return [(_ms(d + timedelta(days=1)), p) for d, p in zip(days, prices, strict=True)]


async def test_coingecko_fills_only_unpriced_assets(session, run, fake, clock) -> None:
    connection = await _scenario(
        session,
        run,
        fake,
        holds={"AAA": FIRST, "NOC": FIRST},
        pairs=[("AAAEUR", "AAA", "EUR")],
        bars={"AAAEUR": _all("2")},
    )
    gecko = FakeGecko(
        tickers=[("AAA", "aaa-coin"), ("NOC", "noc-coin")],
        charts={
            "noc-coin": [
                *_midnights(DAYS, ["7", "7.5", "8", "8.5", "9"]),
                (_ms(FIRST) + 3_600_000, "1000"),  # not a 00:00 point
            ],
            "aaa-coin": _midnights(DAYS, ["50"] * 5),
        },
    )
    async with _world(session, connection.id, fake, clock, gecko=gecko, key=KEY) as (ctx, client):
        await price_assets(ctx, client)
        # Both assets are given: AAA already has a `binance` price on every day.
        written = await coingecko_prices(ctx, ["NOC", "AAA"], clock=clock, sleep=clock.sleep)

    assert written == 5
    # Dated the day before each 00:00 UTC point, in the reporting currency (GBP).
    assert await _prices(session, "NOC", "coingecko") == dict(
        zip(DAYS, map(Decimal, ["7", "7.5", "8", "8.5", "9"]), strict=True)
    )
    assert await _prices(session, "AAA", "coingecko") == {}
    assert await _prices(session, "AAA") == _all("2")
    # Each id is the instrument's `coingecko` identifier; they come from the tickers.
    for code, coin in (("AAA", "aaa-coin"), ("NOC", "noc-coin")):
        found = await find_by_identifier(session, "coingecko", coin)
        assert found is not None and ("binance_asset", code) in {
            (i.scheme, i.value) for i in found.identifiers
        }
    [chart] = gecko.calls("coins/noc-coin/market_chart/range")
    assert chart.url.params["vs_currency"] == "gbp"
    assert gecko.calls("coins/aaa-coin/market_chart/range") == []


async def test_coingecko_fills_the_days_binance_has_no_price_for(session, run, fake, clock) -> None:
    connection = await _scenario(
        session, run, fake, holds={"NOC": FIRST}, pairs=[("NOCEUR", "NOC", "EUR")]
    )
    fake.add("klines", _bar(DAYS[0], "2"), _bar(DAYS[1], "3"), when={"symbol": "NOCEUR"})
    gecko = FakeGecko(
        tickers=[("NOC", "noc-coin")],
        charts={"noc-coin": _midnights(DAYS, ["70", "71", "72", "73", "74"])},
    )
    async with _world(session, connection.id, fake, clock, gecko=gecko, key=KEY) as (ctx, client):
        await price_assets(ctx, client)
        await coingecko_prices(ctx, ["NOC"], clock=clock, sleep=clock.sleep)

    assert set(await _prices(session, "NOC")) == set(DAYS[:2])
    assert await _prices(session, "NOC", "coingecko") == {
        d: Decimal(p) for d, p in zip(DAYS[2:], ["72", "73", "74"], strict=True)
    }


async def test_coingecko_quotes_in_usd_when_the_reporting_currency_is_not_supported(
    session, run, fake, clock
) -> None:
    connection = await _scenario(session, run, fake, holds={"NOC": FIRST})
    gecko = FakeGecko(
        tickers=[("NOC", "noc-coin")],
        charts={"noc-coin": _midnights(DAYS, ["7"] * 5)},
        currencies=["usd", "eur"],
    )
    async with _world(session, connection.id, fake, clock, gecko=gecko, key=KEY) as (ctx, _):
        await coingecko_prices(ctx, ["NOC"], clock=clock, sleep=clock.sleep)

    [chart] = gecko.calls("coins/noc-coin/market_chart/range")
    assert chart.url.params["vs_currency"] == "usd"
    usd = (await get_currency(session, "USD")).id
    quote = await session.scalar(
        text("select distinct quote_instrument_id from finance.prices where source = 'coingecko'")
    )
    assert quote == usd


async def test_coingecko_does_not_ask_for_more_than_365_days(session, run, fake, clock) -> None:
    long_ago = date(2024, 1, 1)
    connection = await _scenario(session, run, fake, holds={"NOC": long_ago})
    gecko = FakeGecko(tickers=[("NOC", "noc-coin")], charts={"noc-coin": []})
    async with _world(session, connection.id, fake, clock, gecko=gecko, key=KEY) as (ctx, _):
        await coingecko_prices(ctx, ["NOC"], clock=clock, sleep=clock.sleep)

    [chart] = gecko.calls("coins/noc-coin/market_chart/range")
    assert int(chart.url.params["from"]) >= NOW_MS // 1000 - 365 * 86_400


async def test_coingecko_is_a_no_op_without_a_key(session, run, fake, clock) -> None:
    connection = await _scenario(session, run, fake, holds={"NOC": FIRST})
    async with _world(session, connection.id, fake, clock) as (ctx, client):  # no gecko at all
        assert await coingecko_prices(ctx, ["NOC"], clock=clock, sleep=clock.sleep) == 0
        report = await update_prices(ctx, client)
    assert report.unpriced == ["NOC"]
    count = await session.scalar(text("select count(*) from finance.binance_coingecko_ids"))
    assert count == 0


async def test_coingecko_id_never_guessed_by_symbol(session, run, fake, clock) -> None:
    connection = await _scenario(session, run, fake, holds={"NOC": FIRST})
    # CoinGecko has a coin whose id is the symbol, but no Binance ticker names it.
    gecko = FakeGecko(
        tickers=[("OTHER", "other-coin")], charts={"noc": _midnights(DAYS, ["7"] * 5)}
    )
    async with _world(session, connection.id, fake, clock, gecko=gecko, key=KEY) as (ctx, client):
        report = await update_prices(ctx, client)

    assert report == PriceReport(priced=0, unpriced=["NOC"])
    assert not [r for r in gecko.requests if "market_chart" in r.url.path]
    ids = await session.execute(
        text("select asset, coingecko_id from finance.binance_coingecko_ids")
    )
    assert [tuple(r) for r in ids] == [("NOC", None)]
    assert await find_by_identifier(session, "coingecko", "noc") is None
    assert (await get_connection(session, connection.id)).config["flags"] == ["no price for NOC"]


async def test_two_ids_for_one_asset_are_not_chosen_between(session, run, fake, clock) -> None:
    connection = await _scenario(session, run, fake, holds={"NOC": FIRST})
    gecko = FakeGecko(tickers=[("NOC", "noc-one"), ("NOC", "noc-two")])
    async with _world(session, connection.id, fake, clock, gecko=gecko, key=KEY) as (ctx, _):
        assert await coingecko_prices(ctx, ["NOC"], clock=clock, sleep=clock.sleep) == 0

    value = await session.scalar(text("select coingecko_id from finance.binance_coingecko_ids"))
    assert value is None


async def test_a_priced_asset_gets_its_id_but_no_prices(session, run, fake, clock) -> None:
    connection = await _scenario(
        session,
        run,
        fake,
        holds={"AAA": FIRST},
        pairs=[("AAAEUR", "AAA", "EUR")],
        bars={"AAAEUR": _all("2")},
    )
    gecko = FakeGecko(
        tickers=[("AAA", "aaa-coin")], charts={"aaa-coin": _midnights(DAYS, ["9"] * 5)}
    )
    async with _world(session, connection.id, fake, clock, gecko=gecko, key=KEY) as (ctx, client):
        report = await update_prices(ctx, client)

    assert report == PriceReport(priced=5, unpriced=[])
    assert [r for r in gecko.requests if "market_chart" in r.url.path] == []
    assert await find_by_identifier(session, "coingecko", "aaa-coin") is not None


async def test_update_prices_prices_what_binance_cannot_through_coingecko(
    session, run, fake, clock
) -> None:
    connection = await _scenario(session, run, fake, holds={"NOC": FIRST})
    gecko = FakeGecko(
        tickers=[("NOC", "noc-coin")], charts={"noc-coin": _midnights(DAYS, ["7"] * 5)}
    )
    async with _world(session, connection.id, fake, clock, gecko=gecko, key=KEY) as (ctx, client):
        report = await update_prices(ctx, client)

    assert report == PriceReport(priced=5, unpriced=[])
    assert "flags" not in (await get_connection(session, connection.id)).config


async def test_coingecko_rerun_writes_and_asks_for_nothing(session, run, fake, clock) -> None:
    connection = await _scenario(session, run, fake, holds={"NOC": FIRST})
    gecko = FakeGecko(
        tickers=[("NOC", "noc-coin")], charts={"noc-coin": _midnights(DAYS, ["7"] * 5)}
    )
    async with _world(session, connection.id, fake, clock, gecko=gecko, key=KEY) as (ctx, client):
        await update_prices(ctx, client)
    rows = await _price_rows(session)
    audit = await session.scalar(text("select count(*) from core.audit_log"))
    gecko.requests.clear()

    async with _world(session, connection.id, fake, clock, gecko=gecko, key=KEY) as (ctx, client):
        await update_prices(ctx, client)

    assert await _price_rows(session) == rows
    assert await session.scalar(text("select count(*) from core.audit_log")) == audit
    assert gecko.requests == []


async def test_a_coingecko_failure_does_not_fail_the_binance_prices(
    session, run, fake, clock
) -> None:
    connection = await _scenario(
        session,
        run,
        fake,
        holds={"AAA": FIRST, "NOC": FIRST},
        pairs=[("AAAEUR", "AAA", "EUR")],
        bars={"AAAEUR": _all("2")},
    )

    def broken(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, content=b"")

    gecko = FakeGecko()
    gecko.handler = broken  # type: ignore[method-assign]
    async with _world(session, connection.id, fake, clock, gecko=gecko, key=KEY) as (ctx, client):
        report = await update_prices(ctx, client)

    assert report == PriceReport(priced=5, unpriced=["NOC"])
    assert await _prices(session, "AAA") == _all("2")


# The CoinGecko client


def _client(handler: Callable[[httpx.Request], httpx.Response], clock: Clock) -> CoinGeckoClient:
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return CoinGeckoClient(http, Secret(KEY), clock=clock, sleep=clock.sleep)


async def test_at_most_25_calls_a_minute() -> None:
    clock = Clock()
    gecko = FakeGecko(clock=clock)
    client = _client(gecko.handler, clock)
    for _ in range(60):
        await client.supported_currencies()

    times = gecko.times
    assert len(times) == 60
    for start in times:
        assert sum(1 for t in times if start <= t < start + 60_000) <= 25
    assert times[-1] - times[0] >= 2 * 60_000 - 1  # 60 calls need three minutes' windows


async def test_a_429_waits_and_retries_then_gives_up() -> None:
    clock = Clock()
    seen: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(clock.ms)
        return httpx.Response(429, headers={"Retry-After": "7"})

    client = _client(handler, clock)
    with pytest.raises(CoinGeckoError) as raised:
        await client.supported_currencies()

    assert len(seen) == 4  # the call and three retries
    assert [b - a for a, b in itertools.pairwise(seen)] == [7_000, 7_000, 7_000]
    assert "429" in str(raised.value)


async def test_an_error_names_the_status_and_never_the_key_or_the_url() -> None:
    clock = Clock()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, content=f"bad key {KEY}".encode())

    with pytest.raises(CoinGeckoError) as raised:
        await _client(handler, clock).supported_currencies()
    assert str(raised.value) == "CoinGecko request failed (HTTP 401)"

    def unreachable(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"cannot reach {request.url}", request=request)

    with pytest.raises(CoinGeckoError) as raised:
        await _client(unreachable, clock).supported_currencies()
    assert str(raised.value) == "CoinGecko request failed (no response)"


async def test_a_redirect_is_not_followed() -> None:
    clock = Clock()
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(302, headers={"Location": "https://evil.example/steal"})

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=True)
    client = CoinGeckoClient(http, Secret(KEY), clock=clock, sleep=clock.sleep)
    with pytest.raises(CoinGeckoError):
        await client.supported_currencies()
    assert len(calls) == 1


async def test_the_client_never_leaves_its_host_or_the_key_its_header() -> None:
    clock = Clock()
    gecko = FakeGecko(
        tickers=[("AAA", "aaa-coin")], charts={"aaa-coin": [(_ms(FIRST), "1")]}, clock=clock
    )
    client = _client(gecko.handler, clock)
    await client.tickers(1)
    await client.market_chart("aaa-coin", "usd", 1, 2)
    assert {r.url.host for r in gecko.requests} == {"api.coingecko.com"}
    with pytest.raises(CoinGeckoError):
        await client.market_chart("../../etc/passwd", "usd", 1, 2)
    assert len(gecko.requests) == 2
    assert repr(client) == "CoinGeckoClient()"


# The instrument of an asset by its CoinGecko id (design 5.5)


async def _stored_id(session, asset: str, coin: str) -> None:
    await session.execute(
        text("insert into finance.binance_coingecko_ids (asset, coingecko_id) values (:a, :c)"),
        {"a": asset, "c": coin},
    )


async def test_an_asset_is_the_instrument_of_its_coingecko_id(session, run) -> None:
    connection = await binance_connection(run)
    old = await run(
        create_instrument,
        kind="crypto",
        symbol="OLDNOC",
        name="Old name",
        decimals=8,
        quote_instrument_id=None,
        identifiers=[InstrumentIdentifier("coingecko", "noc-coin")],
    )
    await _stored_id(session, "NEWNOC", "noc-coin")

    found = await run(instruments.asset, connection.id, "NEWNOC")

    assert found == old.id
    tagged = await find_by_identifier(session, "binance_asset", "NEWNOC")
    assert tagged is not None and tagged.id == old.id


async def test_the_coingecko_id_comes_before_the_symbol(session, run) -> None:
    connection = await binance_connection(run)
    by_id = await run(
        create_instrument,
        kind="crypto",
        symbol="OLDNOC",
        name="By id",
        decimals=8,
        quote_instrument_id=None,
        identifiers=[InstrumentIdentifier("coingecko", "noc-coin")],
    )
    await run(
        create_instrument,
        kind="crypto",
        symbol="NEWNOC",
        name="By symbol",
        decimals=8,
        quote_instrument_id=None,
        identifiers=[],
    )
    await _stored_id(session, "NEWNOC", "noc-coin")

    assert await run(instruments.asset, connection.id, "NEWNOC") == by_id.id


async def test_an_instrument_of_another_binance_asset_is_not_taken_by_id(session, run) -> None:
    connection = await binance_connection(run)
    other = await run(
        create_instrument,
        kind="crypto",
        symbol="OLDNOC",
        name="Other asset",
        decimals=8,
        quote_instrument_id=None,
        identifiers=[
            InstrumentIdentifier("coingecko", "noc-coin"),
            InstrumentIdentifier("binance_asset", "OLDNOC"),
        ],
    )
    await _stored_id(session, "NEWNOC", "noc-coin")

    made = await run(instruments.asset, connection.id, "NEWNOC")

    assert made != other.id


# The sync runs the step


async def test_a_sync_prices_the_assets_it_holds(session, run, fake, connector, clock) -> None:
    fake.add("deposits", _deposit(0, DAYS[1], "5", "AAA"))
    fake.add(
        "exchange-info",
        build_row("exchange-info", symbol="AAAEUR", baseAsset="AAA", quoteAsset="EUR"),
    )
    fake.add("klines", *[_bar(d, "2") for d in DAYS], when={"symbol": "AAAEUR"})
    connection = await binance_connection(run, config=later_config())

    after = await run_binance_sync(session, connection.id, fake.handler)

    assert after.status == "healthy", after.last_error
    # From the day before its first record: the opening adjustment is a posting.
    assert set(DAYS[1:]) <= set(await _prices(session, "AAA")) <= set(DAYS)


async def test_a_sync_prices_through_coingecko_with_the_operators_key(
    session, run, fake, connector, clock
) -> None:
    fake.add("deposits", _deposit(0, DAYS[1], "5", "NOC"))
    gecko = FakeGecko(
        tickers=[("NOC", "noc-coin")], charts={"noc-coin": _midnights(DAYS, ["7"] * 5)}
    )
    settings = SETTINGS.model_copy(update={"coingecko_api_key": SecretStr(KEY)})
    connection = await binance_connection(run, config=later_config())

    after = await run_binance_sync(session, connection.id, _route(fake, gecko), settings=settings)

    assert after.status == "healthy", after.last_error
    assert set(DAYS[1:]) <= set(await _prices(session, "NOC", "coingecko"))
    assert KEY not in json.dumps(after.config) and KEY not in str(after.warnings)
    assert KEY not in (after.last_error or "")
