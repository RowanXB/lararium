"""A client for IBKR's Flex Web Service: SendRequest, then GetStatement until it is ready.

Error messages carry IBKR's error code and, for configuration errors, its ErrorMessage with
the token and any URL removed. They never carry the token, a URL or a response body.
"""

import asyncio
import math
import re
import time
from collections.abc import Awaitable, Callable
from datetime import date
from decimal import Decimal
from xml.etree import ElementTree

import httpx

from lararium import __version__
from lararium.core.errors import CredentialRejectedError
from lararium.core.secrets import Secret

BASE = "https://ndcdyn.interactivebrokers.com/AccountManagement/FlexWebService"
VERSION = "3"
# IBKR rate-limits a token; any two requests of one client are at least this far apart.
MIN_SPACING = 6
# A statement is rarely ready sooner, so the first poll follows its SendRequest by this much.
FIRST_POLL = 5
# Polling stops, as a retryable failure, once this long has passed since the SendRequest.
POLL_BUDGET = 120

# Token, query, or permission problems the owner has to fix.
CONFIG_CODES = frozenset(range(1011, 1017))
# Failures a later sync may not meet again.
RETRYABLE_CODES = frozenset({1001, 1003, 1004, 1005, 1006, 1007, 1008, 1009, 1018, 1019, 1021})
# The statement isn't ready or the server is busy with it: GetStatement is polled again within
# POLL_BUDGET. The other retryable codes (1018 "too many requests" among them) raise at once, so
# a rate limit isn't answered with more requests; the next tick retries.
POLL_AGAIN_CODES = frozenset({1003, 1004, 1005, 1006, 1007, 1008, 1009, 1019})

_URL = re.compile(r"https?://\S+", re.IGNORECASE)
_MAX_MESSAGE = 200


class FlexError(Exception):
    """The Flex Web Service answered with an error (code 0: an answer it couldn't read)."""

    def __init__(self, code: int) -> None:
        super().__init__(f"IBKR Flex error {code}")
        self.code = code


class FlexRetryableError(FlexError):
    """A failure a later sync may not meet again: busy, not ready, or unreachable."""


class FlexConfigError(FlexError, CredentialRejectedError):
    """The token, the query or its permissions are wrong; the owner has to fix them."""

    def __init__(self, code: int, message: str) -> None:
        Exception.__init__(
            self, f"IBKR Flex error {code}: {message}" if message else f"IBKR Flex error {code}"
        )
        self.code = code


type Sleep = Callable[[int], Awaitable[None]]
type Clock = Callable[[], Decimal]


def monotonic() -> Decimal:
    """The monotonic clock in seconds, exact."""
    return Decimal(time.monotonic_ns()) / 1_000_000_000


def _child_text(root: ElementTree.Element, name: str) -> str | None:
    """The text of the first child called `name`, whatever its case."""
    for child in root:
        if child.tag.lower() == name.lower():
            return (child.text or "").strip()
    return None


class FlexClient:
    def __init__(
        self,
        http: httpx.AsyncClient,
        token: Secret,
        *,
        sleep: Sleep = asyncio.sleep,
        clock: Clock = monotonic,
    ) -> None:
        self._http = http
        self._token = token
        self._sleep = sleep
        self._clock = clock
        self._last_request: Decimal | None = None

    def _clean(self, message: str) -> str:
        message = message.replace(self._token.reveal(), "")
        message = " ".join(_URL.sub("", message).split())
        return message[:_MAX_MESSAGE]

    async def _get(self, endpoint: str, params: dict[str, str], gap: int) -> bytes:
        """GET one endpoint, once `gap` seconds have passed since the last request."""
        if self._last_request is not None:
            wait = gap - (self._clock() - self._last_request)
            if wait > 0:
                # Whole seconds, rounded up, so the gap is never short.
                await self._sleep(math.ceil(wait))
        self._last_request = self._clock()
        try:
            response = await self._http.get(
                f"{BASE}/{endpoint}",
                params={"t": self._token.reveal(), **params, "v": VERSION},
                headers={"User-Agent": f"Lararium/{__version__}"},
            )
        except httpx.HTTPError:
            # httpx's messages can quote the URL, which carries the token.
            raise FlexRetryableError(0) from None
        status = response.status_code
        if status == 429 or status >= 500:
            raise FlexRetryableError(0)
        if status != 200:
            raise FlexError(0)
        return response.content

    def _parse(self, body: bytes) -> ElementTree.Element:
        if b"<!doctype" in body.lower():
            raise FlexError(0)
        try:
            return ElementTree.fromstring(body)
        except ElementTree.ParseError:
            raise FlexError(0) from None

    def _error(self, root: ElementTree.Element) -> FlexError:
        text = _child_text(root, "ErrorCode")
        code = int(text) if text is not None and text.isdecimal() else 0
        if code in CONFIG_CODES:
            return FlexConfigError(code, self._clean(_child_text(root, "ErrorMessage") or ""))
        if code in RETRYABLE_CODES:
            return FlexRetryableError(code)
        return FlexError(code)

    async def fetch(self, query_id: str, fd: date, td: date) -> bytes:
        """Request the query's statement for fd..td, wait for it, and return its body."""
        send = self._parse(
            await self._get(
                "SendRequest",
                {"q": query_id, "fd": f"{fd:%Y%m%d}", "td": f"{td:%Y%m%d}"},
                MIN_SPACING,
            )
        )
        reference = _child_text(send, "ReferenceCode")
        if _child_text(send, "Status") != "Success" or not reference:
            raise self._error(send)
        started = self._clock()
        gap = FIRST_POLL
        pending = 1019
        while True:
            if self._clock() - started + gap > POLL_BUDGET:
                raise FlexRetryableError(pending)
            body = await self._get("GetStatement", {"q": reference}, gap)
            gap = MIN_SPACING
            root = self._parse(body)
            if root.tag.lower() != "flexstatementresponse":
                return body
            error = self._error(root)
            if error.code not in POLL_AGAIN_CODES:
                raise error
            pending = error.code
