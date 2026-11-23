"""The Binance connector: the read-only gate, when a sync is due, the balance reads and the
history walks into raw records (design §5.1 to §5.3).

Nothing here logs or reports a row's values, the key, an address or a hash.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

from lararium.core.connections import load_credential, update_config
from lararium.core.connectors import Capability, SyncContext
from lararium.core.errors import CredentialRejectedError, DisplayableError
from lararium.core.json_types import JSONValue
from lararium.core.raw_records import store_raw_record
from lararium.modules.finance.binance.catalogue import ENDPOINTS, paging_params
from lararium.modules.finance.binance.client import (
    BinanceClient,
    BinanceNotPresent,
    Clock,
    Ed25519Signer,
    HmacSigner,
    Signer,
    Sleep,
    real_sleep,
    wall_clock,
)
from lararium.modules.finance.binance.fetch import (
    NOT_PRESENT_KEY,
    VERSION,
    absent,
    commit,
    fresh_config,
    record_type,
    to_json,
    trade_symbols,
    walk,
    walk_trades,
)
from lararium.modules.finance.binance.instruments import assets_seen as seen
from lararium.modules.finance.binance.parse import ApiValue, parse

# Permissions a reading key may have; any other that is true refuses the sync.
HARMLESS_TRUE = frozenset({"enableReading", "ipRestrict"})
DUE_AFTER = timedelta(hours=4)
FIRST_SYNC_DONE_KEY = "first_sync_done"
TRADES_LABEL = "my-trades"
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)

# The phases built so far: what a sync reads (kind account, balance or position) and walks.
BALANCE_LABELS = [
    e.label
    for e in ENDPOINTS.values()
    if e.phase == "A" and e.kind in ("account", "balance", "position")
]
# Trades are walked by symbol and trade id (walk_trades), not by window.
HISTORY_LABELS = [
    e.label
    for e in ENDPOINTS.values()
    if e.phase == "A" and e.kind == "history" and e.paging != "from_id"
]
GATE_LABEL = "api-restrictions"
ACCOUNT_LABEL = "spot-account"


def _at(ms: int) -> datetime:
    return _EPOCH + timedelta(milliseconds=ms)


def due(cursor: JSONValue, now: datetime, requested: bool) -> bool:
    """Whether a sync fetches: the owner asked, none has succeeded yet, or the last is at
    least 4 hours old."""
    if requested:
        return True
    fetched_at = cursor.get("fetched_at") if isinstance(cursor, dict) else None
    if not isinstance(fetched_at, int) or isinstance(fetched_at, bool):
        return True
    return now - _at(fetched_at) >= DUE_AFTER


def _first_true(value: ApiValue) -> str | None:
    if isinstance(value, dict):
        for name, item in value.items():
            if item is True and name not in HARMLESS_TRUE:
                return name
            found = _first_true(item)
            if found is not None:
                return found
    if isinstance(value, list):
        for item in value:
            found = _first_true(item)
            if found is not None:
                return found
    return None


def gate_problem(restrictions: ApiValue) -> str | None:
    """Why the key may not be used: the first permission other than reading (and the IP
    restriction) that is true, at any depth, or `enableReading` when reading is not on."""
    found = _first_true(restrictions)
    if found is not None:
        return found
    if not isinstance(restrictions, dict) or restrictions.get("enableReading") is not True:
        return "enableReading"
    return None


async def open_client(
    ctx: SyncContext, *, clock: Clock = wall_clock, sleep: Sleep = real_sleep
) -> BinanceClient:
    """A client for the connection's key: HMAC (`api_secret`) or Ed25519 (`private_key`)."""
    connection = ctx.connection

    async def credential(purpose: str):
        return await load_credential(ctx.command.session, ctx.master_key, connection.id, purpose)

    api_key = await credential("api_key")
    key_type = connection.config.get("key_type")
    signer: Signer | None = None
    try:
        if key_type == "hmac" and (secret := await credential("api_secret")) is not None:
            signer = HmacSigner(secret)
        elif key_type == "ed25519" and (pem := await credential("private_key")) is not None:
            signer = Ed25519Signer(pem, await credential("private_key_passphrase"))
    except ValueError as error:
        # Its message is written to be shown and never describes the key.
        raise CredentialRejectedError(str(error)) from None
    if api_key is None or signer is None:
        raise CredentialRejectedError("The Binance API key is missing")
    return BinanceClient(ctx.http, api_key, signer, clock=clock, sleep=sleep)


@dataclass(frozen=True)
class BalanceRead:
    """One balance request (all its pages) and its bracket on Binance's clock: `read_from_ms`
    just before it was sent, `read_to_ms` just after its answer (plan Decision 9)."""

    label: str
    read_from_ms: int
    read_to_ms: int
    pages: list[ApiValue]


