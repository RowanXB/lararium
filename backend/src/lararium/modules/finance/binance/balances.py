"""Binance's reported balances (design 5.6, plan Decisions 8 and 9): one bracket per balance
request, the reads a movement falls into made again, each wallet's balance from its source as
`reported` snapshots, the typed report tables, the daily snapshots of the first sync and the
wallet checks.

A snapshot's `as_of` is the `read_from` of the request its balance came from, so the `hybrid`
rule adds exactly the postings after it. A read some record's time falls into (widened by 2
seconds, for times given to the second) may or may not contain that record; it is made again,
and a wallet whose read stays unclear gets no snapshot in this sync.

Nothing here logs or reports an amount, an address or a hash: a flag names a wallet.
"""

import hashlib
import json
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import cast
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import distinct_on, insert
from sqlalchemy.ext.asyncio import AsyncSession

from lararium.core.commands import CommandContext, command_context
from lararium.core.connections import ConnectionRecord, get_connection, update_config
from lararium.core.connectors import SyncContext
from lararium.core.json_types import JSONValue
from lararium.core.raw_records import store_raw_record
from lararium.modules.finance import models
from lararium.modules.finance.accounts import get_account
from lararium.modules.finance.binance import layout, tables
from lararium.modules.finance.binance.accounts import ACCOUNT_MAP_KEY, wallet_account
from lararium.modules.finance.binance.catalogue import (
    ENDPOINTS,
    FIELDS,
    WALLETS,
    paging_params,
    wallet_key,
)
from lararium.modules.finance.binance.client import BinanceClient
from lararium.modules.finance.binance.fetch import (
    HISTORY_LABELS,
    TRADES_LABEL,
    VERSION,
    record_type,
    to_json,
    walk,
    walk_trades,
)
from lararium.modules.finance.binance.instruments import asset
from lararium.modules.finance.binance.parse import ApiValue, BinanceDataError, Row, as_api, parse
from lararium.modules.finance.binance.tables import moment
from lararium.modules.finance.errors import AccountNotFoundError
from lararium.modules.finance.snapshots import record_snapshot

type Flag = Callable[[str], None]

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
# Times Binance gives to the second may be up to a second early (plan Decision 9).
MARGIN = timedelta(seconds=2)
ROUNDS = 3
# The reads a snapshot comes from: only these are made again when a movement falls into them.
SOURCE_LABELS = ("spot-account", "funding-asset", "wallet-balance")
WALLET_LIST = "wallet-balance"
# Reference data, not a wallet's balance: read each sync, stored when it changed.
REFERENCE_LABELS = ("coin-config", "exchange-info")
# Request parameters a read sends.
READ_PARAMS: dict[str, dict[str, str | int | bool]] = {WALLET_LIST: {"needBalanceDetail": True}}
# The digest of each report label's last stored answer: an unchanged answer is not stored again.
DIGESTS_KEY = "report_digests"
# Fields that change with every answer without the report changing.
_VOLATILE = {"exchange-info": frozenset({"serverTime"})}
# Wallets with a source of their own; every other wallet's balance comes from the wallet list
# (design 5.6) until a later phase gives it one.
OWN_SOURCES = {"spot": "spot-account", "funding": "funding-asset"}
_SPOT_FIELDS = ("free", "locked")
_HELD_FIELDS = ("free", "locked", "freeze", "withdrawing")
_MIRROR = "LD"
_DAILY = "snapshot"
_DAILY_SPOT = "SPOT"


def millis(at: datetime) -> int:
    return (at - _EPOCH) // timedelta(milliseconds=1)


@dataclass(frozen=True)
class Read:
    """One balance request (all its pages) and its bracket on Binance's clock: `read_from` just
    before it was sent, `read_to` just after its answer (plan Decision 9).

    `unclear`: a movement still fell into it after every round (`settle_reads`); its report is
    stored, but no snapshot is cut from it."""

    label: str
    body: JSONValue
    read_from: datetime
    read_to: datetime
    unclear: bool = False


