import json
from collections.abc import Callable
from datetime import date, timedelta
from decimal import Decimal

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from lararium.core.settings_store import set_setting
from lararium.modules.finance import models
from lararium.modules.finance.ecb import (
    drop_superseded,
    fetch_rates,
    run_fetch_ecb_rates,
    store_rates,
)
from lararium.modules.finance.instruments import InstrumentIdentifier, create_instrument
from lararium.modules.finance.ledger import PostingInput, TransactionInput, post_transaction
from lararium.modules.finance.pricing import record_fx_rate
from lararium.modules.finance.snapshots import record_snapshot
from lararium.modules.finance.valuation import holdings_asof

from .test_valuation import at

DAY = date(2026, 11, 10)


def frankfurter(rates: dict[str, dict[str, str]]) -> str:
    """A Frankfurter range response; the rates are JSON numbers, as the real one sends."""
    body = ", ".join(
        f'"{day}": {{' + ", ".join(f'"{c}": {v}' for c, v in by.items()) + "}"
        for day, by in rates.items()
    )
    return '{"amount": 1, "base": "EUR", "rates": {' + body + "}}"


def client(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def rows(session: AsyncSession, source: str | None = None) -> list[models.FxRate]:
    statement = select(models.FxRate).order_by(models.FxRate.as_of_date, models.FxRate.source)
    if source is not None:
        statement = statement.where(models.FxRate.source == source)
    return list((await session.scalars(statement)).all())


async def ibkr_rate(run, base, quote, value: str, day: date) -> None:
    await run(
        record_fx_rate,
        base_instrument_id=base.id,
        quote_instrument_id=quote.id,
        rate=Decimal(value),
        as_of_date=day,
        source="ibkr",
    )


async def post_gbp(run, make_account, gbp, when) -> None:
    bank = await make_account(name="Bank", balance_mode="transactions")
    other = await make_account(name="Other", ledger_type="equity", subtype="other")
    await run(
        post_transaction,
        TransactionInput(
            occurred_at=when,
            description="in",
            postings=[
                PostingInput(bank.id, gbp.id, Decimal("5")),
                PostingInput(other.id, gbp.id, Decimal("-5")),
            ],
        ),
    )


async def test_parses_frankfurter_range() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            text=frankfurter(
                {
                    "2026-11-09": {"GBP": "0.87345", "USD": "1.1"},
                    "2026-11-10": {"GBP": "0.874", "USD": "1.0987654321012345678"},
                }
            ),
        )

    async with client(handler) as http:
        got = await fetch_rates(http, date(2026, 11, 9), DAY)

    assert got == {
        date(2026, 11, 9): {"GBP": Decimal("0.87345"), "USD": Decimal("1.1")},
        DAY: {"GBP": Decimal("0.874"), "USD": Decimal("1.0987654321012345678")},
    }
    [request] = seen
    assert request.method == "GET"
    assert request.url.host == "api.frankfurter.dev"
    assert request.url.path == "/v1/2026-11-09..2026-11-10"
    assert request.url.params["base"] == "EUR"


async def test_a_failed_or_malformed_response_raises() -> None:
    async with client(lambda _: httpx.Response(500, text="no")) as http:
        with pytest.raises(httpx.HTTPStatusError):
            await fetch_rates(http, DAY, DAY)
    async with client(lambda _: httpx.Response(200, text=json.dumps({"rates": []}))) as http:
        with pytest.raises(ValueError, match="rates"):
            await fetch_rates(http, DAY, DAY)


async def test_only_existing_currencies_are_stored(session, run, gbp) -> None:
    stored = await run(
        store_rates, {DAY: {"GBP": Decimal("0.87"), "ZZZ": Decimal("3"), "EUR": Decimal("1")}}
    )
    assert stored == 1
    [row] = await rows(session)
    assert (row.quote_instrument_id, row.rate, row.as_of_date, row.source) == (
        gbp.id,
        Decimal("0.87"),
        DAY,
        "ecb",
    )


async def test_gap_rule(session, run, gbp, eur, usd) -> None:
    # IBKR has GBP->EUR on DAY: no ECB EUR->GBP that day, in either direction.
    await ibkr_rate(run, gbp, eur, "1.15", DAY)
    next_day = DAY + timedelta(days=1)
    stored = await run(
        store_rates,
        {
            DAY: {"GBP": Decimal("0.87"), "USD": Decimal("1.1")},
            next_day: {"GBP": Decimal("0.88")},
        },
    )
    assert stored == 2
    ecb = [(r.quote_instrument_id, r.as_of_date) for r in await rows(session, "ecb")]
    assert ecb == [(usd.id, DAY), (gbp.id, next_day)]


async def test_storing_the_same_rates_again_writes_nothing(session, run, gbp) -> None:
    rates = {DAY: {"GBP": Decimal("0.87")}}
    assert await run(store_rates, rates) == 1
    [first] = await rows(session)
    assert await run(store_rates, rates) == 0
    assert [r.recorded_at for r in await rows(session)] == [first.recorded_at]
    assert await run(store_rates, {DAY: {"GBP": Decimal("0.9")}}) == 1


