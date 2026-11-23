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


def answer(request: httpx.Request) -> httpx.Response:
    """A response with the one rate there is, on DAY, when the range asked for holds DAY."""
    first, last = request.url.path.removeprefix("/v1/").split("..")
    has_day = date.fromisoformat(first) <= DAY <= date.fromisoformat(last)
    return httpx.Response(200, text=frankfurter({str(DAY): {"GBP": "0.87"}} if has_day else {}))


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
    async with client(
        lambda _: httpx.Response(200, text=json.dumps({"base": "EUR", "rates": []}))
    ) as http:
        with pytest.raises(ValueError, match="rates"):
            await fetch_rates(http, DAY, DAY)


@pytest.mark.parametrize(
    "body",
    [
        '{"base": "EUR", "rates": {"2026-11-11": {"GBP": 0.87}}}',  # after the range
        '{"base": "USD", "rates": {"2026-11-10": {"GBP": 0.87}}}',
        '{"rates": {"2026-11-10": {"GBP": 0.87}}}',
        '{"amount": 100, "base": "EUR", "rates": {"2026-11-10": {"GBP": 0.87}}}',
    ],
)
async def test_a_response_that_is_not_what_was_asked_for_raises(body: str) -> None:
    async with client(lambda _: httpx.Response(200, text=body)) as http:
        with pytest.raises(ValueError):
            await fetch_rates(http, DAY, DAY)


def like_frankfurter(request: httpx.Request) -> httpx.Response:
    """What Frankfurter answers: a GBP rate for every working day of the range and, for a range
    that starts on a weekend, the working day before it first."""
    first, last = (date.fromisoformat(d) for d in request.url.path.removeprefix("/v1/").split(".."))
    days = [first + timedelta(days=n) for n in range((last - first).days + 1)]
    published = [d for d in days if d.weekday() < 5]
    if first.weekday() >= 5:
        published.insert(0, first - timedelta(days=first.weekday() - 4))
    return httpx.Response(200, text=frankfurter({str(d): {"GBP": "0.87"} for d in published}))


async def test_the_working_day_before_the_range_is_kept() -> None:
    friday, saturday, sunday, monday = (date(2026, 11, d) for d in (6, 7, 8, 9))
    rate = {"GBP": Decimal("0.87")}
    async with client(like_frankfurter) as http:
        # Frankfurter answers a weekend with Friday's rates: the rate in force on it.
        assert await fetch_rates(http, saturday, sunday) == {friday: rate}
        assert await fetch_rates(http, saturday, monday) == {friday: rate, monday: rate}


async def test_a_day_long_before_the_range_is_left_out() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, text=frankfurter({"2026-10-30": {"GBP": "0.9"}, "2026-11-09": {"GBP": "0.87"}})
        )

    async with client(handler) as http:
        got = await fetch_rates(http, date(2026, 11, 7), date(2026, 11, 9))
    assert got == {date(2026, 11, 9): {"GBP": Decimal("0.87")}}


async def test_a_backfill_starting_on_a_weekend_has_a_rate_for_its_first_day(
    session, run, make_account, gbp
) -> None:
    """Holdings that begin on a Sunday are valued with Friday's rate, the latest on or before."""
    sessions = async_sessionmaker(bind=session.bind, join_transaction_mode="create_savepoint")
    friday, monday = date(2026, 11, 6), date(2026, 11, 9)
    await post_gbp(run, make_account, gbp, at(2026, 11, 8))
    async with client(like_frankfurter) as http:
        await run_fetch_ecb_rates(sessions, http, monday)
    assert [r.as_of_date for r in await rows(session, "ecb")] == [friday, monday]


async def test_a_ledger_of_snapshots_only_starts_at_its_first_snapshot(
    session, run, make_account, gbp
) -> None:
    sessions = async_sessionmaker(bind=session.bind, join_transaction_mode="create_savepoint")
    account = await make_account(name="Savings", balance_mode="snapshots")
    await run(
        record_snapshot,
        account_id=account.id,
        instrument_id=gbp.id,
        quantity=Decimal("5"),
        as_of=at(2026, 11, 4),
    )
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(request.url.path)
        return like_frankfurter(request)

    async with client(handler) as http:
        await run_fetch_ecb_rates(sessions, http, DAY)
    assert requested == ["/v1/2026-11-04..2026-11-10"]
    assert len(await rows(session, "ecb")) == 5


