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
from sqlalchemy.dialects.postgresql import insert
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
    Field,
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
from lararium.modules.finance.binance.instruments import MIRROR, asset
from lararium.modules.finance.binance.parse import (
    ApiValue,
    BinanceDataError,
    Row,
    as_api,
    parse,
    unknown_flags,
)
from lararium.modules.finance.binance.tables import millis, moment
from lararium.modules.finance.errors import AccountNotFoundError
from lararium.modules.finance.snapshots import record_snapshot

type Flag = Callable[[str], None]

# Times Binance gives to the second may be up to a second early (plan Decision 9).
MARGIN = timedelta(seconds=2)
ROUNDS = 3
# The reads a snapshot comes from: only these are made again when a movement falls into them.
SOURCE_LABELS = ("spot-account", "funding-asset", "wallet-balance")
WALLET_LIST = "wallet-balance"
# Reference data, not a wallet's balance: read each sync, stored when it changed.
REFERENCE_LABELS = ("coin-config", "asset-detail", "exchange-info")
# Request parameters a read sends.
READ_PARAMS: dict[str, dict[str, str | int | bool]] = {
    WALLET_LIST: {"needBalanceDetail": True},
    # `btcValuation` does not rest on a default the documents do not give.
    "funding-asset": {"needBtcValuation": True},
    "user-asset": {"needBtcValuation": True},
}
# The digest of each report label's last stored answer: an unchanged answer is not stored again.
DIGESTS_KEY = "report_digests"
# Fields that change with every answer without the report changing.
_VOLATILE = {"exchange-info": frozenset({"serverTime"})}
# Wallets with a source of their own; every other wallet's balance comes from the wallet list
# (design 5.6) until a later phase gives it one.
OWN_SOURCES = {"spot": "spot-account", "funding": "funding-asset"}
_SPOT_FIELDS = ("free", "locked")
_HELD_FIELDS = ("free", "locked", "freeze", "withdrawing")
_DAILY = "snapshot"
# Binance answers the daily snapshot's `type` in lower case (`spot`) although the request asks
# for `SPOT` (the catalogue document): it is compared without case.
_DAILY_SPOT = "SPOT"


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


def _record_paths(label: str, wanted: Callable[[Field], bool]) -> list[tuple[str, list[str]]]:
    """The `wanted` fields of a label's record that are its own (not inside an array or a map
    of it): each one's catalogue path and the keys from the record to it."""
    root = layout.root_scope(label)
    paths: list[tuple[str, list[str]]] = []
    for field in FIELDS[label]:
        if not wanted(field):
            continue
        tokens = layout.tokens(field.path)
        rest = tokens[len(root) :]
        if tokens[: len(root)] != root or any(t == "[]" or t.startswith("<") for t in rest):
            continue
        paths.append((field.path, list(rest)))
    return paths


def _time_paths(label: str) -> list[tuple[str, list[str]]]:
    """Every time field of a label's record (a deposit's `insertTime` and `completeTime`). Each
    is a moment the balance may have moved."""
    return _record_paths(label, lambda field: field.type in ("time_ms", "time_text"))


def _is_asset(field: Field) -> bool:
    name = field.path.rpartition(".")[2]
    return field.type == "text" and (name in ("coin", "asset") or name.endswith("Asset"))


def _asset_paths(label: str) -> list[tuple[str, list[str]]]:
    """Every field of a label's record that names an asset (a deposit's `coin`, a convert's
    `fromAsset` and `toAsset`, a trade's `commissionAsset`)."""
    return _record_paths(label, _is_asset)


def read_assets(reads: Mapping[str, Read | None]) -> set[str]:
    """Every asset a wallet's source read shows a quantity of, clear or not."""
    shown = {
        label: replace(read, unclear=False) for label, read in reads.items() if read is not None
    }
    return {code for wallet in _wallets(shown) for code, q in wallet.held.items() if q != 0}


