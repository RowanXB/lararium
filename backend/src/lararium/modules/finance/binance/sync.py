"""The Binance connector: the read-only gate, when a sync is due, the balance reads and the
history walks into raw records (design §5.1 to §5.3).

Nothing here logs or reports a row's values, the key, an address or a hash.

The connection's `config` keys, what each holds and who writes it:

- `key_type`: `hmac` or `ed25519` (the routes, on create and key update).
- `uid`: the Binance account the key answers for (the create route; checked on every sync).
- `account_map`: wallet key to finance account id (`accounts.wallet_account`).
- `cursors`: per history label (or `<label>:<variant>`) `{fetched_to, backfilled_to,
  complete}`, per `my-trades:<symbol>` the last trade id (`fetch.commit`).
- `not_present`: label to when Binance said it does not serve it (`fetch`, the balance reads).
- `first_sync_done`: every first walk has ended and the openings may be posted (`opening`).
- `normalized_through`: the last raw record id applied (`normalize`).
- `normalizer_version`: the NORMALIZER_VERSION that applied them; another replays all
  (`normalize`).
- `flags`: every flag already published (`normalize.publish_flags`).
- `report_digests`: per report label, the digest of its last stored answer
  (`balances.store_reports`).
- `swept`: the (asset, interval start in ms) pairs whose trades were swept (`sweep`).
- `reconciled_through`: the newest report time the last reconciliation covered
  (`reconcile.reconcile`).
- `touched_pending`: per `<account>:<instrument>`, the span of posting times committed runs
  touched that no reconciliation has checked yet (`reconcile.queue_touched`, cleared by
  `reconcile.reconcile`).
- `pending_flags`, `pending_lines`: what is queued for the sync's one message and not told
  yet (`notices.queue`, cleared by `notices.tell`).
- `bars_asked`: per symbol, the first and last day of bars already asked (`prices`).
- `coingecko_ids_asked`: the last attempt to read CoinGecko's id map, `{at_ms, ok}`
  (`prices`).
- `sync_requested`: the owner asked for this sync ("Sync now", a key update); it makes the
  sync run although one is not due (core's `request_sync`; the worker removes it once a
  sync succeeds).
"""

from collections.abc import Callable, Mapping
from datetime import datetime, timedelta
from uuid import UUID

import httpx
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from lararium.core.commands import command_context
from lararium.core.connections import (
    get_connection,
    load_credential,
    lock_connection,
    update_config,
)
from lararium.core.connectors import Capability, SyncContext
from lararium.core.errors import CredentialRejectedError, DisplayableError
from lararium.core.json_types import JSONValue
from lararium.core.secrets import Secret
from lararium.modules.finance.binance import layout, notices
from lararium.modules.finance.binance.balances import (
    Read,
    backfill_daily_snapshots,
    daily_rows,
    millis,
    read_assets,
    read_balances,
    record_assets,
    report_balances,
    settle_reads,
    store_read,
    store_reports,
)
from lararium.modules.finance.binance.client import (
    MAX_RETRIES,
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
    backfill_complete,
    commit,
    fresh_config,
    trade_symbols,
    walk,
    walk_trades,
)
from lararium.modules.finance.binance.instruments import assets_seen as seen
from lararium.modules.finance.binance.normalize import NORMALIZED_THROUGH_KEY, Run, normalize
from lararium.modules.finance.binance.notices import tell
from lararium.modules.finance.binance.opening import set_openings
from lararium.modules.finance.binance.parse import ApiValue, as_api, parse
from lararium.modules.finance.binance.prices import refresh_ids_quietly, update_prices
from lararium.modules.finance.binance.reconcile import (
    Difference,
    Point,
    Touched,
    asset_code,
    check,
    queue_touched,
    reconcile,
    wallets,
)
from lararium.modules.finance.binance.tables import moment

# Permissions a reading key may have; any other that is true refuses the sync.
HARMLESS_TRUE = frozenset({"enableReading", "ipRestrict"})
DUE_AFTER = timedelta(hours=4)
FIRST_SYNC_DONE_KEY = "first_sync_done"
# (asset, interval start in ms) of every interval already swept for that asset's trades.
SWEPT_KEY = "swept"
# The wallets that trade, whose unexplained change sends for the sweep.
SWEPT_WALLETS = frozenset({"spot"})

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


def make_client(
    http: httpx.AsyncClient,
    api_key: Secret | None,
    key_type: JSONValue,
    *,
    api_secret: Secret | None = None,
    private_key: Secret | None = None,
    passphrase: Secret | None = None,
    clock: Clock = wall_clock,
    sleep: Sleep = real_sleep,
    retries: int = MAX_RETRIES,
) -> BinanceClient:
    """A client for key material: HMAC (`api_secret`) or Ed25519 (`private_key`). A missing or
    unusable key is a `CredentialRejectedError` whose message never describes the key."""
    signer: Signer | None = None
    try:
        if key_type == "hmac" and api_secret is not None:
            signer = HmacSigner(api_secret)
        elif key_type == "ed25519" and private_key is not None:
            signer = Ed25519Signer(private_key, passphrase)
    except ValueError as error:
        # Its message is written to be shown and never describes the key.
        raise CredentialRejectedError(str(error)) from None
    if api_key is None or signer is None:
        raise CredentialRejectedError("The Binance API key is missing")
    return BinanceClient(http, api_key, signer, clock=clock, sleep=sleep, retries=retries)


