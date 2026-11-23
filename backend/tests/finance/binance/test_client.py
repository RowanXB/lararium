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
from cryptography.hazmat.primitives import serialization
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
    BinanceNotPresent,
    Ed25519Signer,
    HmacSigner,
    Limiter,
    RequestRefused,
    Signer,
)

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


async def test_a_transport_failure_is_a_binance_error_without_the_url() -> None:
    env = Env(httpx.ConnectError(f"cannot reach https://api.binance.com/?x={API_KEY}"))
    with pytest.raises(BinanceError) as caught:
        await env.client().call(SPOT)
    assert caught.value.http_status is None
    assert "binance.com" not in str(caught.value) and API_KEY not in str(caught.value)


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