async def record_assets(ctx: SyncContext, after: UUID | None) -> set[str]:
    """Every asset the connection's records stored after the raw record `after` (all with
    None) name: what the next normalising run will see."""
    found: set[str] = set()
    async with ctx.session_factory() as session:
        for label in [*HISTORY_LABELS, TRADES_LABEL]:
            paths = _asset_paths(label)
            if not paths:
                continue
            row = "content -> 'row'" if not ENDPOINTS[label].id_fields else "content"
            values = ", ".join(f"{row} #>> cast(:p{i} as text[])" for i in range(len(paths)))
            since = "" if after is None else " and id > :after"
            result = await session.execute(
                text(
                    f"select {values} from core.raw_records"
                    f" where connection_id = :c and record_type = :t{since}"
                ),
                {
                    "c": ctx.connection.id,
                    "t": record_type(label),
                    "after": after,
                    **{f"p{i}": keys for i, (_, keys) in enumerate(paths)},
                },
            )
            found |= {code for codes in result for code in codes if code}
    return found


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
) -> dict[str, Read]:
    """Make the ambiguous reads again, then walk the history labels forwards again (and the
    trades of `symbols`), at most 3 rounds; a read still ambiguous after them is the last one
    made, marked `unclear`.

    Only the reads a snapshot comes from are made again; every other read is kept as it is.
    Each read made again is stored as a raw record, as the first one was. After each round
    every source read is checked again against every record this sync fetched, so a record a
    round's walk finds inside a read that was clear until then has that read made again too.
    """
    settled = dict(reads)

    async def unsettled() -> list[str]:
        times = await record_times(ctx)
        return [
            label
            for label in SOURCE_LABELS
            if label in settled and ambiguous(settled[label], times)
        ]

    pending = await unsettled()
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
        settled.update(again)
        pending = await unsettled()
    for label in pending:
        settled[label] = replace(settled[label], unclear=True)
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


def _stated(label: str, where: str, found: ApiValue) -> list[ApiValue]:
    """The list an answer states a wallet's holdings in. A wallet's report is complete (what it
    does not list is 0), so an answer whose list is missing, null or not a list does not state
    them and is never read as an empty report: it is a `BinanceDataError` naming the field.
    An empty list is an empty wallet."""
    if not isinstance(found, list):
        raise BinanceDataError(label, where)
    return found


def _source_rows(read: Read) -> list[Row]:
    """A source read's rows, its list checked by `_stated` first."""
    body = as_api(read.body)
    where = ENDPOINTS[read.label].rows
    _stated(read.label, where, body if where == "." else _at_path(body, where))
    return parse(read.label, body).rows


def _at_path(body: ApiValue, where: str) -> ApiValue:
    for step in where.split("."):
        body = body.get(step) if isinstance(body, dict) else None
    return body


def _list_states_holdings(row: Row, held: Mapping[str, Decimal]) -> bool:
    """Whether a wallet list row states what the wallet holds: a `balance` that is not 0 with
    no quantity listed does not (the wallet is flagged, `wallet_flags`)."""
    balance = _amount(WALLET_LIST, "[].balance", row.get("balance"))
    return balance == 0 or any(quantity != 0 for quantity in held.values())


@dataclass(frozen=True)
class _Wallet:
    key: str
    read: Read
    # Asset code to quantity, zero quantities included.
    held: dict[str, Decimal]


def _wallets(reads: Mapping[str, Read | None]) -> list[_Wallet]:
    """Each wallet whose source read is clear (not None, not `unclear`) and states what it
    holds, with its quantity per asset (design 5.6). A source whose list is not stated fails
    (`_stated`); a wallet the list shows with no asset detail, or with a balance but no
    quantity, is left out (and flagged, `wallet_flags`): it gets no snapshot and no report time
    from this read, and its earlier balance stands."""
    wallets: list[_Wallet] = []
    reads = {label: read for label, read in reads.items() if read is not None and not read.unclear}
    spot = reads.get("spot-account")
    if spot is not None:
        held = _per_asset(spot.label, "balances[].", _source_rows(spot), _SPOT_FIELDS)
        wallets.append(_Wallet("spot", spot, held))
    funding = reads.get("funding-asset")
    if funding is not None:
        held = _per_asset(funding.label, "[].", _source_rows(funding), _HELD_FIELDS)
        wallets.append(_Wallet("funding", funding, held))
    listed = reads.get(WALLET_LIST)
    if listed is not None:
        for row in _source_rows(listed):
            key = wallet_key(wallet_name_of(row))
            if key in OWN_SOURCES:
                continue
            entries = row.get("assetBalances")
            if not isinstance(entries, list):
                # No detail: the list is read again next sync (flagged, `wallet_flags`).
                continue
            held = _per_asset(WALLET_LIST, "[].assetBalances[].", entries, _HELD_FIELDS)
            if _list_states_holdings(row, held):
                wallets.append(_Wallet(key, listed, held))
    return wallets


