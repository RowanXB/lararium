"""A queries-only client for Binance's REST API.

The only requests it can send are the catalogue's (`ALLOWED`); there is no way to widen the
list. Error messages carry Binance's HTTP status and error code only: never the key, the
secret, a URL, an address or a response body.

Time is in whole milliseconds: `clock` returns the local epoch time and `sleep` takes the
milliseconds to wait, so tests drive both and no binary fraction is ever involved.
"""

import asyncio
import base64
import hashlib
import hmac
import json
import time
from collections import deque
from collections.abc import Awaitable, Callable, Mapping
from decimal import Decimal
from typing import Literal, Protocol
from urllib.parse import quote, urlencode

import httpx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from lararium import __version__
from lararium.core.errors import CredentialRejectedError, DisplayableError
from lararium.core.secrets import Secret
from lararium.modules.finance.binance.catalogue import ENDPOINTS, Endpoint
from lararium.modules.finance.binance.parse import ApiValue

type Clock = Callable[[], int]
type Sleep = Callable[[int], Awaitable[None]]

ALLOWED = frozenset((e.method, e.host, e.path) for e in ENDPOINTS.values())

RECV_WINDOW = 10_000
# Binance's code for a timestamp outside the window it accepts (more than a second ahead of
# its clock, or older than recvWindow).
CLOCK_REFUSED = -1021
# A clock measure whose exchange took longer than this is taken again, at most this often.
CLOCK_SLOW_MS = 500
CLOCK_SAMPLES = 3
KEY_REJECTED_CODES = frozenset({-2014, -2015, -1022})
NOT_PRESENT = frozenset({-3003, -30105, -6021, -5026, -4047, 403004})
# What the options host answers an account it does not serve (its error-code page: -5002
# USER_NOT_ACCESS, "Option user not access"); on `eapi` only, as the code means another thing on
# the other hosts. Binance does not say an account without options gets it: unverified (design 6).
HOST_NOT_PRESENT: dict[str, frozenset[int]] = {"eapi": frozenset({-5002})}
# The hosts Binance documents no Ed25519 key for (design 3.1): an Ed25519 signature refused there
# (HTTP 401 or -1022) while `api` accepted it says the host takes no such key, not that the key
# is wrong (`BinanceKeyTypeRefused`).
KEY_TYPE_HOSTS = frozenset({"fapi", "dapi", "eapi"})
INVALID_SIGNATURE = -1022
# Parameters the client sets itself; a caller may not.
RESERVED = frozenset({"recvWindow", "timestamp", "signature"})

MAX_RETRIES = 3
RETRY_AFTER_RANGE = (1, 300)
RETRY_AFTER_DEFAULT = 30

WINDOW_MS = 60_000
# The limiter keeps each bucket under 80% of its limit, and a used-weight header at 85% of it
# blocks the bucket for the rest of the minute.
BUDGET_PERCENT = 80
BLOCK_PERCENT = 85
SPACING_MS = 100

# Per minute. The futures, options and portfolio hosts publish theirs only in their own
# `exchangeInfo` (`rateLimits`); until a sync has read it, 600 is assumed (`read_limits`).
DEFAULT_CAPS: dict[str, int] = {
    "api": 6_000,
    "ip": 12_000,
    "uid": 180_000,
    "fapi": 600,
    "dapi": 600,
    "eapi": 600,
    "papi": 600,
}
USED_WEIGHT_HEADERS = {
    "ip": "X-SAPI-USED-IP-WEIGHT-1M",
    "uid": "X-SAPI-USED-UID-WEIGHT-1M",
}
DEFAULT_USED_WEIGHT_HEADER = "X-MBX-USED-WEIGHT-1M"
# The `rateLimits` entry an `exchangeInfo` answer states a host's request weight per minute in.
REQUEST_WEIGHT = "REQUEST_WEIGHT"
_MINUTE = "MINUTE"


class BinanceError(DisplayableError):
    """Binance answered with an error, or not at all (`http_status` None)."""

    def __init__(self, http_status: int | None, code: int | None = None) -> None:
        super().__init__(_describe(http_status, code))
        self.http_status = http_status
        self.code = code
        self.label: str | None = None

    def name_query(self, label: str) -> None:
        """Say which catalogue query failed, once; the message gains ", <label>"."""
        if self.label is None:
            self.label = label
            self.args = (f"{self.args[0]}, {label}", *self.args[1:])


class BinanceKeyRejected(BinanceError, CredentialRejectedError):
    """The key is wrong, expired, restricted or its signature is refused."""

    def __init__(self, http_status: int, code: int | None) -> None:
        BinanceError.__init__(self, http_status, code)
        shown = code if code is not None else http_status
        self.args = (f"Binance rejected the key (code {shown})",)


