import base64
import dataclasses
import hashlib
import hmac
import itertools
import json
from collections import deque
from decimal import Decimal
from urllib.parse import parse_qsl, unquote

import httpx
import pytest
from cryptography.exceptions import UnsupportedAlgorithm
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from lararium import __version__
from lararium.core.errors import CredentialRejectedError
from lararium.core.secrets import Secret
from lararium.modules.finance.binance.catalogue import ENDPOINTS, Endpoint
from lararium.modules.finance.binance.client import (
    ALLOWED,
    BinanceBanned,
    BinanceClient,
    BinanceError,
    BinanceKeyRejected,
    BinanceKeyTypeRefused,
    BinanceNotPresent,
    Ed25519Signer,
    HmacSigner,
    Limiter,
    RequestRefused,
    Signer,
    key_type_flag,
)

from .syncing import ED25519_PEM

API_KEY = "ak-5c1d9e7f3b2a4680"
SECRET = "sk-0f9e8d7c6b5a4938"
START_MS = 1_700_000_040_000  # a whole minute, so "the rest of the minute" is a round number

TIME = ENDPOINTS["time"]
SPOT = ENDPOINTS["spot-account"]
FUNDING = ENDPOINTS["funding-asset"]
WITHDRAWALS = ENDPOINTS["withdrawals"]


def ok(body: str = "{}", **headers: str) -> httpx.Response:
    return httpx.Response(200, content=body.encode(), headers=headers)


def error(status: int, code: int | None = None, **headers: str) -> httpx.Response:
    body = json.dumps({"code": code, "msg": "x"}) if code is not None else ""
    return httpx.Response(status, content=body.encode(), headers=headers)


class Env:
    """A fake clock in milliseconds that sleeping advances, and a transport that records."""

    def __init__(self, *responses: httpx.Response | Exception, repeat_last: bool = False) -> None:
        self.now = START_MS
        self.sleeps: list[int] = []
        self.requests: list[httpx.Request] = []
        self._responses = deque(responses)
        self._repeat_last = repeat_last

    async def sleep(self, ms: int) -> None:
        self.sleeps.append(ms)
        self.now += ms

    def clock(self) -> int:
        return self.now

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        reply = self._responses[0] if self._repeat_last else self._responses.popleft()
        if isinstance(reply, Exception):
            raise reply
        return reply

    def http(self) -> httpx.AsyncClient:
        # follow_redirects=True: the client must still refuse to follow one.
        return httpx.AsyncClient(transport=httpx.MockTransport(self.handler), follow_redirects=True)

    def client(self, signer: Signer | None = None) -> BinanceClient:
        return BinanceClient(
            self.http(),
            Secret(API_KEY),
            signer or HmacSigner(Secret(SECRET)),
            clock=self.clock,
            sleep=self.sleep,
        )


def query_of(request: httpx.Request) -> str:
    return request.url.query.decode()


def test_hmac_signature_matches_binance_example() -> None:
    signer = HmacSigner(Secret("NhqPtmdSJYdKjVHjA7PZj4Mge3R5YNiP1e3UZjInClVN65XAbvqqM6A7H5fATj0j"))
    payload = (
        "symbol=LTCBTC&side=BUY&type=LIMIT&timeInForce=GTC&quantity=1&price=0.1"
        "&recvWindow=5000&timestamp=1499827319559"
    )
    assert signer.sign(payload) == (
        "c8db56825ae71d6d79447849e617115f4a920fa2acdcab2b053c4b2838bd6b71"
    )


@pytest.mark.parametrize("passphrase", [None, "pp-77aa"])
async def test_ed25519_signature_verifies(passphrase: str | None) -> None:
    key = Ed25519PrivateKey.generate()
    encryption: serialization.KeySerializationEncryption = (
        serialization.BestAvailableEncryption(passphrase.encode())
        if passphrase
        else serialization.NoEncryption()
    )
    pem = key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, encryption
    ).decode()
    signer = Ed25519Signer(Secret(pem), Secret(passphrase) if passphrase else None)
    env = Env(ok())
    await env.client(signer).call(SPOT)

    query = query_of(env.requests[0])
    signed, _, signature = query.rpartition("&signature=")
    assert signature and "&" not in signature
    key.public_key().verify(base64.b64decode(unquote(signature)), signed.encode())