def _listed_held(row: Row) -> dict[str, Decimal]:
    return _per_asset(WALLET_LIST, "[].assetBalances[].", _listed(row), _HELD_FIELDS)


def _listed_non_zero(row: Row) -> bool:
    if _amount(WALLET_LIST, "[].balance", row.get("balance")) != 0:
        return True
    return any(quantity != 0 for quantity in _listed_held(row).values())


def wallet_flags(wallet_balance: Sequence[Row], derived: Mapping[str, bool]) -> list[str]:
    """What the wallet list says that the connector cannot account for: a wallet name it does
    not know, a wallet (by key, in `derived`) whose zero or non-zero state in the list
    disagrees with its own source, and a wallet counted from the list whose `balance` is not
    zero but which lists no asset to count."""
    flags: list[str] = []
    for row in wallet_balance:
        name = wallet_name_of(row)
        if name not in WALLETS:
            flags.append(f"unknown wallet {name}")
        key = wallet_key(name)
        if key in derived and _listed_non_zero(row) != derived[key]:
            flags.append(f"wallet {name} disagrees with its own report")
        balance = _amount(WALLET_LIST, "[].balance", row.get("balance"))
        listed = any(quantity != 0 for quantity in _listed_held(row).values())
        if key in OWN_SOURCES:
            continue
        if not isinstance(row.get("assetBalances"), list):
            flags.append(f"wallet {name} lists no asset detail")
        elif balance != 0 and not listed:
            flags.append(f"wallet {name} has a balance but lists no asset")
    return flags


# The typed report tables.


def _digest(label: str, body: JSONValue) -> str:
    if isinstance(body, dict) and label in _VOLATILE:
        body = {k: v for k, v in body.items() if k not in _VOLATILE[label]}
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


async def store_reports(
    ctx: CommandContext,
    connection: ConnectionRecord,
    reads: Mapping[str, Read | None],
    *,
    flag: Flag | None = None,
) -> list[str]:
    """Upsert each read's typed report tables, keyed by its `read_from` with its `read_to`,
    when its answer differs from the label's last stored one; the labels stored.

    What a report's answer says beside its records goes to its response table with it.
    Reference labels are stored first: `asset` reads `coin-config`. `flag` is told what the
    parser finds unknown in every read, stored or not (a flag is published once).
    """
    flag = flag or (lambda _: None)
    config = (await get_connection(ctx.session, connection.id)).config
    stored = config.get(DIGESTS_KEY)
    digests: dict[str, JSONValue] = dict(stored) if isinstance(stored, dict) else {}
    written: list[str] = []
    ordered = sorted(reads.items(), key=lambda item: item[0] not in REFERENCE_LABELS)
    for label, read in ordered:
        if read is None:
            continue
        body = as_api(read.body)
        parsed = parse(label, body)
        for name in unknown_flags(label, parsed):
            flag(name)
        if label not in layout.TABLE_LABELS:
            continue
        digest = _digest(label, read.body)
        if digests.get(label) == digest:
            continue
        key = {"connection_id": connection.id, "snapshot_at": read.read_from}
        for record in tables.records(label, parsed):
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


