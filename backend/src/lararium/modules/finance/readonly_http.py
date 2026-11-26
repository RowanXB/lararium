"""A read-only HTTP client for broker connectors.

It sends GET only, only to its base URL's host, and never follows a redirect; anything else
raises `ReadOnlyViolation` before a byte leaves. Calls are paced per pool of endpoints, and a
429 waits out the server's reset. Error messages are fixed text: they never carry a header
value, a query, a URL or a response body.
"""

import asyncio
import json
import math
import re
import time
from collections import deque
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import cast

import httpx

from lararium import __version__
from lararium.core.errors import CredentialRejectedError, DisplayableError
from lararium.core.json_types import JSONValue as RecordJSON

USER_AGENT = f"Lararium/{__version__}"

# JSON as the broker APIs return it, every number an exact Decimal.
type JSONValue = bool | Decimal | str | list[JSONValue] | dict[str, JSONValue] | None
type Sleep = Callable[[int], Awaitable[None]]
type Clock = Callable[[], Decimal]

MAX_RETRIES_429 = 3
# A reset further away than this is not waited for in one sleep; it is a retryable failure.
MAX_WAIT = 120
# A reset header of at least this is an epoch (seconds since 1970), not a number of seconds.
_EPOCH_FROM = Decimal(10**9)
_RESET_HEADERS = ("retry-after", "ratelimit-reset", "x-ratelimit-reset")
# The scope is kept only when it is a plain token: nothing else the server says is passed on.
_SCOPE = re.compile(r"Scope\(\s*([A-Za-z0-9_:.\-]{1,100})\s*\)\s*missing", re.IGNORECASE)
_OUTSIDE = "Refused a request outside the API's own host"
_ENCODED_BAD = ("%3f", "%23")
_CLOUDFLARE_1010 = "error code: 1010"


class HttpError(DisplayableError):
    """The API answered with an error, or didn't answer."""


class ReadOnlyViolation(HttpError):
    """A call that is not a GET to the client's own host. Nothing was sent."""


class HttpAuthError(HttpError, CredentialRejectedError):
    """401: the key is wrong or revoked; the owner has to create a new one."""


class HttpForbiddenError(HttpError):
    """403: the key lacks a permission (`scope`, when the API names it), or the host's
    firewall refused the client's signature (`cloudflare_block`)."""

    def __init__(self, scope: str | None, *, cloudflare_block: bool = False) -> None:
        super().__init__(
            "The API's firewall refused this client (403)"
            if cloudflare_block
            else "The API refused the request (403): the key lacks a permission"
        )
        self.scope = scope
        self.cloudflare_block = cloudflare_block


class HttpNotFoundError(HttpError):
    """404: the API has no such resource (an endpoint removed, an item gone)."""


class HttpRetryableError(HttpError):
    """A failure a later sync may not meet again: rate limited, a server error, a redirect, a
    timeout."""


@dataclass(frozen=True)
class RateLimit:
    requests: int
    per_seconds: int


@dataclass(frozen=True)
class Response:
    status: int
    text: str
    json: JSONValue
    headers: Mapping[str, str]


def monotonic() -> Decimal:
    """The monotonic clock in seconds, exact; pacing is measured on it."""
    return Decimal(time.monotonic_ns()) / 1_000_000_000


def wall_time() -> Decimal:
    """Seconds since 1970, exact; only a reset header given as an epoch is compared with it."""
    return Decimal(time.time_ns()) / 1_000_000_000


def loads_decimal(text: str) -> JSONValue:
    """Parse JSON with every number a Decimal, so no value passes through binary arithmetic."""
    return cast(
        JSONValue,
        json.loads(text, parse_float=Decimal, parse_int=Decimal, parse_constant=_refuse_constant),
    )


def record_json(value: JSONValue) -> RecordJSON:
    """The value as a raw record stores it: every number its Decimal's exact text, since a
    record's JSON takes no Decimal (lararium.core.json_types). Nothing else changes."""
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, list):
        return [record_json(item) for item in value]
    if isinstance(value, dict):
        return {key: record_json(item) for key, item in value.items()}
    return value


def _refuse_constant(name: str) -> object:
    raise ValueError(f"{name} is not a JSON number")


