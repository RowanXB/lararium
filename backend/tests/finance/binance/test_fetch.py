"""Fetching Binance history (plan Decision 7): windows, the newest-first backfill, cursors,
paging, products that are not present, and the trade symbol sets."""

import dataclasses
import itertools
import json
from datetime import UTC, datetime

import httpx
import pytest
from sqlalchemy import text

from lararium.core.connections import get_connection
from lararium.core.errors import DisplayableError
from lararium.modules.finance.binance.balances import read_balances
from lararium.modules.finance.binance.catalogue import ENDPOINTS
from lararium.modules.finance.binance.fetch import (
    CURSORS_KEY,
    LAUNCH_MS,
    record_type,
    trade_symbols,
    walk,
    walk_trades,
    windows,
)
from lararium.modules.finance.binance.parse import BinanceDataError

from .fixtures import DAY_MS, HOUR_MS, NOW_MS, FakeBinance, build_response, build_row
from .syncing import Clock, binance_connection, client_for, context, later_config

DEPOSITS = ENDPOINTS["deposits"]


def _ms(day: str) -> int:
    return int(datetime.fromisoformat(day).replace(tzinfo=UTC).timestamp()) * 1000


def test_launch_is_july_2017() -> None:
    assert LAUNCH_MS == _ms("2017-07-01")
    assert NOW_MS == _ms("2026-11-23")


def _contiguous_newest_first(pieces: list[tuple[int, int]]) -> None:
    for (start, end), (older_start, older_end) in itertools.pairwise(pieces):
        assert older_end + 1 == start
        assert older_start <= older_end < start <= end


def test_windows() -> None:
    assert (DEPOSITS.window_days, DEPOSITS.lookback_days) == (89, None)

    # No cursor: back to July 2017, newest first, none wider than 89 days, no gaps.
    pieces = windows(DEPOSITS, None, NOW_MS)
    assert pieces[0][1] == NOW_MS
    assert pieces[-1][0] == LAUNCH_MS
    assert all(end - start <= 89 * DAY_MS for start, end in pieces)
    _contiguous_newest_first(pieces)

    # A lookback: no window starts further back than it.
    short = dataclasses.replace(DEPOSITS, lookback_days=179)
    pieces = windows(short, None, NOW_MS)
    assert pieces[0][1] == NOW_MS
    assert all(start >= NOW_MS - 179 * DAY_MS for start, _ in pieces)
    assert pieces[-1][0] - (NOW_MS - 179 * DAY_MS) < DAY_MS
    _contiguous_newest_first(pieces)

    # An interrupted backfill: only windows older than what is stored.
    backfilled_to = _ms("2024-02-01")
    cursor = {"fetched_to": NOW_MS, "backfilled_to": backfilled_to, "complete": False}
    pieces = windows(DEPOSITS, cursor, NOW_MS)
    assert pieces[0][1] == backfilled_to - 1
    assert all(end < backfilled_to for _, end in pieces)
    assert pieces[-1][0] == LAUNCH_MS
    _contiguous_newest_first(pieces)

    # After the backfill: forwards from fetched_to minus 7 days.
    cursor = {"fetched_to": _ms("2026-11-20"), "backfilled_to": LAUNCH_MS, "complete": True}
    assert windows(DEPOSITS, cursor, NOW_MS) == [(_ms("2026-11-13"), NOW_MS)]

    # A long gap after the backfill: oldest first, each within the span.
    cursor = {"fetched_to": _ms("2026-01-01"), "backfilled_to": LAUNCH_MS, "complete": True}
    pieces = windows(DEPOSITS, cursor, NOW_MS)
    assert pieces[0][0] == _ms("2025-12-25")
    assert pieces[-1][1] == NOW_MS
    assert all(end - start <= 89 * DAY_MS for start, end in pieces)
    assert all(a_end + 1 == b_start for (_, a_end), (b_start, _) in itertools.pairwise(pieces))


def _deposit(seq: int, at: int, **overrides) -> dict:
    return build_row("deposits", seq, insertTime=at, completeTime=at, **overrides)


async def _config(session, connection) -> dict:
    return (await get_connection(session, connection.id)).config


async def _walk(session, connection, fake: FakeBinance, clock: Clock, label: str):
    async with fake.http() as http:
        ctx = await context(session, connection.id, http)
        return await walk(ctx, client_for(clock, http), label)


