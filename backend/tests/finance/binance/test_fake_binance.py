from collections.abc import Mapping

import pytest

from lararium.core.secrets import Secret
from lararium.modules.finance.binance.catalogue import ENDPOINTS
from lararium.modules.finance.binance.client import (
    BinanceClient,
    BinanceError,
    BinanceNotPresent,
    HmacSigner,
)
from lararium.modules.finance.binance.parse import ApiValue, parse
from tests.finance.binance.fixtures import (
    BASE_TIME_MS,
    DAY_MS,
    HOUR_MS,
    NOW_MS,
    FakeBinance,
    Rowdict,
    build_row,
)

SECRET = "sk-fake-1234"


class Clock:
    """A local clock that sleeping advances, so the limiter never really waits."""

    def __init__(self) -> None:
        self.now = NOW_MS

    def read(self) -> int:
        return self.now

    async def sleep(self, ms: int) -> None:
        self.now += ms


def client_for(fake: FakeBinance, clock: Clock | None = None) -> BinanceClient:
    clock = clock or Clock()
    return BinanceClient(
        fake.http(),
        Secret("ak-fake-5678"),
        HmacSigner(Secret(SECRET)),
        clock=clock.read,
        sleep=clock.sleep,
    )


async def ask(
    fake: FakeBinance, label: str, params: Mapping[str, str | int | bool] | None = None
) -> ApiValue:
    return await client_for(fake).call(ENDPOINTS[label], params or {})


@pytest.mark.parametrize("label", sorted(ENDPOINTS))
async def test_every_label_answers_in_its_catalogue_shape(label: str) -> None:
    fake = FakeBinance()
    # The parameters the catalogue always sends: Binance refuses the query without them.
    always = {name: values[0] for name, values in ENDPOINTS[label].variants}
    body = await ask(fake, label, always)
    parsed = parse(label, body)

    assert parsed.unknown_fields == set() and parsed.unknown_enums == set()
    assert len(parsed.rows) == (1 if ENDPOINTS[label].rows == "" else 0)
    assert [seen.label for seen in fake.requests] == [label]


async def test_stored_rows_come_back_oldest_first_in_the_labels_envelope() -> None:
    fake = FakeBinance()
    rows = [build_row("dividends", seq) for seq in (2, 0, 1)]
    fake.add("dividends", *rows)

    parsed = parse("dividends", await ask(fake, "dividends"))
    assert [r["id"] for r in parsed.rows] == [1_000, 1_001, 1_002]
    assert isinstance(parsed.body, dict) and parsed.body["total"] == 3


async def test_the_time_window_is_honoured_and_inclusive() -> None:
    fake = FakeBinance()
    fake.add("deposits", *[build_row("deposits", seq) for seq in range(5)])
    low = BASE_TIME_MS + HOUR_MS
    high = BASE_TIME_MS + 3 * HOUR_MS

    parsed = parse(
        "deposits",
        await ask(fake, "deposits", {"includeSource": "true", "startTime": low, "endTime": high}),
    )
    assert [r["id"] for r in parsed.rows] == ["1001", "1002", "1003"]
    only_from = parse(
        "deposits", await ask(fake, "deposits", {"includeSource": "true", "startTime": high})
    )
    assert [r["id"] for r in only_from.rows] == ["1003", "1004"]
    nothing = parse(
        "deposits",
        await ask(fake, "deposits", {"includeSource": "true", "startTime": 1, "endTime": 2}),
    )
    assert nothing.rows == []


async def test_a_text_time_is_windowed_too() -> None:
    fake = FakeBinance()
    fake.add("withdrawals", *[build_row("withdrawals", seq) for seq in range(3)])
    parsed = parse(
        "withdrawals",
        await ask(
            fake,
            "withdrawals",
            {"startTime": BASE_TIME_MS + HOUR_MS, "endTime": BASE_TIME_MS + HOUR_MS},
        ),
    )
    assert [r["id"] for r in parsed.rows] == ["1001"]