async def _held_at(session: AsyncSession, account_id: UUID, at: datetime) -> dict[UUID, Decimal]:
    """Each instrument's balance of the account at `at` by the `hybrid` rule valuation uses
    (`v1_balances_asof`): its latest snapshot up to `at` plus the non-void postings after that
    snapshot up to `at`; with no snapshot, the sum of its postings up to `at`."""
    rows = await session.execute(
        text(
            "with snaps as ("
            "  select distinct on (instrument_id) instrument_id, quantity, as_of"
            "  from finance.balance_snapshots where account_id = :a and as_of <= :t"
            "  order by instrument_id, as_of_date desc, as_of desc, recorded_at desc, id desc"
            "), posted as ("
            "  select p.instrument_id, p.quantity, t.occurred_at from finance.postings p"
            "  join finance.transactions t on t.id = p.transaction_id"
            "  where p.account_id = :a and t.status <> 'void' and t.occurred_at <= :t"
            "), keys as ("
            "  select instrument_id from snaps union select instrument_id from posted"
            ")"
            " select k.instrument_id, coalesce(s.quantity, 0) + coalesce(sum(p.quantity)"
            "  filter (where s.as_of is null or p.occurred_at > s.as_of), 0) as held"
            " from keys k left join snaps s on s.instrument_id = k.instrument_id"
            " left join posted p on p.instrument_id = k.instrument_id"
            " group by k.instrument_id, s.quantity, s.as_of"
        ),
        {"a": account_id, "t": at},
    )
    return {row.instrument_id: Decimal(row.held) for row in rows}


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
    *,
    listed: Collection[UUID],
) -> list[UUID]:
    """That the account was reported at `as_of` (its read's `read_from`), whatever the read
    listed, and what that means for the instruments it did not list; the instruments given a
    snapshot of 0.

    A wallet's report is complete: an instrument it does not list (`listed`: those it shows a
    quantity of) is 0 there, and an empty report is a report too. So every other instrument
    whose balance at `as_of` is not 0 (`_held_at`: the `hybrid` rule, which adds postings to
    the latest snapshot) gets a `reported` snapshot of 0 then; otherwise the records would
    stand in for what the report said. Writing one twice writes nothing: the 0 is then the
    balance."""
    if read_to < as_of:
        raise ValueError("a report's read ends before it starts")
    await ctx.session.execute(
        insert(models.AccountReport)
        .values(connection_id=connection.id, account_id=account_id, as_of=as_of, read_to=read_to)
        .on_conflict_do_nothing()
    )
    held = await _held_at(ctx.session, account_id, as_of)
    gone = sorted(i for i, quantity in held.items() if quantity != 0 and i not in listed)
    for instrument in gone:
        await _snapshot(ctx, account_id, instrument, Decimal(0), as_of)
    return gone