class ReadOnlyClient:
    def __init__(
        self,
        http: httpx.AsyncClient,
        base_url: str,
        headers: Callable[[], Mapping[str, str]],
        limits: Mapping[str, RateLimit],
        *,
        sleep: Sleep = asyncio.sleep,
        clock: Clock = monotonic,
        wall_clock: Clock = wall_time,
    ) -> None:
        self._http = http
        self._base = httpx.URL(base_url)
        self._base_text = base_url.rstrip("/")
        self._headers = headers
        self._limits = dict(limits)
        self._sleep = sleep
        self._clock = clock
        self._wall_clock = wall_clock
        self._sent: dict[str, deque[Decimal]] = {pool: deque() for pool in limits}
        self._locks = {pool: asyncio.Lock() for pool in limits}

    def _url(self, path: str) -> str:
        """The request URL, or `ReadOnlyViolation` when it would not stay on the base host and
        path. A query goes through `params`, never the path."""
        lowered = path.lower()
        segments = [segment.replace("%2e", ".") for segment in lowered.split("/")[1:]]
        if (
            not path.startswith("/")
            or path.startswith("//")
            or "\\" in path
            or "?" in path
            or "#" in path
            or any(code in lowered for code in _ENCODED_BAD)
            or any(segment in {".", ".."} for segment in segments)
        ):
            raise ReadOnlyViolation(_OUTSIDE)
        url = f"{self._base_text}{path}"
        try:
            parsed = httpx.URL(url)
        except httpx.InvalidURL:
            raise ReadOnlyViolation(_OUTSIDE) from None
        base_path = self._base.path.rstrip("/")
        same_origin = (parsed.scheme, parsed.host, parsed.port) == (
            self._base.scheme,
            self._base.host,
            self._base.port,
        )
        under_base = parsed.path == base_path or parsed.path.startswith(f"{base_path}/")
        if not (same_origin and under_base):
            raise ReadOnlyViolation(_OUTSIDE)
        return url

    async def _wait(self, seconds: Decimal) -> None:
        if seconds > 0:
            # Whole seconds, rounded up, so a wait is never short.
            await self._sleep(math.ceil(seconds))

    def _expire(self, pool: str, now: Decimal) -> deque[Decimal]:
        """`pool`'s send times still inside its window at `now`."""
        sent = self._sent[pool]
        while sent and now - sent[0] >= self._limits[pool].per_seconds:
            sent.popleft()
        return sent

    async def _pace(self, pool: str) -> None:
        """Wait until `pool`'s limit allows one more request, and count it. The lock makes
        check, sleep and record one step, so concurrent callers can't both pass."""
        limit = self._limits[pool]
        async with self._locks[pool]:
            sent = self._expire(pool, self._clock())
            while len(sent) >= limit.requests:
                await self._wait(sent[0] + limit.per_seconds - self._clock())
                sent = self._expire(pool, self._clock())
            sent.append(self._clock())

    def _reset_wait(self, response: httpx.Response) -> Decimal:
        """Seconds to wait before retrying a 429: the first reset header that parses."""
        for name in _RESET_HEADERS:
            raw = response.headers.get(name)
            if raw is None:
                continue
            try:
                value = Decimal(raw.strip())
            except InvalidOperation:
                continue
            if not value.is_finite() or value < 0:
                continue
            return value - self._wall_clock() if value >= _EPOCH_FROM else value
        return Decimal(1)

    async def get(
        self, path: str, *, pool: str, params: Mapping[str, str] | None = None
    ) -> Response:
        url = self._url(path)
        for attempt in range(MAX_RETRIES_429 + 1):
            await self._pace(pool)
            try:
                raw = await self._http.get(
                    url,
                    params=dict(params) if params else None,
                    headers={"User-Agent": USER_AGENT, **self._headers()},
                    follow_redirects=False,
                )
            except httpx.HTTPError:
                # httpx's messages can quote the URL, which can carry a query.
                raise HttpRetryableError("The API could not be reached") from None
            if raw.status_code == 429:
                wait = self._reset_wait(raw)
                if attempt == MAX_RETRIES_429 or wait > MAX_WAIT:
                    raise HttpRetryableError("The API is rate limiting this client (429)")
                await self._wait(wait)
                continue
            return self._result(raw)
        raise AssertionError("unreachable")  # pragma: no cover

    @staticmethod
    def _result(raw: httpx.Response) -> Response:
        status = raw.status_code
        if status == 401:
            raise HttpAuthError("The API rejected the key (401)")
        if status == 403:
            body = raw.text
            found = _SCOPE.search(body)
            scope = found.group(1) if found else None
            raise HttpForbiddenError(scope, cloudflare_block=_CLOUDFLARE_1010 in body)
        if status == 404:
            # Fixed text: never the path, which can carry an id.
            raise HttpNotFoundError("The API has no such resource (404)")
        if 300 <= status < 400:
            raise HttpRetryableError(f"The API redirected the request ({status})")
        if status >= 500:
            raise HttpRetryableError(f"The API failed ({status})")
        if status >= 400:
            raise HttpError(f"The API refused the request ({status})")
        text = raw.text
        try:
            parsed = loads_decimal(text) if text.strip() else None
        except ValueError:
            raise HttpError("The API's answer was not JSON") from None
        return Response(status, text, parsed, dict(raw.headers))