async def test_backfill_walks_newest_first(session, run, fake, clock) -> None:
    six_months = NOW_MS - 183 * DAY_MS
    fake.fail("deposits", code=-5026, older_than_ms=six_months)
    rows = [_deposit(i, NOW_MS - i * 20 * DAY_MS) for i in range(9)]
    fake.add("deposits", *rows)
    connection = await binance_connection(run)

    ids = await _walk(session, connection, fake, clock, "deposits")

    assert len(set(ids)) == len(rows)
    starts = [int(seen.params["startTime"]) for seen in fake.calls("deposits")]
    assert starts == sorted(starts, reverse=True)
    refused = [start for start in starts if start < six_months]
    assert len(refused) == 1
    assert min(starts) == refused[0]
    cursor = (await _config(session, connection))["cursors"]["deposits"]
    assert cursor["complete"] is True
    assert cursor["fetched_to"] == NOW_MS
    # The refused window is not stored: the cursor names the oldest one that was.
    assert cursor["backfilled_to"] == sorted(starts)[1]


async def test_refused_old_window_ends_the_walk_quietly(session, run, fake, clock) -> None:
    fake.fail("deposits", code=-5026, older_than_ms=NOW_MS - 183 * DAY_MS)
    fake.add("deposits", _deposit(0, NOW_MS - DAY_MS))
    connection = await binance_connection(run)

    await _walk(session, connection, fake, clock, "deposits")

    config = await _config(session, connection)
    assert config.get("not_present", {}) == {}
    assert "flags" not in config
    assert config["cursors"]["deposits"]["complete"] is True


async def test_empty_old_windows_do_not_end_the_walk(session, run, fake, clock) -> None:
    old = NOW_MS - 3 * 365 * DAY_MS
    fake.add("deposits", _deposit(0, old))
    connection = await binance_connection(run)

    ids = await _walk(session, connection, fake, clock, "deposits")

    assert len(ids) == 1
    assert any(int(s.params["startTime"]) < LAUNCH_MS + 89 * DAY_MS for s in fake.calls("deposits"))


async def test_walk_stores_rows_once(session, run, fake, clock) -> None:
    unchanged = _deposit(0, NOW_MS - 2 * DAY_MS)
    changing = _deposit(1, NOW_MS - DAY_MS, status=0)
    fake.add("deposits", unchanged, changing)
    connection = await binance_connection(run)
    first = await _walk(session, connection, fake, clock, "deposits")

    # A day later the deposit is confirmed; the next walk's window overlaps the first's.
    clock.ms += DAY_MS
    fake.set_rows("deposits", [unchanged, {**changing, "status": 1}])
    second = await _walk(session, connection, fake, clock, "deposits")

    assert len(set(first)) == len(set(second)) == 2
    assert len(set(first) & set(second)) == 1


async def test_flexible_rewards_are_asked_with_their_type(session, run, fake, clock) -> None:
    """Binance refuses the rewards history without `type`, which the fake does too."""
    fake.add("earn-flexible-rewards", build_row("earn-flexible-rewards", time=NOW_MS - DAY_MS))
    connection = await binance_connection(run)

    stored = await _walk(session, connection, fake, clock, "earn-flexible-rewards")

    assert len(stored) == 1
    assert {s.params["type"] for s in fake.calls("earn-flexible-rewards")} == {"ALL"}


async def test_deposits_are_asked_with_their_source(session, run, fake, clock) -> None:
    """`sourceAddress` comes back only with `includeSource=true`, as the fake answers too."""
    fake.add("deposits", build_row("deposits", insertTime=NOW_MS - DAY_MS))
    connection = await binance_connection(run)

    stored = await _walk(session, connection, fake, clock, "deposits")

    assert len(stored) == 1
    assert {s.params["includeSource"] for s in fake.calls("deposits")} == {"true"}
    content = await session.scalar(
        text("select content from core.raw_records where id = :i"), {"i": stored[0]}
    )
    assert content["sourceAddress"]


async def test_absent_product_is_recorded_and_skipped(session, run, fake, clock) -> None:
    fake.fail("dividends", code=-3003)
    connection = await binance_connection(run)

    assert await _walk(session, connection, fake, clock, "dividends") == []

    assert len(fake.calls("dividends")) == 1
    config = await _config(session, connection)
    assert "dividends" in config["not_present"]
    assert "dividends" not in config.get("cursors", {})

    # The next sync does not ask; a week later it asks again.
    await _walk(session, connection, fake, clock, "dividends")
    assert len(fake.calls("dividends")) == 1
    clock.ms += 7 * DAY_MS
    fake.clear_failures()
    await _walk(session, connection, fake, clock, "dividends")
    assert len(fake.calls("dividends")) > 1
    assert "dividends" not in (await _config(session, connection))["not_present"]