async def test_a_window_wider_than_the_endpoint_allows_is_an_error_not_a_refusal() -> None:
    fake = FakeBinance()
    window = ENDPOINTS["deposits"].window_days
    assert window == 89
    start = BASE_TIME_MS
    await ask(
        fake,
        "deposits",
        {"includeSource": "true", "startTime": start, "endTime": start + 89 * DAY_MS},
    )
    with pytest.raises(BinanceError) as caught:
        await ask(
            fake,
            "deposits",
            {"includeSource": "true", "startTime": start, "endTime": start + 90 * DAY_MS},
        )
    # Not the code a walk reads as "this window is not available".
    assert not isinstance(caught.value, BinanceNotPresent) and caught.value.code == -1127


async def test_offset_paging() -> None:
    fake = FakeBinance()
    fake.add("deposits", *[build_row("deposits", seq) for seq in range(5)])

    first = parse(
        "deposits", await ask(fake, "deposits", {"includeSource": "true", "limit": 2, "offset": 0})
    )
    second = parse(
        "deposits", await ask(fake, "deposits", {"includeSource": "true", "limit": 2, "offset": 2})
    )
    last = parse(
        "deposits", await ask(fake, "deposits", {"includeSource": "true", "limit": 2, "offset": 4})
    )
    assert [len(p.rows) for p in (first, second, last)] == [2, 2, 1]
    assert [r["id"] for p in (first, second, last) for r in p.rows] == [
        "1000",
        "1001",
        "1002",
        "1003",
        "1004",
    ]


async def test_page_paging_counts_from_the_endpoints_first_page_and_reports_the_total() -> None:
    fake = FakeBinance()
    fake.add(
        "earn-flexible-subscriptions",
        *[build_row("earn-flexible-subscriptions", i) for i in range(5)],
    )

    pages = [
        parse(
            "earn-flexible-subscriptions",
            await ask(fake, "earn-flexible-subscriptions", {"current": n, "size": 2}),
        )
        for n in (1, 2, 3, 4)
    ]
    assert [len(p.rows) for p in pages] == [2, 2, 1, 0]
    assert isinstance(pages[0].body, dict) and pages[0].body["total"] == 5

    fiat = FakeBinance()
    fiat.add("fiat-orders", *[build_row("fiat-orders", i) for i in range(3)])
    asked = {"transactionType": 0, "page": 1, "rows": 2}
    got = parse("fiat-orders", await ask(fiat, "fiat-orders", asked))
    assert len(got.rows) == 2


async def test_page_size_is_capped_at_the_endpoints_maximum() -> None:
    fake = FakeBinance()
    fake.add(
        "earn-flexible-subscriptions",
        *[build_row("earn-flexible-subscriptions", i) for i in range(150)],
    )
    parsed = parse(
        "earn-flexible-subscriptions",
        await ask(fake, "earn-flexible-subscriptions", {"current": 1, "size": 500}),
    )
    assert len(parsed.rows) == ENDPOINTS["earn-flexible-subscriptions"].page_size == 100


async def test_an_unsized_request_gets_the_documented_default_page() -> None:
    fake = FakeBinance()
    fake.add(
        "earn-flexible-subscriptions",
        *[build_row("earn-flexible-subscriptions", i) for i in range(15)],
    )
    parsed = parse("earn-flexible-subscriptions", await ask(fake, "earn-flexible-subscriptions"))
    assert len(parsed.rows) == 10


async def test_from_id_paging_returns_ids_from_the_cursor() -> None:
    fake = FakeBinance()
    fake.add("my-trades", *[build_row("my-trades", seq) for seq in range(5)])

    first = parse("my-trades", await ask(fake, "my-trades", {"symbol": "AAABBB", "limit": 2}))
    after = parse(
        "my-trades", await ask(fake, "my-trades", {"symbol": "AAABBB", "fromId": 1002, "limit": 2})
    )
    assert [r["id"] for r in first.rows] == [1_000, 1_001]
    assert [r["id"] for r in after.rows] == [1_002, 1_003]