async def test_a_snapshot_before_the_first_posting_sets_the_start(
    session, run, make_account, gbp
) -> None:
    sessions = async_sessionmaker(bind=session.bind, join_transaction_mode="create_savepoint")
    account = await make_account(name="Savings", balance_mode="snapshots")
    await run(
        record_snapshot,
        account_id=account.id,
        instrument_id=gbp.id,
        quantity=Decimal("5"),
        as_of=at(2026, 11, 3),
    )
    await post_gbp(run, make_account, gbp, at(2026, 11, 9))
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(request.url.path)
        return like_frankfurter(request)

    async with client(handler) as http:
        await run_fetch_ecb_rates(sessions, http, DAY)
    assert requested == ["/v1/2026-11-03..2026-11-10"]


async def test_a_weekend_neither_fails_nor_stalls_the_job(session, run, make_account, gbp) -> None:
    sessions = async_sessionmaker(bind=session.bind, join_transaction_mode="create_savepoint")
    friday, monday, tuesday = date(2026, 11, 6), date(2026, 11, 9), date(2026, 11, 10)
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(request.url.path)
        return like_frankfurter(request)

    await post_gbp(run, make_account, gbp, at(2026, 11, 6))
    for today in (friday, date(2026, 11, 7), date(2026, 11, 8), monday, tuesday):
        async with client(handler) as http:
            await run_fetch_ecb_rates(sessions, http, today)
    assert requested == [
        "/v1/2026-11-06..2026-11-06",
        "/v1/2026-11-07..2026-11-07",
        "/v1/2026-11-07..2026-11-08",
        "/v1/2026-11-07..2026-11-09",
        "/v1/2026-11-10..2026-11-10",
    ]
    assert [r.as_of_date for r in await rows(session, "ecb")] == [friday, monday, tuesday]


async def test_a_backfill_window_starting_on_a_weekend_is_stored(
    session, run, make_account, gbp
) -> None:
    sessions = async_sessionmaker(bind=session.bind, join_transaction_mode="create_savepoint")
    first = date(2025, 12, 29)
    # The 90-day windows from this Monday start on a Sunday, then on a Saturday.
    await post_gbp(run, make_account, gbp, at(first.year, first.month, first.day))
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(request.url.path)
        return like_frankfurter(request)

    async with client(handler) as http:
        written = await run_fetch_ecb_rates(sessions, http, DAY)

    assert requested[1:3] == ["/v1/2026-03-29..2026-06-26", "/v1/2026-06-27..2026-09-24"]
    stored = [r.as_of_date for r in await rows(session, "ecb")]
    days = [first + timedelta(days=n) for n in range((DAY - first).days + 1)]
    assert stored == [d for d in days if d.weekday() < 5]
    assert written == len(stored)


async def test_a_404_means_no_rates_in_the_range() -> None:
    async with client(lambda _: httpx.Response(404, text="not found")) as http:
        assert await fetch_rates(http, DAY, DAY) == {}
    async with client(lambda _: httpx.Response(403, text="no")) as http:
        with pytest.raises(httpx.HTTPStatusError):
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


@pytest.mark.parametrize("inverse", [False, True], ids=["direct", "inverse"])
async def test_ecb_never_changes_an_ibkr_valuation(
    session, run, make_account, gbp, eur, usd, inverse: bool
) -> None:
    """Review Focus: an account valued through IBKR's rates is unchanged by ECB rates,
    whether IBKR stores them as X->GBP or as GBP->X."""
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
        if inverse:
            await ibkr_rate(run, gbp, usd, str(Decimal("1.4") + Decimal(n) / 100), day)
            await ibkr_rate(run, gbp, eur, str(Decimal("1.15") + Decimal(n) / 100), day)
        else:
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
        return answer(request)

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


