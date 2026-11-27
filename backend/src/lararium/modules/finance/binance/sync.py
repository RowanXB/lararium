"""The Binance connector: the read-only gate, when a sync is due, the balance reads and the
history walks into raw records (design §5.1 to §5.3).

Nothing here logs or reports a row's values, the key, an address or a hash.

The connection's `config` keys, what each holds and who writes it:

- `key_type`: `hmac` or `ed25519` (the routes, on create and key update).
- `uid`: the Binance account the key answers for (the create route; checked on every sync).
- `account_map`: wallet key to finance account id (`accounts.wallet_account`).
- `cursors`: per history label (or `<label>:<variant>`, `:<isolated pair>`) `{fetched_to,
  backfilled_to, complete}`, per `my-trades:<symbol>` and `margin-trades:<symbol>` (an isolated
  pair's `margin-trades:<symbol>:isolated`) the last trade id, per `um-trades:<symbol>`,
  `cm-trades:<symbol>` and `opt-trades:<symbol>` a window cursor as a history label's
  (`fetch.commit`).
- `not_present`: label to when Binance said it does not serve it (`fetch`, the balance reads).
- `refused_hosts`: per derivatives host (`fapi`, `dapi`, `eapi`), when it refused the Ed25519
  key's type (`refuse_host`); counted only while the key is an Ed25519 one, removed when the
  host is asked again (`ask_refused_again`) and by a key update (the route).
- `first_sync_done`: every first walk has ended and the openings may be posted (`opening`).
- `normalized_through`: the last raw record id applied (`normalize`).
- `normalizer_version`: the NORMALIZER_VERSION that applied them; another replays all
  (`normalize`).
- `flags`: every flag already published (`normalize.publish_flags`).
- `report_digests`: per report label, the digest of its last stored answer
  (`balances.store_reports`).
- `swept`: the (asset, interval start in ms) pairs whose Spot trades were swept, and the
  (asset, interval start, wallet key) of a margin wallet's (`sweep`).
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

from collections.abc import Awaitable, Callable, Mapping, Sequence
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
    CROSS_MARGIN_LABEL,
    Read,
    backfill_daily_snapshots,
    daily_rows,
    margin_created,
    millis,
    read_assets,
    read_balances,
    record_assets,
    report_balances,
    settle_reads,
    store_read,
    store_reports,
)
from lararium.modules.finance.binance.catalogue import ENDPOINTS
from lararium.modules.finance.binance.client import (
    MAX_RETRIES,
    BinanceBanned,
    BinanceClient,
    BinanceError,
    BinanceKeyRejected,
    BinanceKeyTypeRefused,
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
    LIMITS_INFO,
    MARGIN_TRADES,
    NOT_PRESENT_KEY,
    REFUSED_HOSTS_KEY,
    SYMBOL_TRADES,
    TRADE_LABELS,
    TRADES_LABEL,
    VERSION,
    TradeWalk,
    absent,
    backfill_complete,
    commit,
    due_refusals,
    fresh_config,
    isolated_symbols,
    trade_cursor_key,
    trade_symbols,
    walk,
    walk_symbol_trades,
    walk_trades,
)
from lararium.modules.finance.binance.instruments import assets_seen as seen
from lararium.modules.finance.binance.normalize import NORMALIZED_THROUGH_KEY, Run, normalize
from lararium.modules.finance.binance.notices import tell
from lararium.modules.finance.binance.opening import set_openings
from lararium.modules.finance.binance.parse import ApiValue, BinanceDataError, as_api, parse
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
# (asset, interval start in ms) of every interval already swept for that asset's Spot trades;
# (asset, interval start, wallet key) for a margin wallet's.
SWEPT_KEY = "swept"
# The wallets that trade, whose unexplained change sends for the sweep: the trade label each
# wallet's trades are in, and whether they are isolated margin pairs'.
SWEPT_WALLETS: dict[str, tuple[str, bool]] = {
    "spot": (TRADES_LABEL, False),
    "cross_margin": (MARGIN_TRADES, False),
    "isolated_margin": (MARGIN_TRADES, True),
}
_SPOT = "spot"

GATE_LABEL = "api-restrictions"
ACCOUNT_LABEL = "spot-account"
COIN_CONFIG_LABEL = "coin-config"
ASSET_DETAIL_LABEL = "asset-detail"
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
        if not done and not backfill_complete(connection.config, await isolated_symbols(ctx)):
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
    for label in [*HISTORY_LABELS, *TRADE_LABELS, *SYMBOL_TRADES]:
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


def _swept_mark(item: JSONValue) -> tuple[str, JSONValue, str] | None:
    """A `config["swept"]` entry as (asset, interval start, wallet key): a Spot sweep's entry
    names no wallet."""
    if isinstance(item, list) and len(item) == 2:
        return (str(item[0]), item[1], _SPOT)
    if isinstance(item, list) and len(item) == 3:
        return (str(item[0]), item[1], str(item[2]))
    return None


def _swept_entry(code: str, from_ms: int, wallet: str) -> list[JSONValue]:
    return [code, from_ms] if wallet == _SPOT else [code, from_ms, wallet]


async def sweep(
    ctx: SyncContext, client: BinanceClient, info: ApiValue, found: list[Difference]
) -> bool:
    """For each difference on a wallet that trades (`SWEPT_WALLETS`: Spot and the margin
    wallets) whose asset was not swept for that interval yet: every trade of the wallet, in the
    interval, of every symbol with the asset on either side (for Isolated Margin, every
    isolated pair with it), so a round trip through an asset never seen is found (design 5.2).
    Each (asset, interval start, wallet) is swept once (`config["swept"]`); whether any was."""
    config = await fresh_config(ctx)
    held = config.get(SWEPT_KEY)
    swept: list[JSONValue] = list(held) if isinstance(held, list) else []
    done = {mark for item in swept if (mark := _swept_mark(item)) is not None}
    wanted: list[tuple[str, str, Difference]] = []
    async with ctx.session_factory() as session:
        owned = await wallets(session, ctx.connection.id)
        for difference in found:
            wallet = owned.get(difference.account_id)
            if wallet not in SWEPT_WALLETS:
                continue
            code = await asset_code(session, difference.instrument_id)
            mark = (code, millis(difference.from_at), wallet)
            if mark not in done:
                done.add(mark)
                wanted.append((code, wallet, difference))
    if not wanted:
        return False
    seen_now = await assets_seen(ctx)
    pairs = await isolated_symbols(ctx)
    for code, wallet, difference in wanted:
        label, isolated = SWEPT_WALLETS[wallet]
        cross = label == MARGIN_TRADES and not isolated
        symbols = trade_symbols(info, seen_now, either_side_of=code, margin=cross)
        if isolated:
            symbols = [symbol for symbol in symbols if symbol in pairs]
        await walk_trades(
            ctx,
            client,
            label,
            symbols,
            since_ms=millis(difference.from_at),
            until_ms=millis(difference.to_at),
            isolated=isolated,
        )
        swept.append(_swept_entry(code, millis(difference.from_at), wallet))
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
        # The wallet checks' flags and the limits reads', published with the records' (one line,
        # Decision 15).
        flags: list[str] = []
        await ask_refused_again(ctx, client.now_ms())
        # Before any other request to a derivatives host: its own limits (plan Decision 4).
        limits = await read_limits(ctx, client, flag=flags.append)
        reads = {
            GATE_LABEL: gate,
            ACCOUNT_LABEL: account,
            **await self._read_balances(ctx, client, flag=flags.append),
        }
        # Before any asset is met: an instrument first needed can then be matched by its
        # CoinGecko id (design 5.5). A failure is logged and the sync goes on.
        await refresh_ids_quietly(ctx, clock=client.now_ms, sleep=self._sleep)
        for read in reads.values():
            await store_read(ctx, read)
        reference = await read_balances(client, [COIN_CONFIG_LABEL, ASSET_DETAIL_LABEL])
        reference |= limits

        for label in HISTORY_LABELS:
            await on_host(ctx, client, walk(ctx, client, label), flag=flags.append)
        # After the income and bill histories: their rows name the symbols traded.
        for label in SYMBOL_TRADES:
            await on_host(ctx, client, walk_symbol_trades(ctx, client, label), flag=flags.append)
        first = connection.config.get(FIRST_SYNC_DONE_KEY) is not True
        reference |= await read_balances(client, [EXCHANGE_INFO_LABEL])
        info = as_api(reference[EXCHANGE_INFO_LABEL].body)
        trades = [
            TradeWalk(
                TRADES_LABEL,
                tuple(await walk_symbols(ctx, client, info, reads, everything=first)),
            ),
            *await walk_margin_trades(ctx, client, info, reads, everything=first),
        ]

        traded = [await _traded(ctx, trade, first) for trade in trades]
        settled = await settle_reads(ctx, client, reads, trades=traded)
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

    async def _read_balances(
        self, ctx: SyncContext, client: BinanceClient, *, flag: Callable[[str], None]
    ) -> dict[str, Read]:
        config: Mapping[str, JSONValue] = await fresh_config(ctx)
        missing = config.get(NOT_PRESENT_KEY)
        missing = missing if isinstance(missing, dict) else {}
        reads: dict[str, Read] = {}
        for label in BALANCE_LABELS:
            if label in (GATE_LABEL, ACCOUNT_LABEL) or absent(config, label, client.now_ms()):
                continue
            try:
                read = (await read_balances(client, [label]))[label]
            except BinanceNotPresent:
                await commit(ctx, label, absent_since=client.now_ms())
                continue
            except BinanceKeyTypeRefused as refused:
                await refuse_host(ctx, client, refused, flag=flag)
                # The host's other labels are not asked now (`fetch.absent`).
                config = await fresh_config(ctx)
                continue
            if label == CROSS_MARGIN_LABEL and not margin_created(read):
                # The account Binance has not opened: not present, as a refused product is.
                await commit(ctx, label, absent_since=client.now_ms())
                continue
            reads[label] = read
            if label in missing:
                await commit(ctx, label, present=True)
        return reads


def limits_unread_flag(label: str) -> str:
    return f"{label} could not be read: its host's request limit stays at the assumed 600 a minute"


async def refuse_host(
    ctx: SyncContext,
    client: BinanceClient,
    refused: BinanceKeyTypeRefused,
    *,
    flag: Callable[[str], None],
) -> None:
    """A host that does not take the key's type (an Ed25519 key off `api`, design 5.1): it goes
    to `refused_hosts`, so nothing more is asked of it while the key is an Ed25519 one, for 7
    days (`ask_refused_again`), and the owner is told by the host's name to use an HMAC key. A
    key update asks it again at once. The rest of the sync goes on; the connection is not
    rejected."""
    await _refusals(ctx, set_to={refused.limit: client.now_ms()})
    flag(str(refused))


async def ask_refused_again(ctx: SyncContext, now_ms: int) -> None:
    """At the start of a sync: the refused hosts to ask again (`fetch.due_refusals`: every one
    once the key is no longer an Ed25519 one, else each refused 7 days ago) leave
    `refused_hosts`."""
    due = due_refusals(await fresh_config(ctx), now_ms)
    if due:
        await _refusals(ctx, remove=due)


async def _refusals(
    ctx: SyncContext, *, set_to: Mapping[str, int] | None = None, remove: Sequence[str] = ()
) -> None:
    """Change `refused_hosts` in a short transaction of its own, under the connection's lock."""
    connection_id = ctx.connection.id
    args: dict[str, JSONValue] = {"connection_id": str(connection_id)}
    async with (
        ctx.session_factory() as session,
        command_context(session, ctx.command.actor, "binance.refused_hosts", args) as cmd,
    ):
        current = (await lock_connection(cmd, connection_id)).config.get(REFUSED_HOSTS_KEY)
        held: dict[str, JSONValue] = dict(current) if isinstance(current, dict) else {}
        held.update(set_to or {})
        for host in remove:
            held.pop(host, None)
        await update_config(cmd, connection_id, {REFUSED_HOSTS_KEY: held})


