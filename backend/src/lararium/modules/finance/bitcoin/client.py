"""A read-only client for an Esplora HTTP API (mempool.space by default).

One pool, one request a second: mempool.space publishes no rate numbers and bans repeat
offenders, so the client stays well under any plausible limit and, on a 429 with no reset
header, waits a minute. Every call returns the parsed value beside the raw body, which the sync
stores as a raw record. An address goes into a request path, so it is checked to be a plain
token first, and no error names it.
"""

import asyncio
import re
from decimal import Decimal

import httpx

from lararium.core.errors import DisplayableError
from lararium.modules.finance.bitcoin.parse import (
    AddressStats,
    Status,
    Tx,
    parse_height,
    parse_stats,
    parse_status,
    parse_txs,
)
from lararium.modules.finance.readonly_http import (
    Clock,
    HttpNotFoundError,
    JSONValue,
    RateLimit,
    ReadOnlyClient,
    Sleep,
    monotonic,
)

POOL = "esplora"
LIMIT = RateLimit(1, 1)
_RESET_WAIT = Decimal(60)
# Bech32 and base58 addresses, and transaction ids, are alphanumeric.
_TOKEN = re.compile(r"[A-Za-z0-9]{1,100}")


class InvalidIdentifier(DisplayableError):
    """An address or transaction id that is not one plain token. Fixed text: the value is
    never echoed."""

    def __init__(self) -> None:
        super().__init__("Not a valid address or transaction id")


def _token(value: str) -> str:
    if not _TOKEN.fullmatch(value):
        raise InvalidIdentifier
    return value


class EsploraClient:
    def __init__(
        self,
        http: httpx.AsyncClient,
        base_url: str,
        *,
        sleep: Sleep = asyncio.sleep,
        clock: Clock = monotonic,
    ) -> None:
        self._client = ReadOnlyClient(
            http,
            base_url,
            dict,
            {POOL: LIMIT},
            sleep=sleep,
            clock=clock,
            default_reset_wait=_RESET_WAIT,
        )

    async def _get(self, path: str) -> JSONValue:
        return (await self._client.get(path, pool=POOL)).json

    async def tip_height(self) -> int:
        return parse_height(await self._get("/blocks/tip/height"))

    async def stats(self, address: str) -> tuple[AddressStats, JSONValue]:
        body = await self._get(f"/address/{_token(address)}")
        return parse_stats(body), body

    async def txs(self, address: str, after: str | None = None) -> tuple[list[Tx], JSONValue]:
        """The address's history: unconfirmed and the newest confirmed first, or, with `after`
        (the last transaction id already seen), the confirmed page that follows it."""
        path = f"/address/{_token(address)}/txs"
        if after is not None:
            path += f"/chain/{_token(after)}"
        body = await self._get(path)
        return parse_txs(body), body

    async def status(self, txid: str) -> tuple[Status | None, JSONValue]:
        """A transaction's status; `None` when the server does not know it (a dropped or
        replaced transaction)."""
        try:
            body = await self._get(f"/tx/{_token(txid)}/status")
        except HttpNotFoundError:
            return None, None
        return parse_status(body), body