async def test_gap_filling_changes_a_day_where_ibkr_has_no_rate_for_the_pair(
    session, run, make_account, gbp, eur, usd
) -> None:
    """Documented behaviour, intended: an ECB rate fills a gap, so a day where IBKR has no
    rate for the valued pair (day 2) takes the ECB's cross rate instead of IBKR's older one,
    while a day IBKR covers (day 1) is unchanged."""
    broker = await make_account(name="Broker")
    await run(
        record_snapshot,
        account_id=broker.id,
        instrument_id=usd.id,
        quantity=Decimal("100"),
        as_of=at(2026, 10, 1),
    )
    day1, day2 = date(2026, 11, 1), date(2026, 11, 2)
    await ibkr_rate(run, usd, gbp, "0.8", day1)
    [day1_before] = await holdings_asof(session, day1)
    [day2_before] = await holdings_asof(session, day2)
    assert day2_before.fx_rate == Decimal("0.8")  # IBKR's day 1 rate carried forward

    both = {"GBP": Decimal("0.9"), "USD": Decimal("1.2")}
    await run(store_rates, {day1: both, day2: both})
    [day1_after] = await holdings_asof(session, day1)
    [day2_after] = await holdings_asof(session, day2)
    assert day1_after == day1_before
    assert day2_after.fx_rate is not None
    assert abs(day2_after.fx_rate - Decimal("0.75")) < Decimal("1e-15")


async def test_a_run_with_nothing_to_fetch_still_drops_superseded_rates(
    session, run, make_account, gbp, eur
) -> None:
    sessions = async_sessionmaker(bind=session.bind, join_transaction_mode="create_savepoint")
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(request.url.path)
        return answer(request)

    await post_gbp(run, make_account, gbp, at(2026, 11, 8))
    async with client(handler) as http:
        await run_fetch_ecb_rates(sessions, http, DAY)
    assert [r.source for r in await rows(session)] == ["ecb"]

    await ibkr_rate(run, gbp, eur, "1.15", DAY)
    requested.clear()
    async with client(handler) as http:
        # A run dated the day before: the marker (yesterday) leaves nothing to fetch.
        assert await run_fetch_ecb_rates(sessions, http, DAY - timedelta(days=1)) == 0
    assert requested == []
    assert [r.source for r in await rows(session)] == ["ibkr"]


async def test_a_failed_fetch_still_drops_superseded_rates(
    session, run, make_account, gbp, eur
) -> None:
    sessions = async_sessionmaker(bind=session.bind, join_transaction_mode="create_savepoint")
    await post_gbp(run, make_account, gbp, at(2026, 11, 8))
    body = frankfurter({"2026-11-10": {"GBP": "0.87"}})
    async with client(lambda _: httpx.Response(200, text=body)) as http:
        await run_fetch_ecb_rates(sessions, http, DAY)
    await ibkr_rate(run, gbp, eur, "1.15", DAY)
    async with client(lambda _: httpx.Response(500, text="down")) as http:
        with pytest.raises(httpx.HTTPStatusError):
            await run_fetch_ecb_rates(sessions, http, DAY + timedelta(days=1))
    assert [r.source for r in await rows(session)] == ["ibkr"]


async def test_the_start_is_clamped_to_the_first_ecb_day(session, run, make_account, gbp) -> None:
    sessions = async_sessionmaker(bind=session.bind, join_transaction_mode="create_savepoint")
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(request.url.path)
        return httpx.Response(200, text=frankfurter({}))

    await post_gbp(run, make_account, gbp, at(1990, 1, 1))
    async with client(handler) as http:
        await run_fetch_ecb_rates(sessions, http, date(1999, 2, 1))
    assert requested == ["/v1/1999-01-04..1999-02-01"]