async def test_payload_is_percent_encoded_before_signing() -> None:
    env = Env(ok())
    await env.client().call(SPOT, {"note": "a b&c"})

    query = query_of(env.requests[0])
    signed, _, signature = query.rpartition("&signature=")
    assert "note=a%20b%26c" in signed
    expected = hmac.new(SECRET.encode(), signed.encode(), hashlib.sha256).hexdigest()
    assert signature == expected


async def test_signed_request_shape() -> None:
    env = Env(ok())
    await env.client().call(SPOT, {"omitZeroBalances": True})

    request = env.requests[0]
    assert request.method == "GET"
    assert request.url.scheme == "https" and request.url.host == "api.binance.com"
    assert request.url.path == "/api/v3/account"
    assert request.headers["X-MBX-APIKEY"] == API_KEY
    assert request.headers["User-Agent"] == f"Lararium/{__version__}"
    pairs = parse_qsl(query_of(request))
    assert [name for name, _ in pairs] == [
        "omitZeroBalances",
        "recvWindow",
        "timestamp",
        "signature",
    ]
    values = dict(pairs)
    assert values["omitZeroBalances"] == "true"
    assert values["recvWindow"] == "10000"
    assert values["timestamp"] == str(START_MS)


async def test_unsigned_request_carries_neither_key_nor_signature() -> None:
    env = Env(ok('{"serverTime": 1}'))
    await env.client().call(TIME)

    request = env.requests[0]
    assert "X-MBX-APIKEY" not in request.headers
    assert query_of(request) == ""


async def test_sync_clock_measures_the_offset_and_now_ms_applies_it() -> None:
    env = Env(ok(json.dumps({"serverTime": START_MS + 5_000})), ok())
    client = env.client()
    assert client.now_ms() == START_MS
    await client.sync_clock()
    assert client.now_ms() == START_MS + 5_000

    await client.call(SPOT)
    # The limiter's 100 ms spacing moved the fake clock before the request was signed.
    assert dict(parse_qsl(query_of(env.requests[1])))["timestamp"] == str(env.now + 5_000)


async def test_numbers_are_parsed_as_decimal() -> None:
    env = Env(ok('{"a": 0.1, "b": 12, "c": "7.50", "d": [1.25]}'))
    body = await env.client().call(SPOT)
    assert body == {"a": Decimal("0.1"), "b": 12, "c": "7.50", "d": [Decimal("1.25")]}
    assert isinstance(body, dict) and isinstance(body["a"], Decimal)


@pytest.mark.parametrize("body", ["not json", "", "NaN", '{"a": Infinity}'])
async def test_an_unreadable_body_is_a_binance_error(body: str) -> None:
    env = Env(ok(body))
    with pytest.raises(BinanceError) as caught:
        await env.client().call(SPOT)
    assert caught.value.http_status == 200


@pytest.mark.parametrize(
    "refused",
    [
        pytest.param(dataclasses.replace(SPOT, host="example.invalid"), id="other-host"),
        pytest.param(dataclasses.replace(SPOT, host="http://api.binance.com"), id="http"),
        pytest.param(dataclasses.replace(SPOT, path="/api/v3/order"), id="unlisted-path"),
        pytest.param(dataclasses.replace(SPOT, method="DELETE"), id="delete-on-listed-path"),
        pytest.param(dataclasses.replace(WITHDRAWALS, method="POST"), id="post-on-a-get"),
    ],
)
async def test_refuses_everything_outside_the_allowlist(refused: Endpoint) -> None:
    env = Env(ok(), repeat_last=True)
    assert refused not in ENDPOINTS.values()
    with pytest.raises(RequestRefused):
        await env.client().call(refused)
    assert env.requests == []