async def test_a_symbol_filter_picks_that_symbols_rows() -> None:
    fake = FakeBinance()
    fake.add("my-trades", build_row("my-trades", 0), build_row("my-trades", 1, symbol="BBBSTB"))
    parsed = parse("my-trades", await ask(fake, "my-trades", {"symbol": "BBBSTB"}))
    assert [r["symbol"] for r in parsed.rows] == ["BBBSTB"]


async def test_limit_on_an_unpaged_endpoint_cuts_the_answer_and_says_more() -> None:
    fake = FakeBinance()
    fake.add("convert", *[build_row("convert", seq) for seq in range(3)])

    cut = await ask(
        fake,
        "convert",
        {"startTime": BASE_TIME_MS, "endTime": BASE_TIME_MS + 10 * HOUR_MS, "limit": 2},
    )
    assert (
        isinstance(cut, dict) and cut["moreData"] is True and len(parse("convert", cut).rows) == 2
    )
    all_of_it = await ask(fake, "convert", {"limit": 5})
    assert isinstance(all_of_it, dict) and all_of_it["moreData"] is False


async def test_a_row_can_answer_only_for_given_request_parameters() -> None:
    fake = FakeBinance()
    fake.add("fiat-orders", build_row("fiat-orders", 0), when={"transactionType": "0"})
    fake.add("fiat-orders", build_row("fiat-orders", 1), when={"transactionType": "1"})

    deposits = parse("fiat-orders", await ask(fake, "fiat-orders", {"transactionType": 0}))
    withdrawals = parse("fiat-orders", await ask(fake, "fiat-orders", {"transactionType": 1}))
    assert [r["orderNo"] for r in deposits.rows] == ["1000"]
    assert [r["orderNo"] for r in withdrawals.rows] == ["1001"]


async def test_the_gate_answers_from_the_permissions_dict() -> None:
    fake = FakeBinance()
    first = await ask(fake, "api-restrictions")
    assert isinstance(first, dict)
    assert first["enableReading"] is True and first["enableWithdrawals"] is False

    fake.permissions["enableWithdrawals"] = True
    fake.permissions["enableReading"] = False
    again = await ask(fake, "api-restrictions")
    assert isinstance(again, dict)
    assert again["enableWithdrawals"] is True and again["enableReading"] is False
    assert parse("api-restrictions", again).unknown_fields == set()


async def test_time_comes_from_the_injected_clock() -> None:
    server = {"now": 5_000_000}
    fake = FakeBinance(now=lambda: server["now"])
    assert await ask(fake, "time") == {"serverTime": 5_000_000}
    server["now"] += 1_000
    assert await ask(fake, "time") == {"serverTime": 5_001_000}


async def test_a_label_can_be_told_to_fail_with_a_binance_code() -> None:
    fake = FakeBinance()
    fake.fail("margin-account", code=-3003)
    with pytest.raises(BinanceNotPresent) as caught:
        await ask(fake, "margin-account")
    assert caught.value.code == -3003
    await ask(fake, "spot-account")  # other labels are untouched


async def test_a_label_can_be_told_to_fail_with_an_http_status() -> None:
    fake = FakeBinance()
    fake.fail("spot-account", status=500)
    with pytest.raises(BinanceError) as caught:
        await ask(fake, "spot-account")
    assert caught.value.http_status == 500 and caught.value.code is None


async def test_a_failure_can_be_limited_to_windows_older_than_a_time() -> None:
    fake = FakeBinance()
    cutoff = NOW_MS - 180 * DAY_MS
    fake.fail("universal-transfer", code=-5026, older_than_ms=cutoff)

    await ask(fake, "universal-transfer", {"type": "MAIN_FUNDING", "startTime": cutoff})
    await ask(fake, "universal-transfer", {"type": "MAIN_FUNDING"})
    with pytest.raises(BinanceNotPresent):
        await ask(fake, "universal-transfer", {"type": "MAIN_FUNDING", "startTime": cutoff - 1})