async def open_client(
    ctx: SyncContext, *, clock: Clock = wall_clock, sleep: Sleep = real_sleep
) -> BinanceClient:
    """A client for the connection's stored key."""
    connection = ctx.connection

    async def credential(purpose: str):
        return await load_credential(ctx.command.session, ctx.master_key, connection.id, purpose)

    return make_client(
        ctx.http,
        await credential("api_key"),
        connection.config.get("key_type"),
        api_secret=await credential("api_secret"),
        private_key=await credential("private_key"),
        passphrase=await credential("private_key_passphrase"),
        clock=clock,
        sleep=sleep,
    )


async def pass_gate(client: BinanceClient) -> Read:
    """Sync the clock, then ask `api-restrictions`: the key may do nothing but read. Nothing
    else is sent with a key the gate refuses."""
    await client.sync_clock()
    gate = (await read_balances(client, [GATE_LABEL]))[GATE_LABEL]
    problem = gate_problem(as_api(gate.body))
    if problem is not None:
        raise CredentialRejectedError(f"this key can do more than read: {problem}")
    return gate


def uid_of(account: Read) -> str | None:
    """The account id a `spot-account` read answers for."""
    found = parse(ACCOUNT_LABEL, as_api(account.body)).body
    found = found.get("uid") if isinstance(found, dict) else None
    return None if found is None else str(found)


async def assets_seen(ctx: SyncContext) -> set[str]:
    """Every asset the connection has shown, in a balance or a record, as committed now."""
    async with ctx.session_factory() as session:
        return await seen(session, ctx.connection.id)


async def opening(ctx: SyncContext) -> set[Point]:
    """After a normalising run, in a transaction of its own: the openings (plan Decision 12),
    once every history label's first walk has ended; what they touched. The run that finds the
    backfill complete also marks the first sync done and queues its one message, in the same
    transaction: the openings are never posted before it, and the message is told once."""
    connection_id = ctx.connection.id
    args: dict[str, JSONValue] = {"connection_id": str(connection_id)}
    async with (
        ctx.session_factory() as session,
        command_context(session, ctx.command.actor, "binance.opening", args) as cmd,
    ):
        connection = await get_connection(session, connection_id)
        done = connection.config.get(FIRST_SYNC_DONE_KEY) is True
        if not done and not backfill_complete(connection.config):
            return set()
        run = await Run.start(ctx, cmd, connection)
        made = await set_openings(run)
        await queue_touched(cmd, connection_id, run.touched)
        if not done:
            await update_config(cmd, connection_id, {FIRST_SYNC_DONE_KEY: True})
            line = await first_sync_line(session, connection_id, made)
            await notices.queue(cmd, connection_id, lines=[line])
        return run.touched


async def first_sync_line(session: AsyncSession, connection_id: UUID, openings: int) -> str:
    """The first sync's message: the records per label and the openings made, counts only."""
    counts: list[str] = []
    for label in [*HISTORY_LABELS, TRADES_LABEL]:
        if label not in layout.TABLE_LABELS:
            continue
        table = layout.layout(label)[0].name
        found = await session.scalar(
            text(f"select count(*) from finance.{table} where connection_id = :c"),
            {"c": connection_id},
        )
        if found:
            counts.append(f"{label} {found}")
    plural = "" if openings == 1 else "s"
    return (
        f"first sync finished: {', '.join(counts) or 'no records'};"
        f" {openings} opening adjustment{plural}"
    )


async def differences(ctx: SyncContext, touched: Touched) -> list[Difference]:
    """The differences of the intervals `touched` and the newest ones, as committed now."""
    async with ctx.session_factory() as session:
        return await check(session, ctx.connection.id, touched)


async def sweep(
    ctx: SyncContext, client: BinanceClient, info: ApiValue, found: list[Difference]
) -> bool:
    """For each difference on a wallet that trades (`SWEPT_WALLETS`) whose asset was not swept
    for that interval yet: every trade, in the interval, of every symbol with the asset on
    either side, so a round trip through an asset never seen is found (design 5.2). Each
    (asset, interval start) is swept once (`config["swept"]`); whether any was."""
    config = await fresh_config(ctx)
    held = config.get(SWEPT_KEY)
    swept: list[JSONValue] = list(held) if isinstance(held, list) else []
    done = {(str(item[0]), item[1]) for item in swept if isinstance(item, list) and len(item) == 2}
    wanted: list[tuple[str, Difference]] = []
    async with ctx.session_factory() as session:
        owned = await wallets(session, ctx.connection.id)
        trading = {a for a, key in owned.items() if key in SWEPT_WALLETS}
        for difference in found:
            if difference.account_id not in trading:
                continue
            code = await asset_code(session, difference.instrument_id)
            mark = (code, millis(difference.from_at))
            if mark not in done:
                done.add(mark)
                wanted.append((code, difference))
    if not wanted:
        return False
    seen_now = await assets_seen(ctx)
    for code, difference in wanted:
        await walk_trades(
            ctx,
            client,
            TRADES_LABEL,
            trade_symbols(info, seen_now, either_side_of=code),
            since_ms=millis(difference.from_at),
            until_ms=millis(difference.to_at),
        )
        swept.append([code, millis(difference.from_at)])
    args: dict[str, JSONValue] = {"connection_id": str(ctx.connection.id)}
    async with (
        ctx.session_factory() as session,
        command_context(session, ctx.command.actor, "binance.sweep", args) as cmd,
    ):
        current = (await lock_connection(cmd, ctx.connection.id)).config.get(SWEPT_KEY)
        merged = list(current) if isinstance(current, list) else []
        merged += [item for item in swept if item not in merged]
        await update_config(cmd, ctx.connection.id, {SWEPT_KEY: merged})
    return True