async def test_a_reserved_parameter_is_refused() -> None:
    env = Env(ok())
    with pytest.raises(RequestRefused):
        await env.client().call(SPOT, {"signature": "x"})
    assert env.requests == []


def test_only_the_two_asset_queries_are_posts() -> None:
    assert {path for method, _, path in ALLOWED if method == "POST"} == {
        "/sapi/v1/asset/get-funding-asset",
        "/sapi/v3/asset/getUserAsset",
    }
    assert {host for _, host, _ in ALLOWED} <= {
        "api.binance.com",
        "fapi.binance.com",
        "dapi.binance.com",
        "eapi.binance.com",
        "papi.binance.com",
    }


async def test_post_queries_send_parameters_in_the_query() -> None:
    env = Env(ok("[]"))
    await env.client().call(FUNDING, {"asset": "AAA", "needBtcValuation": False})

    request = env.requests[0]
    assert request.method == "POST"
    assert request.content == b""
    values = dict(parse_qsl(query_of(request)))
    assert values["asset"] == "AAA" and values["needBtcValuation"] == "false"
    assert "signature" in values


async def test_redirect_is_not_followed() -> None:
    env = Env(
        httpx.Response(302, headers={"Location": "https://example.invalid/steal"}),
        ok(),
    )
    with pytest.raises(BinanceError) as caught:
        await env.client().call(SPOT)
    assert caught.value.http_status == 302
    assert [r.url.host for r in env.requests] == ["api.binance.com"]


@pytest.mark.parametrize(
    ("status", "code", "kind", "message"),
    [
        (401, None, BinanceKeyRejected, "Binance rejected the key (code 401)"),
        (401, -2015, BinanceKeyRejected, "Binance rejected the key (code -2015)"),
        (401, -1022, BinanceKeyRejected, "Binance rejected the key (code -1022)"),
        (400, -2014, BinanceKeyRejected, "Binance rejected the key (code -2014)"),
        (400, -2015, BinanceKeyRejected, "Binance rejected the key (code -2015)"),
        (400, -1022, BinanceKeyRejected, "Binance rejected the key (code -1022)"),
        (418, None, BinanceBanned, None),
        (418, -1003, BinanceBanned, None),
        (404, None, BinanceNotPresent, None),
        *[
            (400, code, BinanceNotPresent, None)
            for code in (-3003, -30105, -6021, -5026, -4047, 403004)
        ],
        (400, -1121, BinanceError, None),
        (500, None, BinanceError, None),
        (503, -1001, BinanceError, None),
    ],
)
async def test_error_mapping(
    status: int, code: int | None, kind: type[BinanceError], message: str | None
) -> None:
    env = Env(error(status, code))
    with pytest.raises(BinanceError) as caught:
        await env.client().call(SPOT)
    assert type(caught.value) is kind
    assert caught.value.http_status == status
    if kind is BinanceKeyRejected:
        assert isinstance(caught.value, CredentialRejectedError)
    if kind is BinanceNotPresent and code is not None:
        assert caught.value.code == code
    if message is not None:
        assert str(caught.value) == message
    assert env.sleeps == []


def _ed25519() -> Ed25519Signer:
    return Ed25519Signer(Secret(ED25519_PEM), None)


@pytest.mark.parametrize("label", ["um-account", "cm-account", "opt-account"])
@pytest.mark.parametrize(("status", "code"), [(401, None), (400, -1022), (401, -1022)])
async def test_ed25519_refused_off_api_after_api_accepted_it_is_the_hosts_refusal(
    label: str, status: int, code: int | None
) -> None:
    endpoint = ENDPOINTS[label]
    env = Env(ok(), error(status, code))
    client = env.client(_ed25519())
    await client.call(SPOT)
    with pytest.raises(BinanceKeyTypeRefused) as caught:
        await client.call(endpoint)
    assert not isinstance(caught.value, CredentialRejectedError)
    assert (caught.value.host, caught.value.limit) == (endpoint.host, endpoint.limit)
    assert str(caught.value) == key_type_flag(endpoint.host)
    assert str(caught.value) == (
        f"{endpoint.host} does not accept this key type; use an HMAC key to include it"
    )