def _merge(label: str, pages: list[ApiValue]) -> ApiValue:
    """The pages of a paged read as one answer: the first page with every page's rows."""
    first = pages[0]
    if len(pages) == 1 or not isinstance(first, dict):
        return first
    where = ENDPOINTS[label].rows
    rows: list[ApiValue] = []
    for page in pages:
        found = page.get(where) if isinstance(page, dict) else None
        rows.extend(found if isinstance(found, list) else [])
    return {**first, where: rows}


async def read_balances(client: BinanceClient, labels: Sequence[str]) -> dict[str, Read]:
    """Each label read once, one after the other, each with its own bracket."""
    reads: dict[str, Read] = {}
    for label in labels:
        endpoint = ENDPOINTS[label]
        fixed = READ_PARAMS.get(label, {})
        read_from = client.now_ms()
        pages: list[ApiValue] = []
        if endpoint.paging == "page":
            cursor_name, size_name, position = paging_params(endpoint)
            while True:
                params = {**fixed, cursor_name: position, size_name: endpoint.page_size}
                body = await client.call(endpoint, params)
                pages.append(body)
                if len(parse(label, body).rows) < endpoint.page_size:
                    break
                position += 1
        else:
            body = await client.call(endpoint, fixed)
            parse(label, body)
            pages.append(body)
        body = to_json(_merge(label, pages))
        reads[label] = Read(label, body, moment(read_from), moment(client.now_ms()))
    return reads


async def store_read(ctx: SyncContext, read: Read) -> UUID:
    """The read as one raw record, committed in a transaction of its own, as a window's
    records are: it is what the sync saw, whatever fails later, and the normaliser dates a
    refund Binance gives no time for by the sync's reads (`Run.report_time`). Its bracket is
    in the content too: two reads with the same answer are two reads, each with its own
    cutoff."""
    bracket: dict[str, JSONValue] = {
        "read_from_ms": millis(read.read_from),
        "read_to_ms": millis(read.read_to),
    }
    args: dict[str, JSONValue] = {"connection_id": str(ctx.connection.id), "label": read.label}
    async with (
        ctx.session_factory() as session,
        command_context(session, ctx.command.actor, "binance.read", args) as cmd,
    ):
        return await store_raw_record(
            cmd,
            connection_id=ctx.connection.id,
            record_type=record_type(read.label),
            request=bracket,
            content={**bracket, "body": read.body},
            connector_version=VERSION,
            sync_id=ctx.sync_id,
        )


# When a movement falls into a read.


def ambiguous(read: Read, record_times: Collection[datetime]) -> bool:
    """Whether a record's time falls inside the read's bracket widened by 2 seconds."""
    low, high = read.read_from - MARGIN, read.read_to + MARGIN
    return any(low <= at <= high for at in record_times)


def _time_paths(label: str) -> list[tuple[str, list[str]]]:
    """Every time field of a label's record (a deposit's `insertTime` and `completeTime`): its
    catalogue path and the keys from the record to it. Each is a moment the balance may have
    moved."""
    root = layout.root_scope(label)
    paths: list[tuple[str, list[str]]] = []
    for field in FIELDS[label]:
        if field.type not in ("time_ms", "time_text"):
            continue
        tokens = layout.tokens(field.path)
        rest = tokens[len(root) :]
        if tokens[: len(root)] != root or any(t == "[]" or t.startswith("<") for t in rest):
            continue
        paths.append((field.path, list(rest)))
    return paths