class BinanceKeyTypeRefused(BinanceError):
    """A host other than `api` refused an Ed25519 key's signature that `api` accepted: the key
    is good, the host does not take its type. Not a rejected key; the host's labels are left
    out (`sync.refuse_host`)."""

    def __init__(self, endpoint: Endpoint, http_status: int, code: int | None) -> None:
        BinanceError.__init__(self, http_status, code)
        self.host = endpoint.host
        # The limit kind that names the host (`fapi`, `dapi`, `eapi`).
        self.limit: str = endpoint.limit
        self.args = (key_type_flag(endpoint.host),)


def key_type_flag(host: str) -> str:
    """What the owner is told of a host that refuses the key's type (design 5.1)."""
    return f"{host} does not accept this key type; use an HMAC key to include it"


class BinanceBanned(BinanceError):
    """HTTP 418: the IP is banned. A sync stops at once."""


class BinanceNotPresent(BinanceError):
    """The window or the product is not available to this account."""


class RequestRefused(DisplayableError):
    """The request is not in the catalogue's allowlist; nothing was sent."""


def _describe(http_status: int | None, code: int | None) -> str:
    if http_status is None:
        return "Binance request failed (no response)"
    if code is None:
        return f"Binance request failed (HTTP {http_status})"
    return f"Binance request failed (HTTP {http_status}, code {code})"


class Signer(Protocol):
    kind: Literal["hmac", "ed25519"]

    def sign(self, payload: str) -> str: ...


class HmacSigner:
    kind: Literal["hmac", "ed25519"] = "hmac"

    def __init__(self, secret: Secret) -> None:
        self._secret = secret

    def sign(self, payload: str) -> str:
        """The hex HMAC-SHA256 of the payload."""
        return hmac.new(
            self._secret.reveal().encode(), payload.encode(), hashlib.sha256
        ).hexdigest()

    def __repr__(self) -> str:
        return "HmacSigner()"


class Ed25519Signer:
    kind: Literal["hmac", "ed25519"] = "ed25519"

    def __init__(self, private_key_pem: Secret, passphrase: Secret | None) -> None:
        try:
            key = serialization.load_pem_private_key(
                private_key_pem.reveal().encode(),
                passphrase.reveal().encode() if passphrase is not None else None,
            )
        except Exception:
            # Whatever the library says (a wrong passphrase, an unsupported algorithm, a
            # malformed PEM), its message can describe the key.
            raise ValueError("the Ed25519 private key can't be read") from None
        if not isinstance(key, Ed25519PrivateKey):
            raise ValueError("the private key is not an Ed25519 key")
        self._key = key

    def sign(self, payload: str) -> str:
        """The base64 Ed25519 signature of the payload."""
        return base64.b64encode(self._key.sign(payload.encode())).decode()

    def __repr__(self) -> str:
        return "Ed25519Signer()"


def wall_clock() -> int:
    return time.time_ns() // 1_000_000


async def real_sleep(ms: int) -> None:
    await asyncio.sleep(ms / 1000)


class Limiter:
    """Keeps each bucket under its budget in any 60 s, spaces requests, and honours blocks."""

    def __init__(self, clock: Clock = wall_clock, sleep: Sleep = real_sleep) -> None:
        self._clock = clock
        self._sleep = sleep
        self._sent: dict[str, deque[tuple[int, int]]] = {}
        self._blocked_until: dict[str, int] = {}
        self._last_request: int | None = None

    def block(self, bucket: str, seconds: int) -> None:
        """Hold the bucket's requests for `seconds` from now."""
        self._blocked_until[bucket] = self._clock() + seconds * 1000

    async def acquire(self, bucket: str, weight: int, cap: int) -> None:
        """Return once a request of `weight` fits the bucket's budget, and record it."""
        budget = cap * BUDGET_PERCENT // 100
        sent = self._sent.setdefault(bucket, deque())
        while True:
            now = self._clock()
            blocked = self._blocked_until.get(bucket, 0) - now
            if blocked > 0:
                await self._sleep(blocked)
                continue
            while sent and sent[0][0] <= now - WINDOW_MS:
                sent.popleft()
            used = sum(w for _, w in sent)
            # A request heavier than the whole budget goes alone, when the bucket is empty.
            if sent and used + weight > budget:
                await self._sleep(sent[0][0] + WINDOW_MS - now)
                continue
            gap = SPACING_MS - (now - self._last_request) if self._last_request is not None else 0
            if gap > 0:
                await self._sleep(gap)
                continue
            sent.append((now, weight))
            self._last_request = now
            return