async def test_a_stored_future_rate_does_not_move_the_start(
    session, run, make_account, gbp
) -> None:
    sessions = async_sessionmaker(bind=session.bind, join_transaction_mode="create_savepoint")
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(request.url.path)
        return httpx.Response(200, text=frankfurter({}))

    await post_gbp(run, make_account, gbp, at(2026, 11, 8))
    await run(store_rates, {date(2027, 1, 1): {"GBP": Decimal("0.87")}})
    async with client(handler) as http:
        await run_fetch_ecb_rates(sessions, http, DAY)
    assert requested == ["/v1/2026-11-08..2026-11-10"]


async def test_days_covered_by_other_sources_are_not_refetched(
    session, run, make_account, gbp, eur
) -> None:
    sessions = async_sessionmaker(bind=session.bind, join_transaction_mode="create_savepoint")
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(request.url.path)
        return answer(request)

    await post_gbp(run, make_account, gbp, at(2026, 11, 10))
    await ibkr_rate(run, gbp, eur, "1.15", DAY)  # every fetched day is covered: no ecb row
    async with client(handler) as http:
        assert await run_fetch_ecb_rates(sessions, http, DAY) == 0
    assert await rows(session, "ecb") == []
    requested.clear()
    async with client(handler) as http:
        await run_fetch_ecb_rates(sessions, http, DAY + timedelta(days=1))
    # The marker stops at yesterday, so the next run starts at DAY, never before it.
    assert requested == ["/v1/2026-11-10..2026-11-11"]


async def test_a_rate_not_yet_published_today_is_fetched_the_next_day(
    session, run, make_account, gbp
) -> None:
    sessions = async_sessionmaker(bind=session.bind, join_transaction_mode="create_savepoint")
    published = False

    def handler(request: httpx.Request) -> httpx.Response:
        if not published:
            return httpx.Response(200, text=frankfurter({}))
        return answer(request)

    await post_gbp(run, make_account, gbp, at(2026, 11, 10))
    async with client(handler) as http:
        assert await run_fetch_ecb_rates(sessions, http, DAY) == 0  # not yet published
    assert await rows(session, "ecb") == []
    published = True
    async with client(handler) as http:
        assert await run_fetch_ecb_rates(sessions, http, DAY + timedelta(days=1)) == 1
    assert [r.as_of_date for r in await rows(session, "ecb")] == [DAY]


async def test_an_empty_or_404_window_leaves_the_marker_and_the_start_unchanged(
    session, run, make_account, gbp
) -> None:
    sessions = async_sessionmaker(bind=session.bind, join_transaction_mode="create_savepoint")
    requested: list[str] = []
    statuses = [404, 200, 404]

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(request.url.path)
        if statuses.pop(0) == 404:
            return httpx.Response(404, text="not found")
        return httpx.Response(200, text=frankfurter({}))

    await post_gbp(run, make_account, gbp, at(2026, 11, 8))
    for _ in range(3):
        async with client(handler) as http:
            assert await run_fetch_ecb_rates(sessions, http, DAY) == 0
    assert requested == ["/v1/2026-11-08..2026-11-10"] * 3


async def test_the_marker_stays_over_a_weekend_and_monday_is_stored(
    session, run, make_account, gbp
) -> None:
    sessions = async_sessionmaker(bind=session.bind, join_transaction_mode="create_savepoint")
    friday = date(2026, 11, 6)
    monday = date(2026, 11, 9)
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(request.url.path)
        first, last = request.url.path.removeprefix("/v1/").split("..")
        published = [d for d in (friday, monday) if first <= str(d) <= last]
        return httpx.Response(200, text=frankfurter({str(d): {"GBP": "0.87"} for d in published}))

    await post_gbp(run, make_account, gbp, at(2026, 11, 6))
    for today in (friday, date(2026, 11, 7), date(2026, 11, 8), monday):
        async with client(handler) as http:
            await run_fetch_ecb_rates(sessions, http, today)
    assert requested == [
        "/v1/2026-11-06..2026-11-06",
        "/v1/2026-11-07..2026-11-07",
        "/v1/2026-11-07..2026-11-08",
        "/v1/2026-11-07..2026-11-09",
    ]
    assert [r.as_of_date for r in await rows(session, "ecb")] == [friday, monday]