async def record_times(ctx: SyncContext) -> list[datetime]:
    """The times of the records this sync fetched or changed. A record stored by an earlier
    sync is older than any read of this one, so only this sync's raw records are asked, and
    only their time fields are read."""
    found: list[datetime] = []
    async with ctx.session_factory() as session:
        for label in [*HISTORY_LABELS, TRADES_LABEL]:
            paths = _time_paths(label)
            if not paths:
                continue
            row = "content -> 'row'" if not ENDPOINTS[label].id_fields else "content"
            values = ", ".join(f"{row} #>> cast(:p{i} as text[])" for i in range(len(paths)))
            result = await session.execute(
                text(
                    f"select {values} from core.raw_records"
                    " where connection_id = :c and sync_id = :s and record_type = :t"
                ),
                {
                    "c": ctx.connection.id,
                    "s": ctx.sync_id,
                    "t": record_type(label),
                    **{f"p{i}": keys for i, (_, keys) in enumerate(paths)},
                },
            )
            for times in result:
                for (path, _), value in zip(paths, times, strict=True):
                    # An absent time (a deposit not yet complete) is no time; one that cannot
                    # be read fails the sync, naming the field, rather than drop out unseen.
                    if value is not None:
                        found.append(tables.time_value(label, path, value))
    return found


async def settle_reads(
    ctx: SyncContext,
    client: BinanceClient,
    reads: dict[str, Read],
    *,
    symbols: Sequence[str] = (),
) -> dict[str, Read | None]:
    """Make the ambiguous reads again, then walk the history labels forwards again (and the
    trades of `symbols`), at most 3 rounds; a read still ambiguous after them is the last one
    made, marked `unclear`.

    Only the reads a snapshot comes from are made again; every other read is kept as it is.
    Each read made again is stored as a raw record, as the first one was.
    """
    settled: dict[str, Read | None] = dict(reads)
    times = await record_times(ctx)
    pending = [
        label for label in SOURCE_LABELS if label in reads and ambiguous(reads[label], times)
    ]
    for _ in range(ROUNDS):
        if not pending:
            break
        again = await read_balances(client, pending)
        for read in again.values():
            await store_read(ctx, read)
        # After the reads: what happened while they were made is what the walks must find.
        for label in HISTORY_LABELS:
            await walk(ctx, client, label)
        if symbols:
            await walk_trades(ctx, client, TRADES_LABEL, symbols)
        times = await record_times(ctx)
        settled.update(again)
        pending = [label for label in pending if ambiguous(again[label], times)]
    for label in pending:
        read = settled[label]
        if read is not None:
            settled[label] = replace(read, unclear=True)
    return settled


# Each wallet's balance per asset.


def _amount(label: str, path: str, value: ApiValue) -> Decimal:
    """An amount field: absent is 0 only where the catalogue says it may be absent; a value
    that is not a finite number is a `BinanceDataError` naming the field."""
    if value is None:
        if _REQUIRED[label].get(path, False):
            raise BinanceDataError(label, path)
        return Decimal(0)
    return tables.decimal_value(label, path, value)


def _per_asset(
    label: str, prefix: str, entries: Sequence[ApiValue | Row], names: Sequence[str]
) -> dict[str, Decimal]:
    """Quantity per asset code of the entries at `prefix` (`balances[].`): the sum of `names`."""
    held: dict[str, Decimal] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            raise BinanceDataError(label, prefix.rstrip("."))
        code = entry.get("asset")
        if not isinstance(code, str):
            raise BinanceDataError(label, f"{prefix}asset")
        total = sum((_amount(label, prefix + name, entry.get(name)) for name in names), Decimal(0))
        held[code] = held.get(code, Decimal(0)) + total
    return held


_REQUIRED = {label: {f.path: f.required for f in fields} for label, fields in FIELDS.items()}
_WALLET_NAME = "[].walletName"
_NAME_MAX = 64


def wallet_name_of(row: Row) -> str:
    """A wallet list row's `walletName`, bounded before it names a wallet key, an account and
    a flag: stripped, 1 to 64 printable characters; anything else is a `BinanceDataError`
    naming the field, never the value."""
    name = row.get("walletName")
    name = name.strip() if isinstance(name, str) else ""
    if not 0 < len(name) <= _NAME_MAX or not name.isprintable():
        raise BinanceDataError(WALLET_LIST, _WALLET_NAME)
    return name


def _listed(row: Row) -> list[ApiValue]:
    found = row.get("assetBalances")
    return found if isinstance(found, list) else []


@dataclass(frozen=True)
class _Wallet:
    key: str
    read: Read
    # Asset code to quantity, zero quantities included.
    held: dict[str, Decimal]