async def test_a_404_parks_only_a_label_that_never_answered(session, run, fake, clock) -> None:
    # A label whose history walk is complete: a 404 is nothing this time, not "not present".
    fake.add("deposits", _deposit(0, NOW_MS - DAY_MS))
    config = later_config(fetched_to=NOW_MS - DAY_MS)
    config["cursors"].pop("dividends")  # pyright: ignore[reportAttributeAccessIssue,reportUnknownMemberType]
    connection = await binance_connection(run, config=config)
    before = (await _config(session, connection))["cursors"]["deposits"]
    fake.fail("deposits", status=404, times=1)

    assert await _walk(session, connection, fake, clock, "deposits") == []

    config = await _config(session, connection)
    assert "deposits" not in config.get("not_present", {})
    assert config["cursors"]["deposits"] == before
    # The next walk asks again at once and stores what the refused one missed.
    assert len(await _walk(session, connection, fake, clock, "deposits")) == 1

    # A label never walked: the same 404 parks it.
    fake.fail("dividends", status=404, times=1)
    assert await _walk(session, connection, fake, clock, "dividends") == []
    assert "dividends" in (await _config(session, connection))["not_present"]


def _rows_at(label: str, count: int, time_field: str) -> list[dict]:
    return [build_row(label, i, **{time_field: NOW_MS - DAY_MS - i * 1000}) for i in range(count)]


@pytest.mark.parametrize(
    ("label", "time_field", "count", "requests"),
    [
        # none: a full answer splits the window until each part fits.
        ("dividends", "divTime", 501, None),
        ("deposits", "insertTime", 1001, 2),
        ("earn-flexible-subscriptions", "time", 101, 2),
        ("earn-flexible-subscriptions", "time", 100, 2),
    ],
)
async def test_walk_pages_until_short_page(
    session, run, fake, clock, label: str, time_field: str, count: int, requests: int | None
) -> None:
    endpoint = ENDPOINTS[label]
    fake.add(label, *_rows_at(label, count, time_field))
    cursor = {"fetched_to": NOW_MS - 2 * DAY_MS, "backfilled_to": LAUNCH_MS, "complete": True}
    connection = await binance_connection(run, config={"cursors": {label: cursor}})

    ids = await _walk(session, connection, fake, clock, label)

    assert len(set(ids)) == count
    calls = fake.calls(label)
    if requests is not None:
        assert len(calls) == requests
    else:
        assert len(calls) > 1
        assert all(s.params.get("limit") == str(endpoint.page_size) for s in calls)