async def test_a_failure_can_happen_a_given_number_of_times() -> None:
    fake = FakeBinance()
    fake.fail("spot-account", status=500, times=2)
    for _ in range(2):
        with pytest.raises(BinanceError):
            await ask(fake, "spot-account")
    await ask(fake, "spot-account")
    fake.fail("spot-account", status=500)
    fake.clear_failures()
    await ask(fake, "spot-account")


async def test_a_429_carries_retry_after_and_the_client_waits_then_succeeds() -> None:
    fake = FakeBinance()
    fake.fail("spot-account", status=429, retry_after=3, times=1)
    clock = Clock()
    await client_for(fake, clock).call(ENDPOINTS["spot-account"])
    assert len(fake.calls("spot-account")) == 2
    assert clock.now - NOW_MS >= 3_000


async def test_every_request_is_recorded_without_the_signing_parameters() -> None:
    fake = FakeBinance()
    await ask(fake, "deposits", {"includeSource": "true", "startTime": 5, "endTime": 9, "limit": 1})
    await ask(fake, "funding-asset", {"asset": "AAA"})
    await ask(fake, "time")

    assert [(s.label, s.method) for s in fake.requests] == [
        ("deposits", "GET"),
        ("funding-asset", "POST"),
        ("time", "GET"),
    ]
    assert fake.calls("deposits")[0].params == {
        "includeSource": "true",
        "startTime": "5",
        "endTime": "9",
        "limit": "1",
    }
    assert fake.calls("funding-asset")[0].params == {"asset": "AAA"}


async def test_a_wrong_signature_is_refused_when_the_fake_knows_the_secret() -> None:
    right = FakeBinance(secret=SECRET)
    await ask(right, "spot-account")
    wrong = FakeBinance(secret="another-secret")
    with pytest.raises(BinanceError) as caught:
        await ask(wrong, "spot-account")
    assert caught.value.code == -1022


async def test_a_single_record_label_answers_with_what_is_stored() -> None:
    fake = FakeBinance()
    mine: Rowdict = build_row("earn-account", totalAmountInBTC="9.5")
    fake.set_rows("earn-account", [mine])
    body = await ask(fake, "earn-account")
    assert isinstance(body, dict) and body["totalAmountInBTC"] == "9.5"
    assert fake.rows("earn-account") == [mine]


async def test_an_override_naming_no_field_is_an_error() -> None:
    with pytest.raises(KeyError):
        build_row("deposits", amunt="1")


async def test_trades_are_asked_for_at_most_a_day_at_a_time() -> None:
    # The catalogue's my-trades: "The time between startTime and endTime can't be longer than
    # 24 hours"; a start alone is the 24 hours from it; a time cannot go with fromId.
    fake = FakeBinance()
    start = BASE_TIME_MS
    fake.add(
        "my-trades",
        *[build_row("my-trades", seq, time=start + seq * 12 * HOUR_MS) for seq in range(4)],
    )
    day = {"symbol": "AAABBB", "startTime": start, "endTime": start + DAY_MS}
    assert [r["id"] for r in parse("my-trades", await ask(fake, "my-trades", day)).rows] == [
        1_000,
        1_001,
        1_002,
    ]
    from_start = {"symbol": "AAABBB", "startTime": start + 12 * HOUR_MS}
    assert [r["id"] for r in parse("my-trades", await ask(fake, "my-trades", from_start)).rows] == [
        1_001,
        1_002,
        1_003,
    ]
    for params in (
        {"symbol": "AAABBB", "startTime": start, "endTime": start + DAY_MS + 1},
        {"symbol": "AAABBB", "startTime": start, "fromId": 1_000},
    ):
        with pytest.raises(BinanceError) as caught:
            await ask(fake, "my-trades", params)
        assert not isinstance(caught.value, BinanceNotPresent)
        assert caught.value.code in (-1127, -1128)


async def test_a_query_without_a_parameter_the_catalogue_always_sends_is_refused() -> None:
    with pytest.raises(BinanceError) as caught:
        await ask(FakeBinance(), "earn-flexible-rewards")
    assert caught.value.code == -1102