class _Assets:
    """Asset codes to instruments, each code resolved once."""

    def __init__(self, ctx: CommandContext, connection: ConnectionRecord, flag: Flag) -> None:
        self._ctx, self._connection, self._flag = ctx, connection, flag
        self._found: dict[str, UUID | None] = {}

    async def resolve(self, codes: Collection[str]) -> None:
        # Every other code before an `LD` one, so that the asset a mirror stands for is known
        # when the mirror is met (in another wallet, in the same sync).
        for code in sorted(codes, key=lambda c: (c.startswith(MIRROR), c)):
            if code not in self._found:
                self._found[code] = await asset(
                    self._ctx, self._connection.id, code, flag=self._flag
                )

    def unresolved(self, held: Mapping[str, Decimal]) -> str | None:
        """The first code held (not zero) that has no instrument and is not an `LD` mirror
        (the only code `asset` gives none); None when every quantity has its instrument."""
        for code, quantity in sorted(held.items()):
            if quantity != 0 and self._found.get(code) is None and not code.startswith(MIRROR):
                return code
        return None

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
    is stored). An instrument the report does not list and whose balance is not 0 then gets
    a snapshot of 0 (`record_report`). An asset never held gets none, and a wallet that holds
    nothing gets no account. `flag` is told about the wallet list (`wallet_flags`) and what
    `asset` flags.
    """
    flag = flag or (lambda _: None)
    listed = reads.get(WALLET_LIST)
    if listed is not None:
        # A wallet's name becomes a key, an account and a flag: it is checked before anything
        # of the read is written.
        for row in parse(listed.label, as_api(listed.body)).rows:
            wallet_name_of(row)
    wallets = _wallets(reads)
    await store_reports(ctx, connection, reads, flag=flag)
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
        at = wallet.read.read_from
        for instrument, quantity in quantities.items():
            await _snapshot(ctx, account, instrument, quantity, at)
            written[(wallet.key, instrument)] = quantity
        for instrument in await record_report(
            ctx, connection, account, at, wallet.read.read_to, listed=set(quantities)
        ):
            written[(wallet.key, instrument)] = Decimal(0)

    if listed is not None and not listed.unclear:
        for name in wallet_flags(parse(listed.label, as_api(listed.body)).rows, derived):
            flag(name)
    return written


async def record_reads(
    ctx: CommandContext,
    connection: ConnectionRecord,
    reads: Mapping[str, Read | None],
    *,
    daily: bool = False,
    flag: Flag | None = None,
) -> None:
    """A clear read is a report for its wallet whether or not the account existed when it was
    read: for every wallet whose source read is clear and whose account exists now, its report
    (`record_report`: its time, and a 0 for each instrument it does not list that the records
    would otherwise hold then); with `daily`, the first sync's daily snapshots again
    (`backfill_daily_snapshots`), which reports the days it writes on the Spot account if it
    exists now. Run, in the transaction that applies the records, after they made the accounts
    and postings they name; writing one twice writes nothing."""
    wallets = _wallets(reads)
    assets = _Assets(ctx, connection, flag or (lambda _: None))
    await assets.resolve({code for w in wallets for code, q in w.held.items() if q != 0})
    for wallet in wallets:
        account = await _mapped_account(ctx, connection, wallet.key)
        if account is not None:
            shown = set(assets.per_instrument(wallet.held))
            await record_report(
                ctx, connection, account, wallet.read.read_from, wallet.read.read_to, listed=shown
            )
    if daily:
        rows = await daily_rows(ctx.session, connection.id)
        await backfill_daily_snapshots(ctx, connection, rows, flag=flag)


async def daily_rows(session: AsyncSession, connection_id: UUID) -> list[Row]:
    """The Spot daily snapshots the connection fetched (on its first sync)."""
    contents = await session.scalars(
        text(
            "select content from core.raw_records where connection_id = :c"
            " and record_type = :t and upper(content ->> 'type') = :k"
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


def _daily_unstated(entries: ApiValue) -> str | None:
    """The field by which a daily Spot snapshot's balances do not state what Spot held: the
    list missing, null or not a list, or an entry without its asset or an amount."""
    where = "snapshotVos[].data.balances"
    if not isinstance(entries, list):
        return where
    for entry in entries:
        if isinstance(entry, dict):
            for name in ("asset", *_SPOT_FIELDS):
                if entry.get(name) is None:
                    return f"{where}[].{name}"
    return None


async def backfill_daily_snapshots(
    ctx: CommandContext,
    connection: ConnectionRecord,
    rows: Sequence[Row],
    *,
    flag: Flag | None = None,
) -> int:
    """Each `snapshot` row of type SPOT as `reported` Spot snapshots at its own `updateTime`
    (`free + locked`, `LD` mirrors dropped), an asset held then that the day does not list at
    0 (`record_report`); how many were written. A snapshot already written is not written
    again.

    A day's report time (`record_report`) is written with its snapshots, by this pass alone: a
    report says every instrument it does not list was 0, so a day is reported only when the
    Spot account exists and every quantity of the day has its instrument. A day with a code
    that has none (other than an `LD` mirror), or whose balances do not state what Spot held
    (`_daily_unstated`), is not written at all and is flagged."""
    flag = flag or (lambda _: None)
    days: list[tuple[datetime, dict[str, Decimal]]] = []
    for row in rows:
        kind = row.get("type")
        if not isinstance(kind, str) or kind.upper() != _DAILY_SPOT:
            continue
        data = row.get("data")
        entries = data.get("balances") if isinstance(data, dict) else None
        # Fetched on the first sync only, so never read again: a day that does not state what
        # Spot held is left out and flagged, neither read as an empty day nor failing the sync.
        unstated = _daily_unstated(entries)
        if unstated is not None:
            flag(f"a daily Spot snapshot was not written: {_DAILY}.{unstated} is missing")
            continue
        assert isinstance(entries, list)
        at = tables.time_value(_DAILY, "snapshotVos[].updateTime", row.get("updateTime"))
        held = _per_asset(_DAILY, "snapshotVos[].data.balances[].", entries, _SPOT_FIELDS)
        days.append((at, held))
    days.sort(key=lambda day: day[0])
    assets = _Assets(ctx, connection, flag)
    await assets.resolve({code for _, held in days for code, q in held.items() if q != 0})

    account = await _mapped_account(ctx, connection, "spot")
    written = 0
    for at, held in days:
        missing = assets.unresolved(held)
        if missing is not None:
            flag(f"a daily Spot snapshot was not written: {missing} has no instrument")
            continue
        quantities = assets.per_instrument(held)
        if quantities and account is None:
            account = await wallet_account(ctx, connection, "spot")
        if account is None:
            continue
        for instrument, quantity in quantities.items():
            if not await _reported_at(ctx.session, account, instrument, at, quantity):
                await _snapshot(ctx, account, instrument, quantity, at)
                written += 1
        written += len(await record_report(ctx, connection, account, at, at, listed=quantities))
    return written