@pytest.mark.parametrize(
    ("signer", "accepted", "label", "status", "code"),
    [
        # An HMAC key refused anywhere is a rejected key.
        ("hmac", True, "opt-account", 401, None),
        ("hmac", True, "um-account", 400, -1022),
        # Before `api` accepted the Ed25519 key, the key itself is in question.
        ("ed25519", False, "opt-account", 401, None),
        # Refused on `api` itself.
        ("ed25519", True, "spot-account", 400, -1022),
        # Another rejection code is about the key, not its type, even with HTTP 401.
        ("ed25519", True, "um-account", 400, -2015),
        ("ed25519", True, "um-account", 401, -2015),
        ("ed25519", True, "opt-account", 401, -2014),
    ],
)
async def test_other_refusals_stay_a_rejected_key(
    signer: str, accepted: bool, label: str, status: int, code: int | None
) -> None:
    env = Env(*([ok()] if accepted else []), error(status, code))
    client = env.client(_ed25519() if signer == "ed25519" else None)
    if accepted:
        await client.call(SPOT)
    with pytest.raises(BinanceKeyRejected):
        await client.call(ENDPOINTS[label])


@pytest.mark.parametrize("signer", ["hmac", "ed25519"])
@pytest.mark.parametrize("label", ["um-exchange-info", "opt-exchange-info", "time"])
async def test_an_unsigned_request_refused_says_nothing_of_the_key(signer: str, label: str) -> None:
    env = Env(ok(), error(401))
    client = env.client(_ed25519() if signer == "ed25519" else None)
    await client.call(SPOT)
    with pytest.raises(BinanceError) as caught:
        await client.call(ENDPOINTS[label])
    assert type(caught.value) is BinanceError
    assert caught.value.http_status == 401


async def test_the_options_hosts_no_access_code_is_not_present_there_only() -> None:
    env = Env(error(400, -5002), error(400, -5002))
    client = env.client()
    with pytest.raises(BinanceNotPresent) as caught:
        await client.call(ENDPOINTS["opt-account"])
    assert caught.value.code == -5002
    with pytest.raises(BinanceError) as other:
        await client.call(SPOT)
    assert type(other.value) is BinanceError


async def test_a_transport_failure_is_a_binance_error_without_the_url() -> None:
    env = Env(httpx.ConnectError(f"cannot reach https://api.binance.com/?x={API_KEY}"))
    with pytest.raises(BinanceError) as caught:
        await env.client().call(SPOT)
    assert caught.value.http_status is None
    assert "binance.com" not in str(caught.value) and API_KEY not in str(caught.value)


async def test_a_failed_query_is_named_by_its_label() -> None:
    env = Env(error(400, -1102))
    with pytest.raises(BinanceError) as caught:
        await env.client().call(SPOT)
    assert str(caught.value) == f"Binance request failed (HTTP 400, code -1102), {SPOT.label}"
    assert caught.value.label == SPOT.label


async def test_retries_429_then_gives_up() -> None:
    env = Env(
        httpx.Response(429, headers={"Retry-After": "2"}),
        httpx.Response(429, headers={"Retry-After": "2"}),
        ok('{"v": 1}'),
    )
    assert await env.client().call(SPOT) == {"v": 1}
    assert env.sleeps == [2000, 2000]
    assert len(env.requests) == 3

    env = Env(httpx.Response(429, headers={"Retry-After": "2"}), repeat_last=True)
    with pytest.raises(BinanceError) as caught:
        await env.client().call(SPOT)
    assert caught.value.http_status == 429
    assert len(env.requests) == 4
    assert env.sleeps == [2000, 2000, 2000]


@pytest.mark.parametrize(
    ("headers", "slept"),
    [({}, 30_000), ({"Retry-After": "0"}, 1_000), ({"Retry-After": "9999"}, 300_000)],
)
async def test_retry_after_is_clamped_with_a_default(headers: dict[str, str], slept: int) -> None:
    env = Env(httpx.Response(429, headers=headers), ok())
    await env.client().call(SPOT)
    assert env.sleeps == [slept]


