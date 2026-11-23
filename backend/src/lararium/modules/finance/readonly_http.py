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

import httpx

from lararium import __version__
from lararium.core.errors import CredentialRejectedError, DisplayableError

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
_SCOPE = re.compile(r"Scope\(\s*([^)\s]+)\s*\)\s*missing", re.IGNORECASE)
_CLOUDFLARE_1010 = "error code: 1010"
_MAX_SCOPE = 100


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
        super().__init__(f"The API refused the request (403), missing scope: {scope or 'unknown'}")
        self.scope = scope
        self.cloudflare_block = cloudflare_block


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
    """The clock in seconds, exact. Wall time, so a reset given as an epoch compares with it."""
    return Decimal(time.time_ns()) / 1_000_000_000


def loads_decimal(text: str) -> JSONValue:
    """Parse JSON with every number a Decimal, so no value passes through binary arithmetic."""
    return json.loads(text, parse_float=Decimal, parse_int=Decimal)  # type: ignore[no-any-return]


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
    ) -> None:
        self._http = http
        self._base = httpx.URL(base_url)
        self._base_text = base_url.rstrip("/")
        self._headers = headers
        self._limits = dict(limits)
        self._sleep = sleep
        self._clock = clock
        self._sent: dict[str, deque[Decimal]] = {pool: deque() for pool in limits}

    def _url(self, path: str) -> str:
        """The request URL, or `ReadOnlyViolation` when it would not stay on the base host."""
        if not path.startswith("/") or path.startswith("//") or "\\" in path:
            raise ReadOnlyViolation("Refused a request outside the API's own host")
        url = f"{self._base_text}{path}"
        try:
            parsed = httpx.URL(url)
        except httpx.InvalidURL:
            raise ReadOnlyViolation("Refused a request outside the API's own host") from None
        if (parsed.scheme, parsed.host, parsed.port) != (
            self._base.scheme,
            self._base.host,
            self._base.port,
        ):
            raise ReadOnlyViolation("Refused a request outside the API's own host")
        return url

    async def _wait(self, seconds: Decimal) -> None:
        if seconds > 0:
            # Whole seconds, rounded up, so a wait is never short.
            await self._sleep(math.ceil(seconds))

    async def _pace(self, pool: str) -> None:
        """Wait until `pool`'s limit allows one more request, and count it."""
        limit = self._limits[pool]
        sent = self._sent[pool]
        now = self._clock()
        while sent and now - sent[0] >= limit.per_seconds:
            sent.popleft()
        if len(sent) >= limit.requests:
            await self._wait(sent[0] + limit.per_seconds - now)
            now = self._clock()
            while sent and now - sent[0] >= limit.per_seconds:
                sent.popleft()
        sent.append(now)

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
            return value - self._clock() if value >= _EPOCH_FROM else value
        return Decimal(1)

    async def get(
        self, path: str, *, pool: str, params: Mapping[str, str] | None = None
    ) -> Response:
        url = self._url(path)
        if pool not in self._limits:
            raise KeyError(pool)
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
            scope = found.group(1)[:_MAX_SCOPE] if found else None
            raise HttpForbiddenError(scope, cloudflare_block=_CLOUDFLARE_1010 in body)
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