async def reconciliation(ctx: SyncContext, touched: Touched) -> None:
    """Store, close and warn about the differences (plan Decision 11), in a transaction of its
    own."""
    connection_id = ctx.connection.id
    args: dict[str, JSONValue] = {"connection_id": str(connection_id)}
    async with (
        ctx.session_factory() as session,
        command_context(session, ctx.command.actor, "binance.reconcile", args) as cmd,
    ):
        run = await Run.start(ctx, cmd, await get_connection(session, connection_id))
        run.touched = set(touched)
        await reconcile(run)


class BinanceConnector:
    name = "binance"
    version = VERSION
    capabilities: frozenset[Capability] = frozenset({"balances", "holdings", "transactions"})

    def __init__(self, *, clock: Clock = wall_clock, sleep: Sleep = real_sleep) -> None:
        self._clock = clock
        self._sleep = sleep

    async def _gated_client(self, ctx: SyncContext) -> tuple[BinanceClient, Read]:
        client = await open_client(ctx, clock=self._clock, sleep=self._sleep)
        return client, await pass_gate(client)

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
        uid = uid_of(account)
        if uid is None or uid != str(connection.config.get("uid")):
            raise DisplayableError("Binance answered for another account than this connection's")
        reads = {GATE_LABEL: gate, ACCOUNT_LABEL: account, **await self._read_balances(ctx, client)}
        # Before any asset is met: an instrument first needed can then be matched by its
        # CoinGecko id (design 5.5). A failure is logged and the sync goes on.
        await refresh_ids_quietly(ctx, clock=client.now_ms, sleep=self._sleep)
        for read in reads.values():
            await store_read(ctx, read)
        reference = await read_balances(client, [COIN_CONFIG_LABEL])

        for label in HISTORY_LABELS:
            await walk(ctx, client, label)
        first = connection.config.get(FIRST_SYNC_DONE_KEY) is not True
        reference |= await read_balances(client, [EXCHANGE_INFO_LABEL])
        info = as_api(reference[EXCHANGE_INFO_LABEL].body)
        symbols = await walk_symbols(ctx, client, info, reads, everything=first)

        settled = await settle_reads(ctx, client, reads, symbols=await _traded(ctx, symbols, first))
        # The wallet checks' flags, published with the records' (one line, Decision 15).
        flags: list[str] = []
        await _report(ctx, reference, settled, first=first, flag=flags.append)
        touched = set((await normalize(ctx, flags=flags, reads=settled, daily=first)).touched)
        touched |= await opening(ctx)
        # The first sync asked every symbol from its first trade: a sweep finds nothing more.
        if not first and await sweep(ctx, client, info, await differences(ctx, touched)):
            touched |= (await normalize(ctx, reads=settled)).touched
            touched |= await opening(ctx)
        await reconciliation(ctx, touched)
        await update_prices(ctx, client, sleep=self._sleep)
        # Last: what the steps queued is told once the sync has got this far.
        await tell(ctx)
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


async def walk_symbols(
    ctx: SyncContext,
    client: BinanceClient,
    info: ApiValue,
    reads: Mapping[str, Read],
    *,
    everything: bool,
) -> list[str]:
    """Walk the trades of this sync's symbols; the symbols walked. With `everything` (the
    first sync) every listed symbol; otherwise those both of whose assets the account has shown
    (Decision 7): every asset seen, those this sync's balance reads show and those its records
    not yet normalised name. A trade walked can name another (its commission asset), so the
    set is worked out again until a walk adds no symbol: every trade of this sync's symbols is
    fetched before its reads are settled against them."""
    walked: list[str] = []
    while True:
        through = (await fresh_config(ctx)).get(NORMALIZED_THROUGH_KEY)
        after = UUID(through) if isinstance(through, str) else None
        shown = await assets_seen(ctx) | read_assets(reads) | await record_assets(ctx, after)
        more = [s for s in trade_symbols(info, shown, everything=everything) if s not in walked]
        if not more:
            return walked
        await walk_trades(ctx, client, TRADES_LABEL, more)
        walked += more


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