async def test_superseded_rate_is_dropped(session, run, gbp, eur, usd) -> None:
    await run(store_rates, {DAY: {"GBP": Decimal("0.87"), "USD": Decimal("1.1")}})
    assert await run(drop_superseded) == 0
    await ibkr_rate(run, gbp, eur, "1.15", DAY)  # the inverse direction counts too
    assert await run(drop_superseded) == 1
    assert [(r.quote_instrument_id, r.source) for r in await rows(session)] == [
        (usd.id, "ecb"),
        (eur.id, "ibkr"),
    ]


async def test_ecb_never_changes_an_ibkr_valuation(
    session, run, make_account, gbp, eur, usd
) -> None:
    """Review Focus: an account valued through IBKR's rates is unchanged by ECB rates."""
    days = [date(2026, 11, 1) + timedelta(days=n) for n in range(10)]
    broker = await make_account(name="Broker")
    for instrument, quantity in ((usd, "1000"), (eur, "500")):
        await run(
            record_snapshot,
            account_id=broker.id,
            instrument_id=instrument.id,
            quantity=Decimal(quantity),
            as_of=at(2026, 10, 1),
        )
    for n, day in enumerate(days):
        await ibkr_rate(run, usd, gbp, str(Decimal("0.7") + Decimal(n) / 100), day)
        await ibkr_rate(run, eur, gbp, str(Decimal("0.85") + Decimal(n) / 100), day)
    before = [await holdings_asof(session, day) for day in days]
    assert all(h.status == "ok" for holdings in before for h in holdings)

    around = [days[0] - timedelta(days=1), *days, days[-1] + timedelta(days=1)]
    ecb = {
        day: {"GBP": Decimal("0.9") + Decimal(n) / 50, "USD": Decimal("1.2") - Decimal(n) / 50}
        for n, day in enumerate(around)
    }
    assert await run(store_rates, ecb) > 0
    assert await run(drop_superseded) == 0

    assert [await holdings_asof(session, day) for day in days] == before


async def test_task_fetches_from_the_day_after_the_latest_rate(
    session, run, make_account, gbp
) -> None:
    sessions = async_sessionmaker(bind=session.bind, join_transaction_mode="create_savepoint")
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(request.url.path)
        return httpx.Response(200, text=frankfurter({"2026-11-10": {"GBP": "0.87"}}))

    # Nothing to value yet and no rate: nothing to fetch.
    async with client(handler) as http:
        assert await run_fetch_ecb_rates(sessions, http, DAY) == 0
    assert requested == []

    # With no rate yet: from the earliest booking date of any posting.
    await post_gbp(run, make_account, gbp, at(2026, 11, 8))
    async with client(handler) as http:
        assert await run_fetch_ecb_rates(sessions, http, DAY) == 1
    assert requested == ["/v1/2026-11-08..2026-11-10"]

    # Then from the day after the latest ecb date; up to date: no request.
    requested.clear()
    async with client(handler) as http:
        assert await run_fetch_ecb_rates(sessions, http, DAY) == 0
    assert requested == []
    async with client(handler) as http:
        await run_fetch_ecb_rates(sessions, http, DAY + timedelta(days=2))
    assert requested == ["/v1/2026-11-11..2026-11-12"]


async def test_a_long_range_is_fetched_in_chunks(session, run, make_account, gbp) -> None:
    sessions = async_sessionmaker(bind=session.bind, join_transaction_mode="create_savepoint")
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(request.url.path)
        return httpx.Response(200, text=frankfurter({}))

    await post_gbp(run, make_account, gbp, at(2026, 1, 1))
    async with client(handler) as http:
        await run_fetch_ecb_rates(sessions, http, DAY)
    assert requested[0] == "/v1/2026-01-01..2026-03-31"
    assert requested[-1] == f"/v1/2026-09-28..{DAY}"
    assert len(requested) == 4


async def test_reporting_currency_outside_the_ecb_is_missing_fx(
    session, run, make_account, gbp
) -> None:
    await run(
        create_instrument,
        kind="fiat",
        symbol="XTS",
        name="Test Currency",
        decimals=2,
        quote_instrument_id=None,
        identifiers=[InstrumentIdentifier("iso4217", "XTS")],
    )
    bank = await make_account()
    await run(
        record_snapshot,
        account_id=bank.id,
        instrument_id=gbp.id,
        quantity=Decimal("100"),
        as_of=at(2026, 11, 1),
    )
    await run(set_setting, "finance.reporting_currency", "XTS")
    # The ECB has no rate for the reporting currency, so nothing of it is stored.
    await run(store_rates, {DAY: {"GBP": Decimal("0.87"), "USD": Decimal("1.1")}})
    [holding] = await holdings_asof(session, DAY)
    assert (holding.status, holding.value) == ("missing_fx", None)