def minute_weight_limit(body: ApiValue) -> int | None:
    """The request weight per minute an `exchangeInfo` answer states: its `rateLimits` entry of
    type `REQUEST_WEIGHT` over one `MINUTE`, a positive whole number; None when it states
    none (another interval is not converted: the limiter counts by the minute)."""
    listed = body.get("rateLimits") if isinstance(body, dict) else None
    for entry in listed if isinstance(listed, list) else []:
        if not isinstance(entry, dict):
            continue
        if (entry.get("rateLimitType"), entry.get("interval")) != (REQUEST_WEIGHT, _MINUTE):
            continue
        number, limit = entry.get("intervalNum"), entry.get("limit")
        if number != 1 or isinstance(limit, bool) or not isinstance(limit, int | Decimal):
            continue
        if limit > 0 and limit == int(limit):
            return int(limit)
    return None


def load_json(text: str) -> ApiValue:
    """A JSON answer with its numbers exact (`Decimal`); a non-finite number is a ValueError."""

    def refuse(_: str) -> Decimal:
        raise ValueError("non-finite number")

    return json.loads(text, parse_float=Decimal, parse_constant=refuse)


def retry_after(response: httpx.Response) -> int:
    """The seconds a 429 asks to wait: `Retry-After` within 1 to 300, else 30."""
    shown = response.headers.get("Retry-After", "")
    low, high = RETRY_AFTER_RANGE
    return min(max(int(shown), low), high) if shown.isdecimal() else RETRY_AFTER_DEFAULT


def _error_code(response: httpx.Response) -> int | None:
    try:
        body = load_json(response.text)
    except ValueError:
        return None
    if isinstance(body, dict):
        code = body.get("code")
        if isinstance(code, int) and not isinstance(code, bool):
            return code
    return None