def _wallets(reads: Mapping[str, Read | None]) -> list[_Wallet]:
    """Each wallet whose source read is clear (not None, not `unclear`), with its quantity per
    asset (design 5.6)."""
    wallets: list[_Wallet] = []
    reads = {label: read for label, read in reads.items() if read is not None and not read.unclear}
    spot = reads.get("spot-account")
    if spot is not None:
        rows = parse(spot.label, as_api(spot.body)).rows
        held = _per_asset(spot.label, "balances[].", list(rows), _SPOT_FIELDS)
        wallets.append(_Wallet("spot", spot, held))
    funding = reads.get("funding-asset")
    if funding is not None:
        rows = parse(funding.label, as_api(funding.body)).rows
        held = _per_asset(funding.label, "[].", list(rows), _HELD_FIELDS)
        wallets.append(_Wallet("funding", funding, held))
    listed = reads.get(WALLET_LIST)
    if listed is not None:
        for row in parse(listed.label, as_api(listed.body)).rows:
            key = wallet_key(wallet_name_of(row))
            if key not in OWN_SOURCES:
                wallets.append(_Wallet(key, listed, _listed_held(row)))
    return wallets


def _listed_held(row: Row) -> dict[str, Decimal]:
    return _per_asset(WALLET_LIST, "[].assetBalances[].", _listed(row), _HELD_FIELDS)


def _listed_non_zero(row: Row) -> bool:
    if _amount(WALLET_LIST, "[].balance", row.get("balance")) != 0:
        return True
    return any(quantity != 0 for quantity in _listed_held(row).values())


def wallet_flags(wallet_balance: Sequence[Row], derived: Mapping[str, bool]) -> list[str]:
    """What the wallet list says that the connector cannot account for: a wallet name it does
    not know, and a wallet (by key, in `derived`) whose zero or non-zero state in the list
    disagrees with its own source."""
    flags: list[str] = []
    for row in wallet_balance:
        name = wallet_name_of(row)
        if name not in WALLETS:
            flags.append(f"unknown wallet {name}")
        key = wallet_key(name)
        if key in derived and _listed_non_zero(row) != derived[key]:
            flags.append(f"wallet {name} disagrees with its own report")
    return flags


# The typed report tables.


def _digest(label: str, body: JSONValue) -> str:
    if isinstance(body, dict) and label in _VOLATILE:
        body = {k: v for k, v in body.items() if k not in _VOLATILE[label]}
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


async def store_reports(
    ctx: CommandContext, connection: ConnectionRecord, reads: Mapping[str, Read | None]
) -> list[str]:
    """Upsert each read's typed report tables, keyed by its `read_from` with its `read_to`,
    when its answer differs from the label's last stored one; the labels stored.

    What a report's answer says beside its records goes to its response table with it.
    Reference labels are stored first: `asset` reads `coin-config`.
    """
    config = (await get_connection(ctx.session, connection.id)).config
    stored = config.get(DIGESTS_KEY)
    digests: dict[str, JSONValue] = dict(stored) if isinstance(stored, dict) else {}
    written: list[str] = []
    ordered = sorted(reads.items(), key=lambda item: item[0] not in REFERENCE_LABELS)
    for label, read in ordered:
        if read is None or label not in layout.TABLE_LABELS:
            continue
        digest = _digest(label, read.body)
        if digests.get(label) == digest:
            continue
        body = as_api(read.body)
        key = {"connection_id": connection.id, "snapshot_at": read.read_from}
        for record in tables.records(label, parse(label, body)):
            await tables.store(ctx, label, key, record, {"read_to": read.read_to})
        await tables.store_response(
            ctx, connection.id, label, body, read=(read.read_from, read.read_to)
        )
        digests[label] = digest
        written.append(label)
    if written:
        await update_config(ctx, connection.id, {DIGESTS_KEY: digests})
    return written


# The snapshots.