async def test_each_retry_is_signed_with_a_fresh_timestamp() -> None:
    env = Env(httpx.Response(429, headers={"Retry-After": "2"}), ok())
    await env.client().call(SPOT)
    stamps = [dict(parse_qsl(query_of(r)))["timestamp"] for r in env.requests]
    assert stamps == [str(START_MS), str(START_MS + 2_000)]


async def test_limiter_keeps_each_bucket_under_its_cap() -> None:
    env = Env()
    limiter = Limiter(env.clock, env.sleep)
    sent: list[tuple[int, int]] = []
    for _ in range(30):
        await limiter.acquire("uid:/x", 18_000, 180_000)
        sent.append((env.now, 18_000))

    for at, _ in sent:
        in_window = sum(weight for t, weight in sent if at - 60_000 < t <= at)
        assert in_window <= 144_000
    assert env.now - START_MS >= 3 * 60_000 - 60_000  # it did wait, rather than burst


async def test_limiter_spaces_any_two_requests_by_100_ms() -> None:
    env = Env()
    limiter = Limiter(env.clock, env.sleep)
    times = []
    for bucket in ("a", "b", "a"):
        await limiter.acquire(bucket, 1, 6000)
        times.append(env.now)
    assert [b - a for a, b in itertools.pairwise(times)] == [100, 100]


async def test_limiter_blocks_a_bucket_until_the_block_ends() -> None:
    env = Env()
    limiter = Limiter(env.clock, env.sleep)
    limiter.block("a", 40)
    await limiter.acquire("b", 1, 6000)
    assert env.now == START_MS
    await limiter.acquire("a", 1, 6000)
    assert env.now == START_MS + 40_000


async def test_used_weight_header_blocks_the_bucket() -> None:
    # 153,000 of the 180,000 UID cap is 85%.
    env = Env(
        ok(**{"X-SAPI-USED-UID-WEIGHT-1M": "153000"}),
        ok(**{"X-SAPI-USED-UID-WEIGHT-1M": "10"}),
        repeat_last=False,
    )
    client = env.client()
    await client.call(WITHDRAWALS)
    assert env.sleeps == []
    await client.call(WITHDRAWALS)
    # The rest of the minute (START_MS is a whole minute, so all of it), no more.
    assert env.sleeps == [60_000]


async def test_a_used_weight_below_the_threshold_does_not_block() -> None:
    env = Env(ok(**{"X-SAPI-USED-UID-WEIGHT-1M": "152999"}), ok())
    client = env.client()
    await client.call(WITHDRAWALS)
    await client.call(WITHDRAWALS)
    assert env.sleeps == [100]  # only the spacing between two requests


async def test_the_ip_weight_header_of_the_api_pool_is_read() -> None:
    env = Env(ok(**{"X-MBX-USED-WEIGHT-1M": "5100"}), ok())
    client = env.client()
    await client.call(SPOT)
    await client.call(TIME)
    assert env.sleeps == [60_000]


