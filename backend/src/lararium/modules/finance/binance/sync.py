"""The Binance connector: the read-only gate, when a sync is due, the balance reads and the
history walks into raw records (design §5.1 to §5.3).

Nothing here logs or reports a row's values, the key, an address or a hash.
"""

from collections.abc import Callable, Mapping
from datetime import datetime, timedelta

from lararium.core.commands import command_context
from lararium.core.connections import load_credential, update_config
from lararium.core.connectors import Capability, SyncContext
from lararium.core.errors import CredentialRejectedError, DisplayableError
from lararium.core.json_types import JSONValue
from lararium.modules.finance.binance.balances import (
    Read,
    backfill_daily_snapshots,
    daily_rows,
    read_balances,
    report_balances,
    settle_reads,
    store_read,
    store_reports,
)
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
    BALANCE_LABELS,
    CURSORS_KEY,
    HISTORY_LABELS,
    NOT_PRESENT_KEY,
    TRADES_LABEL,
    VERSION,
    absent,
    commit,
    fresh_config,
    trade_symbols,
    walk,
    walk_trades,
)
from lararium.modules.finance.binance.instruments import assets_seen as seen
from lararium.modules.finance.binance.parse import ApiValue, as_api, parse
from lararium.modules.finance.binance.tables import moment

# Permissions a reading key may have; any other that is true refuses the sync.
HARMLESS_TRUE = frozenset({"enableReading", "ipRestrict"})
DUE_AFTER = timedelta(hours=4)
FIRST_SYNC_DONE_KEY = "first_sync_done"

GATE_LABEL = "api-restrictions"
ACCOUNT_LABEL = "spot-account"
COIN_CONFIG_LABEL = "coin-config"
EXCHANGE_INFO_LABEL = "exchange-info"


def due(cursor: JSONValue, now: datetime, requested: bool) -> bool:
    """Whether a sync fetches: the owner asked, none has succeeded yet, or the last is at
    least 4 hours old."""
    if requested:
        return True
    fetched_at = cursor.get("fetched_at") if isinstance(cursor, dict) else None
    if not isinstance(fetched_at, int) or isinstance(fetched_at, bool):
        return True
    return now - moment(fetched_at) >= DUE_AFTER


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


# Hooks the later tasks fill in, in the order a sync runs them.


async def assets_seen(ctx: SyncContext) -> set[str]:
    """Every asset the connection has shown, in a balance or a record, as committed now."""
    async with ctx.session_factory() as session:
        return await seen(session, ctx.connection.id)


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

    async def _gated_client(self, ctx: SyncContext) -> tuple[BinanceClient, Read]:
        client = await open_client(ctx, clock=self._clock, sleep=self._sleep)
        await client.sync_clock()
        gate = (await read_balances(client, [GATE_LABEL]))[GATE_LABEL]
        problem = gate_problem(as_api(gate.body))
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
        if not due(connection.cursor, moment(now_ms), ctx.requested):
            return connection.cursor
        client, gate = await self._gated_client(ctx)
        account = (await read_balances(client, [ACCOUNT_LABEL]))[ACCOUNT_LABEL]
        uid = parse(ACCOUNT_LABEL, as_api(account.body)).body
        uid = uid.get("uid") if isinstance(uid, dict) else None
        if uid is None or str(uid) != str(connection.config.get("uid")):
            raise DisplayableError("Binance answered for another account than this connection's")
        reads = {GATE_LABEL: gate, ACCOUNT_LABEL: account, **await self._read_balances(ctx, client)}
        for read in reads.values():
            await store_read(ctx, read)
        reference = await read_balances(client, [COIN_CONFIG_LABEL])

        for label in HISTORY_LABELS:
            await walk(ctx, client, label)
        first = connection.config.get(FIRST_SYNC_DONE_KEY) is not True
        reference |= await read_balances(client, [EXCHANGE_INFO_LABEL])
        info = as_api(reference[EXCHANGE_INFO_LABEL].body)
        symbols = trade_symbols(info, await assets_seen(ctx), everything=first)
        await walk_trades(ctx, client, TRADES_LABEL, symbols)

        settled = await settle_reads(ctx, client, reads, symbols=await _traded(ctx, symbols, first))
        # Task 6 publishes these (Run.flag, publish_flags).
        flags: list[str] = []
        await _report(ctx, reference, settled, first=first, flag=flags.append)
        await normalize(ctx)
        await opening(ctx)
        await reconcile(ctx)
        await prices(ctx, client)
        if first:
            # After every walk: this transaction holds the connection's row from here on.
            await update_config(ctx.command, connection.id, {FIRST_SYNC_DONE_KEY: True})
        return {"fetched_at": now_ms}

    async def _read_balances(self, ctx: SyncContext, client: BinanceClient) -> dict[str, Read]:
        config: Mapping[str, JSONValue] = await fresh_config(ctx)
        missing = config.get(NOT_PRESENT_KEY)
        missing = missing if isinstance(missing, dict) else {}
        reads: dict[str, Read] = {}
        for label in BALANCE_LABELS:
            if label in (GATE_LABEL, ACCOUNT_LABEL) or absent(config, label, client.now_ms()):
                continue
            try:
                reads |= await read_balances(client, [label])
            except BinanceNotPresent:
                await commit(ctx, label, absent_since=client.now_ms())
                continue
            if label in missing:
                await commit(ctx, label, present=True)
        return reads


async def _traded(ctx: SyncContext, symbols: list[str], first: bool) -> list[str]:
    """The symbols whose trades a read made again is checked against: those of a later sync;
    on the first sync (every listed symbol) only those with a trade, or each round would ask
    them all again."""
    if not first:
        return symbols
    cursors = (await fresh_config(ctx)).get(CURSORS_KEY)
    known = cursors if isinstance(cursors, dict) else {}
    return [symbol for symbol in symbols if f"{TRADES_LABEL}:{symbol}" in known]


async def _report(
    ctx: SyncContext,
    reference: Mapping[str, Read],
    settled: Mapping[str, Read | None],
    *,
    first: bool,
    flag: Callable[[str], None],
) -> None:
    """The reports and snapshots, in a short transaction of its own: making a wallet's account
    writes the connection's config, whose row must not stay locked while later steps walk.

    The reference reports go first (`asset` reads `coin-config`), then the first sync's daily
    snapshots, so that an asset they show and the wallet no longer holds gets its 0 now.
    """
    connection = ctx.connection
    args: dict[str, JSONValue] = {"connection_id": str(connection.id)}
    async with (
        ctx.session_factory() as session,
        command_context(session, ctx.command.actor, "binance.report", args) as cmd,
    ):
        await store_reports(cmd, connection, reference)
        if first:
            rows = await daily_rows(session, connection.id)
            await backfill_daily_snapshots(cmd, connection, rows, flag=flag)
        await report_balances(cmd, connection, settled, flag=flag)