async def read_balance(client: BinanceClient, label: str) -> BalanceRead:
    endpoint = ENDPOINTS[label]
    read_from = client.now_ms()
    pages: list[ApiValue] = []
    if endpoint.paging == "page":
        cursor_name, size_name, position = paging_params(endpoint)
        while True:
            body = await client.call(
                endpoint, {cursor_name: position, size_name: endpoint.page_size}
            )
            pages.append(body)
            if len(parse(label, body).rows) < endpoint.page_size:
                break
            position += 1
    else:
        body = await client.call(endpoint, {})
        parse(label, body)
        pages.append(body)
    return BalanceRead(label, read_from, client.now_ms(), pages)


async def store_read(ctx: SyncContext, read: BalanceRead) -> UUID:
    """The read as one raw record. Its bracket is in the content too: two reads with the same
    answer are two reads, each with its own cutoff."""
    bracket: dict[str, JSONValue] = {
        "read_from_ms": read.read_from_ms,
        "read_to_ms": read.read_to_ms,
    }
    return await store_raw_record(
        ctx.command,
        connection_id=ctx.connection.id,
        record_type=record_type(read.label),
        request=bracket,
        content={**bracket, "pages": [to_json(page) for page in read.pages]},
        connector_version=VERSION,
        sync_id=ctx.sync_id,
    )


# Hooks the later tasks fill in, in the order a sync runs them.


async def assets_seen(ctx: SyncContext) -> set[str]:
    """Every asset the connection has shown, in a balance or a record, as committed now."""
    async with ctx.session_factory() as session:
        return await seen(session, ctx.connection.id)


async def settle_reads(ctx: SyncContext, client: BinanceClient, reads: list[BalanceRead]) -> None:
    """Repeat the ambiguous balance reads and the forward walks, at most 3 rounds (Task 5,
    plan Decision 9)."""


async def normalize(ctx: SyncContext) -> None:
    """Raw records into typed tables and the ledger (Task 6)."""


async def opening(ctx: SyncContext) -> None:
    """Opening adjustments (Task 7, plan Decision 12)."""


async def reconcile(ctx: SyncContext) -> None:
    """Reported balances against postings (Task 7, plan Decision 11)."""


async def prices(ctx: SyncContext, client: BinanceClient) -> None:
    """Daily bars into EUR prices (Task 10, plan Decision 13)."""


class BinanceConnector:
    name = "binance"
    version = VERSION
    capabilities: frozenset[Capability] = frozenset({"balances", "holdings", "transactions"})

    def __init__(self, *, clock: Clock = wall_clock, sleep: Sleep = real_sleep) -> None:
        self._clock = clock
        self._sleep = sleep

    async def _gated_client(self, ctx: SyncContext) -> tuple[BinanceClient, BalanceRead]:
        client = await open_client(ctx, clock=self._clock, sleep=self._sleep)
        await client.sync_clock()
        gate = await read_balance(client, GATE_LABEL)
        problem = gate_problem(gate.pages[0])
        if problem is not None:
            raise CredentialRejectedError(f"this key can do more than read: {problem}")
        return client, gate

    async def check_access(self, ctx: SyncContext) -> bool:
        # There is no approval step: a key that passes the gate works at once.
        await self._gated_client(ctx)
        return True

    async def sync(self, ctx: SyncContext) -> JSONValue:
        connection = ctx.connection
        now_ms = self._clock()
        if not due(connection.cursor, _at(now_ms), ctx.requested):
            return connection.cursor
        client, gate = await self._gated_client(ctx)
        account = await read_balance(client, ACCOUNT_LABEL)
        uid = parse(ACCOUNT_LABEL, account.pages[0]).body
        uid = uid.get("uid") if isinstance(uid, dict) else None
        if uid is None or str(uid) != str(connection.config.get("uid")):
            raise DisplayableError("Binance answered for another account than this connection's")
        reads = [gate, account, *await self._read_balances(ctx, client)]
        for read in reads:
            await store_read(ctx, read)

        for label in HISTORY_LABELS:
            await walk(ctx, client, label)
        first = connection.config.get(FIRST_SYNC_DONE_KEY) is not True
        info = await client.call(ENDPOINTS["exchange-info"], {})
        symbols = trade_symbols(info, await assets_seen(ctx), everything=first)
        await walk_trades(ctx, client, TRADES_LABEL, symbols)

        await settle_reads(ctx, client, reads)
        await normalize(ctx)
        await opening(ctx)
        await reconcile(ctx)
        await prices(ctx, client)
        if first:
            # After every walk: this transaction holds the connection's row from here on.
            await update_config(ctx.command, connection.id, {FIRST_SYNC_DONE_KEY: True})
        return {"fetched_at": now_ms}

    async def _read_balances(self, ctx: SyncContext, client: BinanceClient) -> list[BalanceRead]:
        config: Mapping[str, JSONValue] = await fresh_config(ctx)
        missing = config.get(NOT_PRESENT_KEY)
        missing = missing if isinstance(missing, dict) else {}
        reads: list[BalanceRead] = []
        for label in BALANCE_LABELS:
            if label in (GATE_LABEL, ACCOUNT_LABEL) or absent(config, label, client.now_ms()):
                continue
            try:
                reads.append(await read_balance(client, label))
            except BinanceNotPresent:
                await commit(ctx, label, absent_since=client.now_ms())
                continue
            if label in missing:
                await commit(ctx, label, present=True)
        return reads