async def test_a_futures_hosts_limit_is_read_from_its_exchange_information() -> None:
    income = ENDPOINTS["um-income"]  # weight 30
    env = Env(ok("[]"), repeat_last=True)
    client = env.client()
    assert client.caps["fapi"] == client.caps["dapi"] == 600
    # 600 a minute until it is read: 16 asks of weight 30 fill 80% of it, the 17th waits for
    # the minute to pass.
    for _ in range(17):
        await client.call(income)
    assert max(env.sleeps) > 50_000
    env.sleeps.clear()

    stated = {
        "rateLimits": [
            {"rateLimitType": "ORDERS", "interval": "SECOND", "intervalNum": 10, "limit": 300},
            {
                "rateLimitType": "REQUEST_WEIGHT",
                "interval": "MINUTE",
                "intervalNum": 1,
                "limit": 2400,
            },
        ]
    }
    client.read_limits(ENDPOINTS["um-exchange-info"], stated)

    assert client.caps["fapi"] == 2400 and client.caps["dapi"] == 600
    # Under 80% of 2400 with the asks of the last minute: only the spacing between requests.
    for _ in range(40):
        await client.call(income)
    assert set(env.sleeps) == {100}
    # A limit over another interval, or none, leaves the host's as it was.
    for other in (
        {
            "rateLimits": [
                {
                    "rateLimitType": "REQUEST_WEIGHT",
                    "interval": "SECOND",
                    "intervalNum": 1,
                    "limit": 50,
                }
            ]
        },
        {
            "rateLimits": [
                {
                    "rateLimitType": "REQUEST_WEIGHT",
                    "interval": "MINUTE",
                    "intervalNum": 5,
                    "limit": 50,
                }
            ]
        },
        {"rateLimits": []},
        {},
    ):
        client.read_limits(ENDPOINTS["cm-exchange-info"], other)
        assert client.caps["dapi"] == 600


async def test_messages_never_contain_key_material() -> None:
    pem = (
        Ed25519PrivateKey.generate()
        .private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
        .decode()
    )
    signers: list[Signer] = [HmacSigner(Secret(SECRET)), Ed25519Signer(Secret(pem), None)]
    replies: list[httpx.Response | Exception] = [
        error(401, -2015),
        error(418, -1003),
        error(404),
        error(400, -3003),
        error(400, -1121),
        error(500),
        httpx.Response(302, headers={"Location": f"https://example.invalid/?k={API_KEY}"}),
        ok("not json"),
        httpx.ConnectError(f"https://api.binance.com/?X-MBX-APIKEY={API_KEY}&k={SECRET}"),
    ]
    secrets = [API_KEY, SECRET, "PRIVATE KEY", pem.splitlines()[1]]
    for signer in signers:
        for reply in replies:
            env = Env(reply)
            client = env.client(signer)
            with pytest.raises(BinanceError) as caught:
                await client.call(SPOT)
            for text in (str(caught.value), repr(caught.value), repr(client), repr(signer)):
                assert not any(secret in text for secret in secrets)
                assert "http" not in text and "binance.com" not in text
    refused = Env(ok())
    with pytest.raises(RequestRefused) as caught_refused:
        await refused.client().call(dataclasses.replace(SPOT, path="/nope"))
    assert API_KEY not in str(caught_refused.value)


@pytest.mark.parametrize(
    "refused",
    [
        pytest.param(dataclasses.replace(WITHDRAWALS, signed=False), id="unsigned"),
        pytest.param(dataclasses.replace(WITHDRAWALS, weight=1), id="lighter"),
        pytest.param(dataclasses.replace(WITHDRAWALS, limit="ip"), id="another-limit"),
        pytest.param(dataclasses.replace(WITHDRAWALS, page_size=5), id="another-page-size"),
        pytest.param(dataclasses.replace(SPOT, label="spot-copy"), id="another-label"),
    ],
)
async def test_only_the_catalogues_own_endpoint_is_sent(refused: Endpoint) -> None:
    # Each one names a route the allowlist lists.
    assert (refused.method, refused.host, refused.path) in ALLOWED
    env = Env(ok(), repeat_last=True)
    with pytest.raises(RequestRefused):
        await env.client().call(refused)
    assert env.requests == []


async def test_sync_clock_leaves_out_a_429s_wait() -> None:
    env = Env(
        error(429, None, **{"Retry-After": "7"}), ok(json.dumps({"serverTime": START_MS + 9_000}))
    )
    client = env.client()
    await client.sync_clock()

    assert env.sleeps == [7_000]
    # The server's reading was taken in the second exchange, at START + 7 s locally.
    assert client.now_ms() == env.now + 2_000