async def _mapped_account(
    ctx: CommandContext, connection: ConnectionRecord, wallet: str
) -> UUID | None:
    """The wallet's account if it has one; none is made."""
    current = (await get_connection(ctx.session, connection.id)).config.get(ACCOUNT_MAP_KEY)
    found = current.get(wallet) if isinstance(current, dict) else None
    if not isinstance(found, str):
        return None
    try:
        return (await get_account(ctx.session, UUID(found))).id
    except (AccountNotFoundError, ValueError):
        return None


async def _latest(session: AsyncSession, account_id: UUID) -> dict[UUID, Decimal]:
    """The latest reported quantity of each instrument of the account."""
    s = models.BalanceSnapshot
    rows = await session.execute(
        select(s.instrument_id, s.quantity)
        .where(s.account_id == account_id, s.kind == "reported")
        .ext(distinct_on(s.instrument_id))
        .order_by(s.instrument_id, s.as_of.desc(), s.recorded_at.desc(), s.id.desc())
    )
    return {row.instrument_id: row.quantity for row in rows}


async def _snapshot(
    ctx: CommandContext, account_id: UUID, instrument_id: UUID, quantity: Decimal, at: datetime
) -> None:
    await record_snapshot(
        ctx,
        account_id=account_id,
        instrument_id=instrument_id,
        quantity=quantity,
        as_of=at,
        as_of_date=at.astimezone(UTC).date(),
        kind="reported",
    )


async def record_report(
    ctx: CommandContext,
    connection: ConnectionRecord,
    account_id: UUID,
    as_of: datetime,
    read_to: datetime,
) -> None:
    """That the account was reported at `as_of` (its read's `read_from`), whatever the read
    listed: a wallet's report is complete, so an asset it does not list is 0 there, and an
    empty report is a report too."""
    if read_to < as_of:
        raise ValueError("a report's read ends before it starts")
    await ctx.session.execute(
        insert(models.AccountReport)
        .values(connection_id=connection.id, account_id=account_id, as_of=as_of, read_to=read_to)
        .on_conflict_do_nothing()
    )


class _Assets:
    """Asset codes to instruments, each code resolved once."""

    def __init__(self, ctx: CommandContext, connection: ConnectionRecord, flag: Flag) -> None:
        self._ctx, self._connection, self._flag = ctx, connection, flag
        self._found: dict[str, UUID | None] = {}

    async def resolve(self, codes: Collection[str]) -> None:
        # Every other code before an `LD` one, so that the asset a mirror stands for is known
        # when the mirror is met (in another wallet, in the same sync).
        for code in sorted(codes, key=lambda c: (c.startswith(_MIRROR), c)):
            if code not in self._found:
                self._found[code] = await asset(
                    self._ctx, self._connection.id, code, flag=self._flag
                )

    def per_instrument(self, held: Mapping[str, Decimal]) -> dict[UUID, Decimal]:
        quantities: dict[UUID, Decimal] = {}
        for code, quantity in held.items():
            instrument = self._found.get(code)
            if instrument is not None and quantity != 0:
                quantities[instrument] = quantities.get(instrument, Decimal(0)) + quantity
        return {i: q for i, q in quantities.items() if q != 0}