async def on_host[T](
    ctx: SyncContext,
    client: BinanceClient,
    step: Awaitable[T],
    *,
    flag: Callable[[str], None],
) -> T | None:
    """Run one walk; a host that refuses the key's type there is left out (`refuse_host`) and
    the sync goes on."""
    try:
        return await step
    except BinanceKeyTypeRefused as refused:
        await refuse_host(ctx, client, refused, flag=flag)
        return None


async def read_limits(
    ctx: SyncContext, client: BinanceClient, *, flag: Callable[[str], None]
) -> dict[str, Read]:
    """Each derivatives host's exchange information, and its request weight per minute taken
    from it (`BinanceClient.read_limits`); the reads, stored with the reference reports.

    A read used only for a limit that has a safe default never stops the sync: one that cannot
    be fetched (any Binance error but a ban or a rejected key, which stop every request) or
    typed (`BinanceDataError`: an answer whose shape the catalogue refuses) leaves that host at
    the assumed limit, is flagged by its label (never a value), and nothing of it is stored.
    It is unsigned, so it says nothing of the key's type. A host that refuses the key's type is
    not asked (`fetch.absent`)."""
    reads: dict[str, Read] = {}
    for label in LIMITS_INFO:
        if absent(await fresh_config(ctx), label, client.now_ms()):
            continue
        try:
            read = (await read_balances(client, [label]))[label]
        except (BinanceBanned, BinanceKeyRejected):
            raise
        except (BinanceError, BinanceDataError):
            flag(limits_unread_flag(label))
            continue
        client.read_limits(ENDPOINTS[label], as_api(read.body))
        reads[label] = read
    return reads