async def test_a_slow_clock_measure_is_taken_again_and_the_quickest_is_used() -> None:
    """A new connection's handshake comes before the server reads its clock: halfway through
    that exchange is seconds before the reading, and a timestamp built on it is ahead."""
    env = Env()
    took = iter([4_000, 40])

    def handler(request: httpx.Request) -> httpx.Response:
        env.requests.append(request)
        spent = next(took)
        # The server reads its clock (two seconds ahead of the local one) as the exchange ends.
        env.now += spent
        return ok(json.dumps({"serverTime": env.now + 2_000}))

    env.handler = handler  # type: ignore[method-assign]
    client = env.client()
    await client.sync_clock()

    assert len(env.requests) == 2
    # Within the quick exchange's half, not the slow one's two seconds.
    assert 0 <= client.now_ms() - (env.now + 2_000) <= 20


async def test_a_refused_timestamp_is_measured_again_and_asked_once_more() -> None:
    env = Env(
        error(400, -1021),
        ok(json.dumps({"serverTime": START_MS + 3_000})),
        ok('{"v": 1}'),
    )
    client = env.client()
    assert await client.call(SPOT) == {"v": 1}
    assert [r.url.path for r in env.requests] == [SPOT.path, "/api/v3/time", SPOT.path]
    # The second measure was taken 100 ms in (the limiter's spacing): 2.9 s ahead of local.
    assert dict(parse_qsl(query_of(env.requests[2])))["timestamp"] == str(env.now + 2_900)

    env = Env(error(400, -1021), ok(json.dumps({"serverTime": START_MS})), error(400, -1021))
    with pytest.raises(BinanceError) as caught:
        await env.client().call(SPOT)
    assert caught.value.code == -1021 and len(env.requests) == 3


async def test_sync_clock_leaves_out_the_limiters_wait() -> None:
    env = Env(ok(), ok(json.dumps({"serverTime": START_MS + 1_100})))
    client = env.client()
    await client.call(SPOT)
    await client.sync_clock()

    assert env.sleeps == [100]
    assert client.now_ms() == env.now + 1_000


def _pem(key: Ed25519PrivateKey | rsa.RSAPrivateKey, passphrase: str | None = None) -> str:
    encryption: serialization.KeySerializationEncryption = (
        serialization.BestAvailableEncryption(passphrase.encode())
        if passphrase
        else serialization.NoEncryption()
    )
    return key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, encryption
    ).decode()


def _boom(*_: object, **__: object) -> None:
    raise UnsupportedAlgorithm("detail-from-the-library: key bytes 0xdeadbeef")


@pytest.mark.parametrize(
    "case",
    ["rsa", "wrong-passphrase", "missing-passphrase", "unneeded-passphrase", "garbage", "backend"],
)
def test_a_key_that_cannot_be_loaded_is_a_clean_error(
    case: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    ed = Ed25519PrivateKey.generate()
    pem, passphrase = {
        "rsa": (_pem(rsa.generate_private_key(65537, 2048)), None),
        "wrong-passphrase": (_pem(ed, "right-pp-31"), "wrong-pp-77"),
        "missing-passphrase": (_pem(ed, "right-pp-31"), None),
        "unneeded-passphrase": (_pem(ed), "some-pp-55"),
        "garbage": ("-----BEGIN PRIVATE KEY-----\nbm90IGEga2V5\n-----END PRIVATE KEY-----", None),
        "backend": (_pem(ed), None),
    }[case]
    if case == "backend":
        monkeypatch.setattr(serialization, "load_pem_private_key", _boom)

    with pytest.raises(ValueError) as caught:
        Ed25519Signer(Secret(pem), Secret(passphrase) if passphrase else None)

    error = caught.value
    assert str(error) in {
        "the Ed25519 private key can't be read",
        "the private key is not an Ed25519 key",
    }
    # Nothing the library said, and no way back to it.
    assert error.__cause__ is None
    assert error.__context__ is None or error.__suppress_context__
    shown = repr(error) + str(error.args)
    assert not any(line in shown for line in pem.splitlines()[1:-1])
    assert not any(word in shown for word in ("pp-", "deadbeef", "library", "Bad", "decrypt"))