async def report_balances(
    ctx: CommandContext,
    connection: ConnectionRecord,
    reads: Mapping[str, Read | None],
    *,
    flag: Flag | None = None,
) -> dict[tuple[str, UUID], Decimal]:
    """The typed report tables, then one `reported` snapshot per wallet and asset held, at the
    `read_from` of the read its balance came from; what it wrote, by (wallet key, instrument).

    A wallet whose source read is None or `unclear` gets no snapshot (an unclear read's report
    is stored). An instrument whose latest snapshot is not zero and that the wallet no longer
    holds gets a snapshot of 0. An asset never held gets none, and a wallet that holds nothing
    gets no account. `flag` is told about the wallet list
    (`wallet_flags`) and what `asset` flags.
    """
    flag = flag or (lambda _: None)
    listed = reads.get(WALLET_LIST)
    if listed is not None:
        # A wallet's name becomes a key, an account and a flag: it is checked before anything
        # of the read is written.
        for row in parse(listed.label, as_api(listed.body)).rows:
            wallet_name_of(row)
    wallets = _wallets(reads)
    await store_reports(ctx, connection, reads)
    assets = _Assets(ctx, connection, flag)
    await assets.resolve({code for w in wallets for code, q in w.held.items() if q != 0})

    written: dict[tuple[str, UUID], Decimal] = {}
    derived: dict[str, bool] = {}
    for wallet in wallets:
        quantities = assets.per_instrument(wallet.held)
        if wallet.key in OWN_SOURCES:
            derived[wallet.key] = bool(quantities)
        if quantities:
            account = await wallet_account(ctx, connection, wallet.key)
        else:
            account = await _mapped_account(ctx, connection, wallet.key)
        if account is None:
            continue
        await record_report(ctx, connection, account, wallet.read.read_from, wallet.read.read_to)
        latest = await _latest(ctx.session, account)
        gone = {i: Decimal(0) for i, q in latest.items() if q != 0 and i not in quantities}
        for instrument, quantity in {**quantities, **gone}.items():
            await _snapshot(ctx, account, instrument, quantity, wallet.read.read_from)
            written[(wallet.key, instrument)] = quantity

    if listed is not None and not listed.unclear:
        for name in wallet_flags(parse(listed.label, as_api(listed.body)).rows, derived):
            flag(name)
    return written


async def daily_rows(session: AsyncSession, connection_id: UUID) -> list[Row]:
    """The Spot daily snapshots the connection fetched (on its first sync)."""
    contents = await session.scalars(
        text(
            "select content from core.raw_records where connection_id = :c"
            " and record_type = :t and content ->> 'type' = :k"
        ),
        {"c": connection_id, "t": record_type(_DAILY), "k": _DAILY_SPOT},
    )
    return [cast(Row, content) for content in contents if isinstance(content, dict)]


async def _reported_at(
    session: AsyncSession, account_id: UUID, instrument_id: UUID, at: datetime, quantity: Decimal
) -> bool:
    s = models.BalanceSnapshot
    found = await session.scalar(
        select(s.id)
        .where(
            s.account_id == account_id,
            s.instrument_id == instrument_id,
            s.as_of == at,
            s.kind == "reported",
            s.quantity == quantity,
        )
        .limit(1)
    )
    return found is not None


async def backfill_daily_snapshots(
    ctx: CommandContext,
    connection: ConnectionRecord,
    rows: Sequence[Row],
    *,
    flag: Flag | None = None,
) -> int:
    """Each `snapshot` row of type SPOT as `reported` Spot snapshots at its own `updateTime`
    (`free + locked`, `LD` mirrors dropped), an asset the day before held and this day not at
    0; how many were written. A snapshot already written is not written again."""
    flag = flag or (lambda _: None)
    days: list[tuple[datetime, dict[str, Decimal]]] = []
    for row in rows:
        if row.get("type") != _DAILY_SPOT:
            continue
        data = row.get("data")
        balances = data.get("balances") if isinstance(data, dict) else None
        entries = balances if isinstance(balances, list) else []
        at = tables.time_value(_DAILY, "snapshotVos[].updateTime", row.get("updateTime"))
        held = _per_asset(_DAILY, "snapshotVos[].data.balances[].", entries, _SPOT_FIELDS)
        days.append((at, held))
    days.sort(key=lambda day: day[0])
    assets = _Assets(ctx, connection, flag)
    await assets.resolve({code for _, held in days for code, q in held.items() if q != 0})

    account: UUID | None = None
    before: set[UUID] = set()
    written = 0
    for at, held in days:
        quantities = assets.per_instrument(held)
        if quantities and account is None:
            account = await wallet_account(ctx, connection, "spot")
        if account is None:
            continue
        await record_report(ctx, connection, account, at, at)
        gone = {i: Decimal(0) for i in before if i not in quantities}
        for instrument, quantity in {**quantities, **gone}.items():
            if not await _reported_at(ctx.session, account, instrument, at, quantity):
                await _snapshot(ctx, account, instrument, quantity, at)
                written += 1
        before = set(quantities)
    return written