def _moment(ms: int) -> datetime:
    return datetime.fromtimestamp(ms // 1000, UTC).replace(microsecond=ms % 1000 * 1000)


async def test_walk_stores_each_pages_response(session, run, fake, clock) -> None:
    label = "earn-flexible-rewards"
    fake.add(label, *_rows_at(label, 101, "time"))
    cursor = {"fetched_to": NOW_MS - 2 * DAY_MS, "backfilled_to": LAUNCH_MS, "complete": True}
    connection = await binance_connection(run, config={"cursors": {label: cursor}})

    await _walk(session, connection, fake, clock, label)

    [call, *_] = fake.calls(label)
    window = (_moment(int(call.params["startTime"])), _moment(int(call.params["endTime"])))
    rows = (
        await session.execute(
            text(
                "select window_from, window_to, page, total"
                " from finance.binance_earn_flexible_rewards_responses"
                " where connection_id = :c order by page"
            ),
            {"c": connection.id},
        )
    ).all()
    assert [tuple(row) for row in rows] == [(*window, 0, 101), (*window, 1, 101)]


async def test_a_split_window_stores_each_halfs_response(session, run, fake, clock) -> None:
    label = "dividends"
    fake.add(label, *_rows_at(label, 501, "divTime"))
    cursor = {"fetched_to": NOW_MS - 2 * DAY_MS, "backfilled_to": LAUNCH_MS, "complete": True}
    connection = await binance_connection(run, config={"cursors": {label: cursor}})

    await _walk(session, connection, fake, clock, label)

    asked = {
        (_moment(int(s.params["startTime"])), _moment(int(s.params["endTime"])))
        for s in fake.calls(label)
    }
    stored = (
        await session.execute(
            text(
                "select window_from, window_to, page from finance.binance_dividends_responses"
                " where connection_id = :c"
            ),
            {"c": connection.id},
        )
    ).all()
    assert {(row.window_from, row.window_to) for row in stored} == asked
    assert {row.page for row in stored} == {0}


async def test_trades_page_until_short_page(session, run, fake, clock) -> None:
    fake.add("my-trades", *[build_row("my-trades", i, time=NOW_MS - i) for i in range(1001)])
    connection = await binance_connection(run)

    async with fake.http() as http:
        ctx = await context(session, connection.id, http)
        ids = await walk_trades(ctx, client_for(clock, http), "my-trades", ["AAABBB"])

    assert len(set(ids)) == 1001
    calls = fake.calls("my-trades")
    assert [s.params["fromId"] for s in calls] == ["0", str(1000 + 1000)]
    cursors = (await _config(session, connection))["cursors"]
    assert cursors["my-trades:AAABBB"] == 1000 + 1000

    # The next walk asks after the last trade only.
    fake.requests.clear()
    async with fake.http() as http:
        ctx = await context(session, connection.id, http)
        assert await walk_trades(ctx, client_for(clock, http), "my-trades", ["AAABBB"]) == []
    assert [s.params["fromId"] for s in fake.calls("my-trades")] == [str(1000 + 1001)]


async def test_rows_without_an_id_keep_their_occurrence(session, run, fake, clock) -> None:
    reward = build_row("earn-flexible-rewards", 0, time=NOW_MS - DAY_MS)
    fake.add("earn-flexible-rewards", reward, dict(reward))
    cursor = {"fetched_to": NOW_MS - 2 * DAY_MS, "backfilled_to": LAUNCH_MS, "complete": True}
    connection = await binance_connection(
        run, config={"cursors": {"earn-flexible-rewards": cursor}}
    )

    ids = await _walk(session, connection, fake, clock, "earn-flexible-rewards")

    assert len(set(ids)) == 2


def _info(*pairs: tuple[str, str]):
    return build_response(
        "exchange-info",
        [build_row("exchange-info", symbol=b + q, baseAsset=b, quoteAsset=q) for b, q in pairs],
    )


def test_trade_symbols() -> None:
    info = _info(("AAA", "BBB"), ("BBB", "STB"), ("AAA", "STB"), ("CCC", "DDD"))

    assert trade_symbols(info, set(), everything=True) == ["AAABBB", "BBBSTB", "AAASTB", "CCCDDD"]
    assert trade_symbols(info, {"AAA", "STB"}) == ["AAASTB"]
    assert trade_symbols(info, {"AAA", "BBB", "STB"}) == ["AAABBB", "BBBSTB", "AAASTB"]
    assert trade_symbols(info, set(), either_side_of="STB") == ["BBBSTB", "AAASTB"]
    assert trade_symbols(info, set()) == []


def test_record_types() -> None:
    assert record_type("deposits") == "binance.deposit"
    assert record_type("my-trades") == "binance.spot_trade"
    assert record_type("earn-flexible-rewards") == "binance.earn_flexible_reward"
    assert record_type("convert") == "binance.convert"
    assert record_type("spot-account") == "binance.spot_account"


async def test_a_walk_failing_midway_keeps_what_it_stored(session, run, fake, clock) -> None:
    fake.add("deposits", _deposit(0, NOW_MS - DAY_MS), _deposit(1, NOW_MS - 200 * DAY_MS))
    fake.fail("deposits", status=500, older_than_ms=NOW_MS - 100 * DAY_MS)
    connection = await binance_connection(run)

    with pytest.raises(Exception, match="HTTP 500"):
        await _walk(session, connection, fake, clock, "deposits")

    cursor = (await _config(session, connection))["cursors"]["deposits"]
    assert cursor["complete"] is False
    assert cursor["backfilled_to"] == NOW_MS - 89 * DAY_MS


# The sweep: every trade of the symbols in an interval, whatever its length.


async def test_a_sweep_walks_an_interval_of_any_length_a_day_at_a_time(
    session, run, fake, clock
) -> None:
    since, until = NOW_MS - 3 * DAY_MS, NOW_MS - 1
    inside = [since + hours * HOUR_MS for hours in (1, 30, 55, 70)]
    times = [since - HOUR_MS, *inside, until + HOUR_MS]
    fake.add("my-trades", *[build_row("my-trades", i, time=at) for i, at in enumerate(times)])
    connection = await binance_connection(run, config={"cursors": {"my-trades:AAABBB": 1_005}})

    async with fake.http() as http:
        ctx = await context(session, connection.id, http)
        client = client_for(clock, http)
        ids = await walk_trades(
            ctx, client, "my-trades", ["AAABBB"], since_ms=since, until_ms=until
        )

    assert len(set(ids)) == len(inside)
    calls = fake.calls("my-trades")
    asked = [(int(s.params["startTime"]), int(s.params["endTime"])) for s in calls]
    assert all("fromId" not in s.params for s in calls)
    assert asked[0][0] == since and asked[-1][1] == until
    for (_, end), (start, _) in itertools.pairwise(asked):
        assert start == end + 1
    assert all(end - start < DAY_MS for start, end in asked)
    assert len(asked) == 3
    # The sweep leaves the symbol's cursor to the walk by trade id.
    assert (await _config(session, connection))["cursors"]["my-trades:AAABBB"] == 1_005


async def test_a_full_page_of_a_sweep_window_is_split(session, run, fake, clock) -> None:
    since = NOW_MS - DAY_MS
    rows = [build_row("my-trades", i, time=since + i * 60_000) for i in range(1001)]
    fake.add("my-trades", *rows)
    connection = await binance_connection(run)

    async with fake.http() as http:
        ctx = await context(session, connection.id, http)
        ids = await walk_trades(
            ctx,
            client_for(clock, http),
            "my-trades",
            ["AAABBB"],
            since_ms=since,
            until_ms=NOW_MS - 1,
        )

    assert len(set(ids)) == 1001
    assert len(fake.calls("my-trades")) == 3


async def test_a_full_page_in_one_millisecond_fails_the_sweep(session, run, fake, clock) -> None:
    at = NOW_MS - HOUR_MS
    fake.add("my-trades", *[build_row("my-trades", i, time=at) for i in range(1000)])
    connection = await binance_connection(run)

    async with fake.http() as http:
        ctx = await context(session, connection.id, http)
        with pytest.raises(DisplayableError, match="AAABBB"):
            await walk_trades(
                ctx, client_for(clock, http), "my-trades", ["AAABBB"], since_ms=at, until_ms=at
            )


# A row that cannot be typed is never stored: every stored raw record can be normalised.


@pytest.mark.parametrize("trades", [False, True])
async def test_a_row_that_cannot_be_typed_fails_its_page_and_stores_nothing(
    session, run, fake, clock, trades: bool
) -> None:
    connection = await binance_connection(run, config=later_config())
    label = "my-trades" if trades else "deposits"
    if trades:
        fake.add(label, build_row(label, symbol="AAABBB", time=10**20))
    else:
        fake.add(label, build_row(label, insertTime=NOW_MS - DAY_MS, completeTime=10**20))
    before = (await get_connection(session, connection.id)).config.get(CURSORS_KEY)

    with pytest.raises(BinanceDataError) as caught:
        async with fake.http() as http:
            ctx = await context(session, connection.id, http)
            client = client_for(clock, http)
            if trades:
                await walk_trades(ctx, client, label, ["AAABBB"])
            else:
                await walk(ctx, client, label)

    field = "[].time" if trades else "[].completeTime"
    assert (caught.value.label, caught.value.field) == (label, field)
    stored = await session.scalar(
        text("select count(*) from core.raw_records where connection_id = :c"),
        {"c": connection.id},
    )
    assert stored == 0
    assert (await get_connection(session, connection.id)).config.get(CURSORS_KEY) == before


async def test_a_balance_read_that_cannot_be_typed_fails(fake, clock) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        answer = fake.handler(request)
        if request.url.path != "/api/v3/account":
            return answer
        body = json.loads(answer.content)
        return httpx.Response(200, content=json.dumps({**body, "updateTime": 10**20}).encode())

    with pytest.raises(BinanceDataError) as caught:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            await read_balances(client_for(clock, http), ["spot-account"])

    assert (caught.value.label, caught.value.field) == ("spot-account", "updateTime")


@pytest.mark.parametrize(
    ("row", "field"),
    [
        ({"free": "1e999999999"}, "[].free"),
        ({"free": "1e-99999"}, "[].free"),
    ],
)
async def test_a_value_its_column_cannot_hold_fails_the_read(fake, clock, row, field) -> None:
    # Postgres' numeric holds at most 131072 digits before the point and 16383 after it.
    fake.set_rows("funding-asset", [build_row("funding-asset", asset="AAA", **row)])

    with pytest.raises(BinanceDataError) as caught:
        async with fake.http() as http:
            await read_balances(client_for(clock, http), ["funding-asset"])

    assert (caught.value.label, caught.value.field) == ("funding-asset", field)


async def test_an_integer_its_column_cannot_hold_fails_the_walk(session, run, fake, clock) -> None:
    fake.add("deposits", build_row("deposits", insertTime=NOW_MS - DAY_MS, transferType=2**63))
    connection = await binance_connection(run, config=later_config())

    with pytest.raises(BinanceDataError) as caught:
        await _walk(session, connection, fake, clock, "deposits")

    assert (caught.value.label, caught.value.field) == ("deposits", "[].transferType")