async def walk_symbols(
    ctx: SyncContext,
    client: BinanceClient,
    info: ApiValue,
    reads: Mapping[str, Read],
    *,
    everything: bool,
    label: str = TRADES_LABEL,
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
        listed = trade_symbols(info, shown, everything=everything, margin=label == MARGIN_TRADES)
        more = [s for s in listed if s not in walked]
        if not more:
            return walked
        await walk_trades(ctx, client, label, more)
        walked += more


async def walk_margin_trades(
    ctx: SyncContext,
    client: BinanceClient,
    info: ApiValue,
    reads: Mapping[str, Read],
    *,
    everything: bool,
) -> list[TradeWalk]:
    """Walk the margin trades of this sync's symbols; the walks made. Cross margin's only when
    this sync read the cross margin account (an account not created is not read), by the
    symbol rules of Spot's (`walk_symbols`); every isolated pair the account has, as isolated
    trades."""
    walked: list[TradeWalk] = []
    if CROSS_MARGIN_LABEL in reads:
        symbols = await walk_symbols(
            ctx, client, info, reads, everything=everything, label=MARGIN_TRADES
        )
        walked.append(TradeWalk(MARGIN_TRADES, tuple(symbols)))
    pairs = sorted(await isolated_symbols(ctx))
    if pairs:
        await walk_trades(ctx, client, MARGIN_TRADES, pairs, isolated=True)
        walked.append(TradeWalk(MARGIN_TRADES, tuple(pairs), isolated=True))
    return walked


async def _traded(ctx: SyncContext, trade: TradeWalk, first: bool) -> TradeWalk:
    """The symbols whose trades a read made again is checked against: those of a later sync;
    on the first sync (every listed symbol) only those with a trade, or each round would ask
    them all again."""
    if not first:
        return trade
    cursors = (await fresh_config(ctx)).get(CURSORS_KEY)
    known = cursors if isinstance(cursors, dict) else {}
    kept = tuple(
        symbol
        for symbol in trade.symbols
        if trade_cursor_key(trade.label, symbol, isolated=trade.isolated) in known
    )
    return TradeWalk(trade.label, kept, trade.isolated)


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
        await store_reports(cmd, connection, reference, flag=flag)
        if first:
            rows = await daily_rows(session, connection.id)
            await backfill_daily_snapshots(cmd, connection, rows, flag=flag)
        await report_balances(cmd, connection, settled, flag=flag)