class BinanceClient:
    def __init__(
        self,
        http: httpx.AsyncClient,
        api_key: Secret,
        signer: Signer,
        *,
        clock: Clock = wall_clock,
        sleep: Sleep = real_sleep,
        limiter: Limiter | None = None,
        retries: int = MAX_RETRIES,
    ) -> None:
        self._http = http
        self._api_key = api_key
        self._signer = signer
        self._clock = clock
        self._sleep = sleep
        self._limiter = limiter or Limiter(clock, sleep)
        self._retries = retries
        self._offset = 0
        # Whether `api.binance.com` has answered a signed request: only then can another host's
        # refusal of an Ed25519 signature be about the host and not the key.
        self._api_accepted = False
        # Per limit kind; a task that reads a host's own limits replaces its entry.
        self.caps = dict(DEFAULT_CAPS)

    def __repr__(self) -> str:
        return "BinanceClient()"

    def read_limits(self, endpoint: Endpoint, body: ApiValue) -> None:
        """Take a host's request weight per minute from its own `exchangeInfo` answer
        (`minute_weight_limit`, plan Decision 4): the cap of the limit kind the endpoint's
        host counts in. An answer that states none leaves the cap as it was."""
        found = minute_weight_limit(body)
        if found is not None:
            self.caps[endpoint.limit] = found

    def now_ms(self) -> int:
        """Binance's clock: the local one plus the offset the last `sync_clock` measured."""
        return self._clock() + self._offset

    async def sync_clock(self) -> None:
        """Measure Binance's clock against the local one. The server read its clock somewhere
        in the round trip, taken as halfway; only the exchange that was answered counts, not
        the limiter's waits nor a 429's. A slow exchange (a new connection's handshake comes
        before the server reads its clock) can put the reading most of a round trip off, and
        Binance refuses a timestamp more than a second ahead of it (-1021): such a measure is
        taken again, on the connection now open, and the quickest exchange is the one used."""
        best: tuple[int, int] | None = None
        for _ in range(CLOCK_SAMPLES):
            body, sent, received = await self._request(ENDPOINTS["time"], {})
            server = body.get("serverTime") if isinstance(body, dict) else None
            if not isinstance(server, int) or isinstance(server, bool):
                raise BinanceError(200)
            took = received - sent
            if best is None or took < best[0]:
                best = (took, server - (sent + received) // 2)
            if took <= CLOCK_SLOW_MS:
                break
        assert best is not None
        self._offset = best[1]

    def _bucket(self, endpoint: Endpoint) -> str:
        if endpoint.limit in ("ip", "uid"):
            return f"{endpoint.limit}:{endpoint.path}"
        return endpoint.limit

    def _url(self, endpoint: Endpoint, params: Mapping[str, str | int | bool]) -> str:
        items = [
            (name, str(value).lower() if isinstance(value, bool) else str(value))
            for name, value in params.items()
        ]
        if endpoint.signed:
            items += [("recvWindow", str(RECV_WINDOW)), ("timestamp", str(self.now_ms()))]
        query = urlencode(items, quote_via=quote, safe="")
        if endpoint.signed:
            signature = quote(self._signer.sign(query), safe="")
            query = f"{query}&signature={signature}"
        return f"https://{endpoint.host}{endpoint.path}" + (f"?{query}" if query else "")

    def _note_used_weight(self, endpoint: Endpoint, response: httpx.Response) -> None:
        header = USED_WEIGHT_HEADERS.get(endpoint.limit, DEFAULT_USED_WEIGHT_HEADER)
        shown = response.headers.get(header)
        if shown is None or not shown.isdecimal():
            return
        cap = self.caps[endpoint.limit]
        if int(shown) * 100 >= cap * BLOCK_PERCENT:
            # Binance's minute is the clock's minute.
            seconds = 60 - (self.now_ms() // 1000) % 60
            self._limiter.block(self._bucket(endpoint), seconds)

    async def call(
        self, endpoint: Endpoint, params: Mapping[str, str | int | bool] = {}
    ) -> ApiValue:
        """Send one catalogue query; numbers in the answer are `Decimal`."""
        body, _, _ = await self._request(endpoint, params)
        return body

    async def _request(
        self, endpoint: Endpoint, params: Mapping[str, str | int | bool]
    ) -> tuple[ApiValue, int, int]:
        try:
            try:
                return await self._exchange(endpoint, params)
            except BinanceError as error:
                # The timestamp was outside what Binance accepts: the clocks drifted apart
                # since they were compared (a first sync runs for half an hour). Compare them
                # again and ask once more.
                if error.code != CLOCK_REFUSED or not endpoint.signed:
                    raise
            await self.sync_clock()
            return await self._exchange(endpoint, params)
        except BinanceError as error:
            # Which query failed is what tells a wrong request from an outage; a label is
            # the catalogue's own name, never part of the key or the query.
            # A rejected key, a ban and an absent product say what they are already.
            if type(error) is BinanceError:
                error.name_query(endpoint.label)
            raise

    async def _exchange(
        self, endpoint: Endpoint, params: Mapping[str, str | int | bool]
    ) -> tuple[ApiValue, int, int]:
        """The answer, and the local clock just before and just after the exchange that gave it."""
        # A hand-built endpoint can carry a listed route with other terms (unsigned, a lighter
        # weight, another limit): only the catalogue's own is sent.
        if (
            ENDPOINTS.get(endpoint.label) != endpoint
            or (endpoint.method, endpoint.host, endpoint.path) not in ALLOWED
        ):
            raise RequestRefused(f"Binance request refused: {endpoint.label} is not a listed query")
        if RESERVED & params.keys():
            raise RequestRefused("Binance request refused: a parameter is set by the client")
        headers = {"User-Agent": f"Lararium/{__version__}"}
        if endpoint.signed:
            headers["X-MBX-APIKEY"] = self._api_key.reveal()
        retries = 0
        while True:
            await self._limiter.acquire(
                self._bucket(endpoint), endpoint.weight, self.caps[endpoint.limit]
            )
            sent = self._clock()
            try:
                response = await self._http.request(
                    endpoint.method,
                    self._url(endpoint, params),
                    headers=headers,
                    follow_redirects=False,
                )
            except httpx.HTTPError:
                # httpx's messages can quote the URL, which carries the signed query.
                raise BinanceError(None) from None
            received = self._clock()
            self._note_used_weight(endpoint, response)
            status = response.status_code
            if status == 429 and retries < self._retries:
                retries += 1
                await self._sleep(retry_after(response) * 1000)
                continue
            return self._result(endpoint, response), sent, received

    def _result(self, endpoint: Endpoint, response: httpx.Response) -> ApiValue:
        status = response.status_code
        if 200 <= status < 300:
            try:
                body = load_json(response.text)
            except ValueError:
                raise BinanceError(status) from None
            if endpoint.signed and endpoint.limit in ("api", "ip", "uid"):
                self._api_accepted = True
            return body
        code = _error_code(response)
        if status == 401 or code in KEY_REJECTED_CODES:
            refused = status == 401 or code == INVALID_SIGNATURE
            if (
                refused
                and self._signer.kind == "ed25519"
                and endpoint.limit in KEY_TYPE_HOSTS
                and self._api_accepted
            ):
                raise BinanceKeyTypeRefused(endpoint, status, code)
            raise BinanceKeyRejected(status, code)
        if status == 418:
            raise BinanceBanned(status, code)
        if status == 404 or code in NOT_PRESENT or code in HOST_NOT_PRESENT.get(endpoint.limit, ()):
            raise BinanceNotPresent(status, code)
        raise BinanceError(status, code)
