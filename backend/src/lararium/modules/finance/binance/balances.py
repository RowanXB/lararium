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
from collections.abc import Awaitable, Callable, Collection, Mapping, Sequence
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
from lararium.modules.finance.binance.accounts import (
    ACCOUNT_MAP_KEY,
    wallet_account,
    wallet_name,
)
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
    ACCOUNT_INFO_LABEL,
    HISTORY_LABELS,
    ISOLATED_MARGIN_LABEL,
    TRADE_LABELS,
    VERSION,
    TradeWalk,
    record_type,
    to_json,
    typed,
    walk,
    walk_trades,
)
from lararium.modules.finance.binance.instruments import MIRROR, asset, uncounted
from lararium.modules.finance.binance.parse import (
    ApiValue,
    BinanceDataError,
    Row,
    as_api,
    parse,
    row_prefix,
    unknown_flags,
)
from lararium.modules.finance.binance.tables import millis, moment
from lararium.modules.finance.errors import AccountNotFoundError
from lararium.modules.finance.snapshots import record_snapshot

type Flag = Callable[[str], None]

# Times Binance gives to the second may be up to a second early (plan Decision 9).
MARGIN = timedelta(seconds=2)
ROUNDS = 3
# The two reads of the Crypto Loans wallet, flexible first, made one after the other: its debt
# per loan coin is minus the `totalDebt` of every ongoing order of both (design 5.6). Their one
# report is cut at the first read's start and is clear only when nothing happened from then to
# the end of the second (`loan_bracket`), so they are made again together.
LOAN_READS = ("loan-flexible-ongoing", "vip-loan-ongoing")
# The two margin accounts: Cross Margin holds each asset's `netAsset`, Isolated Margin the base
# and quote `netAsset` of each of its pairs, summed per asset (design 5.6).
CROSS_MARGIN_LABEL = "margin-account"
# The futures wallets' reports: each account's `assets[]`, an asset's balance its wallet balance
# plus its unrealised profit (design 5.6). Reconciliation compares the records with the change of
# the wallet balance alone (`reconcile.reported_at`, plan Decision 11).
FUTURES_SOURCES = {"um_futures": "um-account", "cm_futures": "cm-account"}
_FUTURES_FIELDS = ("walletBalance", "unrealizedProfit")
# The Options wallet's report: its margin account's `asset[]`, an asset's balance its `equity`
# (design 5.6), which Binance's example gives as the margin balance plus the unrealised profit.
# Reconciliation compares the records with the change of the margin balance alone
# (`reconcile.reported_at`), as a futures wallet's with its wallet balance.
OPTIONS_SOURCES = {"options": "opt-account"}
# The Portfolio Margin wallet's report (while `account-info` says it is on): its balance's rows,
# an asset's balance its `totalWalletBalance` less what it owes (`crossMarginBorrowed`,
# `crossMarginInterest`: a borrowed asset is negative, as in Cross Margin) plus its unrealised
# USDⓈ-M and COIN-M profit (design 5.6). Binance's example has `totalWalletBalance` equal to the
# cross margin free and locked amounts plus the UM and CM wallet balances, debt not netted.
# Reconciliation compares the records with the change of the net wallet balance alone
# (`reconcile.reported_at`), as a futures wallet's.
PORTFOLIO_SOURCES = {"portfolio_margin": "pm-balance"}
# The wallets Portfolio Margin holds the content of while it is on (its balance states each
# asset's cross margin, UM and CM parts): each gets an empty report when it is seen on, and the
# Portfolio Margin wallet one when it is seen off (`empty_reports`), so no asset counts twice.
PORTFOLIO_REPLACED = ("cross_margin", "um_futures", "cm_futures")
# `account-info`'s word that the account uses Portfolio Margin (design 5.13).
PORTFOLIO_MARGIN_FIELD = "isPortfolioMarginRetailEnabled"
# Each derivatives account's quantity per asset: the sum of these fields of its rows.
_ACCOUNT_FIELDS: dict[str, tuple[str, ...]] = {
    **dict.fromkeys(FUTURES_SOURCES.values(), _FUTURES_FIELDS),
    "opt-account": ("equity",),
    "pm-balance": ("totalWalletBalance", "umUnrealizedPNL", "cmUnrealizedPNL"),
}
# Fields an account's rows owe, taken from the sum: Portfolio Margin's debt.
_ACCOUNT_OWED: dict[str, tuple[str, ...]] = {
    "pm-balance": ("crossMarginBorrowed", "crossMarginInterest"),
}
# Fields a derivatives account must state for each asset beside those summed: what reconciliation
# compares with the records (`reconcile.ACCOUNT_REPORTS`). A futures account sums its wallet
# balance already.
_ACCOUNT_STATED: dict[str, tuple[str, ...]] = {"opt-account": ("marginBalance",)}
# The reads a snapshot comes from: only these are made again when a movement falls into them.
SOURCE_LABELS = (
    "spot-account",
    "funding-asset",
    "wallet-balance",
    *LOAN_READS,
    CROSS_MARGIN_LABEL,
    ISOLATED_MARGIN_LABEL,
    *FUTURES_SOURCES.values(),
    *OPTIONS_SOURCES.values(),
    *PORTFOLIO_SOURCES.values(),
)
WALLET_LIST = "wallet-balance"
# Reference data, not a wallet's balance: read each sync, stored when it changed.
REFERENCE_LABELS = (
    "coin-config",
    "asset-detail",
    "exchange-info",
    "um-exchange-info",
    "cm-exchange-info",
    "opt-exchange-info",
)
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
_VOLATILE = {
    label: frozenset({"serverTime"})
    for label in ("exchange-info", "um-exchange-info", "cm-exchange-info", "opt-exchange-info")
}
# Wallets with a source of their own; every other wallet's balance comes from the wallet list
# (design 5.6) until a later phase gives it one.
OWN_SOURCES = {
    "spot": "spot-account",
    "funding": "funding-asset",
    "loans": LOAN_READS[0],
    "cross_margin": CROSS_MARGIN_LABEL,
    "isolated_margin": ISOLATED_MARGIN_LABEL,
    **FUTURES_SOURCES,
    **OPTIONS_SOURCES,
    **PORTFOLIO_SOURCES,
}
# The wallets counted from the wallet list for which Binance has no record endpoint (design
# 5.13): any change of theirs is a reconciliation difference, so a balance is flagged once.
RECORD_LESS = ("trading_bots", "copy_trading")
LOANS = "loans"
_SPOT_FIELDS = ("free", "locked")
_NET_FIELDS = ("netAsset",)
_LEGS = ("baseAsset", "quoteAsset")
_HELD_FIELDS = ("free", "locked", "freeze", "withdrawing")
_DAILY = "snapshot"


@dataclass(frozen=True)
class _DailyWallet:
    """Which wallet a daily snapshot of one `type` reports, where its quantities are and which
    of their fields sum to an asset's quantity there."""

    wallet: str
    entries: str
    fields: tuple[str, ...]


# The daily snapshot types written as reports, by the request's `type`. Binance answers the
# daily snapshot's `type` in lower case (`spot`) although the request asks for `SPOT` (the
# catalogue document): it is compared without case. Cross Margin's day holds each asset's
# `netAsset`, as its account does. A FUTURES day is USDⓈ-M's (Binance's example lists a USDT
# asset and a BTCUSDT position; the page does not say which futures wallet): its `walletBalance`
# alone, as it gives no unrealised profit to add (its `marginBalance` "can ignore", its
# positions' profit is "at the time of opening"). That is the balance reconciliation compares.
DAILY_WALLETS: dict[str, _DailyWallet] = {
    "SPOT": _DailyWallet("spot", "balances", _SPOT_FIELDS),
    "MARGIN": _DailyWallet("cross_margin", "userAssets", _NET_FIELDS),
    "FUTURES": _DailyWallet("um_futures", "assets", ("walletBalance",)),
}


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
                if len(typed(label, body).rows) < endpoint.page_size:
                    break
                position += 1
        else:
            body = await client.call(endpoint, fixed)
            typed(label, body)
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
        for label in [*HISTORY_LABELS, *TRADE_LABELS]:
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
        for label in [*HISTORY_LABELS, *TRADE_LABELS]:
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


type Reread = Callable[[str], Awaitable[Read | None]]
type Rewalk = Callable[[str], Awaitable[object]]


async def settle_reads(
    ctx: SyncContext,
    client: BinanceClient,
    reads: Mapping[str, Read],
    *,
    trades: Sequence[TradeWalk] = (),
    reread: Reread | None = None,
    rewalk: Rewalk | None = None,
) -> dict[str, Read | None]:
    """Make the ambiguous reads again, then walk the history labels forwards again (and the
    trades of `trades`), at most 3 rounds; a read still ambiguous after them is the last one
    made, marked `unclear`.

    Only the reads a snapshot comes from are made again; every other read is kept as it is.
    Each read made again is stored as a raw record, as the first one was. After each round
    every source read is checked again against every record this sync fetched, so a record a
    round's walk finds inside a read that was clear until then has that read made again too.

    `reread` makes one read again and `rewalk` walks one history label; the sync's handle a
    refusal as its first reads and walks do (`sync.read_or_absent`, `sync.on_host`). A read
    `reread` gives None for (not served now, its host refused the key) is None in the result:
    its wallet gets no snapshot in this sync, as an unclear one does.
    """
    settled: dict[str, Read | None] = dict(reads)

    async def read_again(label: str) -> Read | None:
        return (await read_balances(client, [label]))[label]

    async def walk_again(label: str) -> object:
        return await walk(ctx, client, label)

    reread = reread or read_again
    rewalk = rewalk or walk_again

    async def unsettled() -> list[str]:
        times = await record_times(ctx)
        loans = loan_bracket(settled)
        unclear_loans = loans is not None and ambiguous(loans, times)
        return [
            label
            for label in SOURCE_LABELS
            if (read := settled.get(label)) is not None
            and (unclear_loans if label in LOAN_READS else ambiguous(read, times))
        ]

    pending = await unsettled()
    for _ in range(ROUNDS):
        if not pending:
            break
        again = {label: await reread(label) for label in pending}
        for read in again.values():
            if read is not None:
                await store_read(ctx, read)
        # After the reads: what happened while they were made is what the walks must find.
        for label in HISTORY_LABELS:
            await rewalk(label)
        for trade in trades:
            if trade.symbols:
                await walk_trades(ctx, client, trade.label, trade.symbols, isolated=trade.isolated)
        settled.update(again)
        pending = await unsettled()
    for label in pending:
        read = settled[label]
        if read is not None:
            settled[label] = replace(read, unclear=True)
    return settled


# Each wallet's balance per asset.


def _amount(label: str, path: str, value: ApiValue) -> Decimal:
    """An amount the code sums or compares: absent, or not a finite number, is a
    `BinanceDataError` naming the field, whatever the catalogue's flag. A quantity left out
    is never read as 0."""
    if value is None:
        raise BinanceDataError(label, path)
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


def loan_bracket(reads: Mapping[str, Read | None]) -> Read | None:
    """The Crypto Loans wallet's one read: from the start of the first of its reads the sync
    made to the end of the last (`LOAN_READS`; a product Binance does not serve is not read and
    holds no debt). None when it made neither; `unclear` when one of them is, or is None."""
    made = [reads[label] for label in LOAN_READS if label in reads]
    if not made:
        return None
    present = [read for read in made if read is not None]
    if not present:
        return None
    unclear = len(present) < len(made) or any(read.unclear for read in present)
    return replace(
        present[0],
        read_from=min(read.read_from for read in present),
        read_to=max(read.read_to for read in present),
        unclear=unclear,
    )


def _loan_rows(read: Read) -> list[Row]:
    """An ongoing-orders read's rows. An answer that leaves its list out is empty only when its
    `total` says it has no records; otherwise it does not state the debt (`_stated`)."""
    body = as_api(read.body)
    if isinstance(body, dict) and "rows" not in body and body.get("total") in (0, "0"):
        return []
    return _source_rows(read)


def margin_created(read: Read) -> bool:
    """Whether a cross margin account read says the account exists: an answer `created: false`
    is the account Binance has not opened, which is not present (design 6)."""
    body = as_api(read.body)
    return not (isinstance(body, dict) and body.get("created") is False)


def _isolated_held(read: Read) -> dict[str, Decimal]:
    """Each asset's net quantity over the isolated pairs created: the base and the quote leg's
    `netAsset`, summed per asset; a pair not created holds nothing."""
    label = read.label
    held: dict[str, Decimal] = {}
    for row in _source_rows(read):
        if row.get("isolatedCreated") is False:
            continue
        for leg in _LEGS:
            entry = row.get(leg)
            if not isinstance(entry, dict):
                raise BinanceDataError(label, f"assets[].{leg}")
            code = entry.get("asset")
            if not isinstance(code, str):
                raise BinanceDataError(label, f"assets[].{leg}.asset")
            net = _amount(label, f"assets[].{leg}.netAsset", entry.get("netAsset"))
            held[code] = held.get(code, Decimal(0)) + net
    return held


def _debts(reads: Mapping[str, Read | None]) -> _Wallet | None:
    """The Crypto Loans wallet from its clear reads: minus the `totalDebt` of every ongoing
    order, per loan coin (design 5.6)."""
    bracket = loan_bracket(reads)
    if bracket is None or bracket.unclear:
        return None
    held: dict[str, Decimal] = {}
    for label in LOAN_READS:
        read = reads.get(label)
        if read is None:
            continue
        for row in _loan_rows(read):
            code = row.get("loanCoin")
            if not isinstance(code, str):
                raise BinanceDataError(label, "rows[].loanCoin")
            debt = _amount(label, "rows[].totalDebt", row.get("totalDebt"))
            held[code] = held.get(code, Decimal(0)) - debt
    return _Wallet(LOANS, bracket, held)


def _wallets(reads: Mapping[str, Read | None]) -> list[_Wallet]:
    """Each wallet whose source read is clear (not None, not `unclear`) and states what it
    holds, with its quantity per asset (design 5.6). A source whose list is not stated fails
    (`_stated`); a wallet the list shows with no asset detail, or with a balance but no
    quantity, is left out (and flagged, `wallet_flags`): it gets no snapshot and no report time
    from this read, and its earlier balance stands."""
    wallets: list[_Wallet] = []
    loans = _debts(reads)
    reads = {label: read for label, read in reads.items() if read is not None and not read.unclear}
    spot = reads.get("spot-account")
    if spot is not None:
        held = _per_asset(spot.label, "balances[].", _source_rows(spot), _SPOT_FIELDS)
        wallets.append(_Wallet("spot", spot, held))
    funding = reads.get("funding-asset")
    if funding is not None:
        held = _per_asset(funding.label, "[].", _source_rows(funding), _HELD_FIELDS)
        wallets.append(_Wallet("funding", funding, held))
    cross = reads.get(CROSS_MARGIN_LABEL)
    if cross is not None and margin_created(cross):
        held = _per_asset(cross.label, "userAssets[].", _source_rows(cross), _NET_FIELDS)
        wallets.append(_Wallet("cross_margin", cross, held))
    isolated = reads.get(ISOLATED_MARGIN_LABEL)
    if isolated is not None:
        wallets.append(_Wallet("isolated_margin", isolated, _isolated_held(isolated)))
    for key, label in {**FUTURES_SOURCES, **OPTIONS_SOURCES, **PORTFOLIO_SOURCES}.items():
        account = reads.get(label)
        if account is not None:
            prefix = row_prefix(label)
            rows = _source_rows(account)
            held = _per_asset(label, prefix, rows, _ACCOUNT_FIELDS[label])
            owed = _per_asset(label, prefix, rows, _ACCOUNT_OWED.get(label, ()))
            held = {code: quantity - owed.get(code, Decimal(0)) for code, quantity in held.items()}
            # Never read as 0 where it is left out.
            _per_asset(label, prefix, rows, _ACCOUNT_STATED.get(label, ()))
            wallets.append(_Wallet(key, account, held))
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
    if loans is not None:
        wallets.append(loans)
    return wallets


def _listed_held(row: Row) -> dict[str, Decimal]:
    return _per_asset(WALLET_LIST, "[].assetBalances[].", _listed(row), _HELD_FIELDS)


def _listed_non_zero(row: Row) -> bool:
    if _amount(WALLET_LIST, "[].balance", row.get("balance")) != 0:
        return True
    return any(quantity != 0 for quantity in _listed_held(row).values())


def record_less_flag(name: str) -> str:
    return f"{name} has a balance: Binance lists no records for it"


def unread_flag(name: str) -> str:
    """A wallet the list shows a balance in whose own source this sync did not read
    (`unread_wallets`): it is not counted."""
    return f"wallet {name} has a balance but its own report was not read: it is not counted"


def portfolio_margin_on(account_info: Read) -> bool:
    """Whether an `account-info` read says Portfolio Margin is on; a field left out is off."""
    body = as_api(account_info.body)
    return isinstance(body, dict) and body.get(PORTFOLIO_MARGIN_FIELD) is True


def unread_wallets(reads: Mapping[str, Read | None]) -> set[str]:
    """The wallets with a source of their own (`OWN_SOURCES`) whose source this sync did not
    read: not served (`not_present`, a cross margin account not created), its host refused the
    key, or made again and refused (`settle_reads`). Not those the Portfolio Margin state of
    this sync's `account-info` read leaves out on purpose: on, the wallets it holds; off, its
    own. A source read that is only unclear was read."""
    account_info = reads.get(ACCOUNT_INFO_LABEL)
    on = account_info is not None and portfolio_margin_on(account_info)
    replaced = set(PORTFOLIO_REPLACED) if on else set(PORTFOLIO_SOURCES)
    unread = {wallet for wallet, label in OWN_SOURCES.items() if reads.get(label) is None}
    cross = reads.get(CROSS_MARGIN_LABEL)
    if cross is not None and not margin_created(cross):
        unread.add("cross_margin")
    return unread - replaced


def wallet_flags(
    wallet_balance: Sequence[Row], derived: Mapping[str, bool], unread: Collection[str] = ()
) -> list[str]:
    """What the wallet list says that the connector cannot account for: a wallet name it does
    not know, a wallet (by key, in `derived`) whose zero or non-zero state in the list
    disagrees with its own source, a wallet (by key, in `unread`) the list shows a balance in
    whose own source was not read, a wallet counted from the list whose `balance` is not zero
    but which lists no asset to count, and a wallet with no record endpoint (`RECORD_LESS`)
    that is not empty."""
    flags: list[str] = []
    for row in wallet_balance:
        name = wallet_name_of(row)
        if name not in WALLETS:
            flags.append(f"unknown wallet {name}")
        key = wallet_key(name)
        if key in derived and _listed_non_zero(row) != derived[key]:
            flags.append(f"wallet {name} disagrees with its own report")
        if key in unread and _listed_non_zero(row):
            flags.append(unread_flag(name))
        balance = _amount(WALLET_LIST, "[].balance", row.get("balance"))
        listed = any(quantity != 0 for quantity in _listed_held(row).values())
        if key in OWN_SOURCES:
            continue
        if not isinstance(row.get("assetBalances"), list):
            flags.append(f"wallet {name} lists no asset detail")
        elif balance != 0 and not listed:
            flags.append(f"wallet {name} has a balance but lists no asset")
        if key in RECORD_LESS and _listed_non_zero(row):
            flags.append(record_less_flag(name))
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


async def holding_wallets(
    session: AsyncSession, connection_id: UUID, wallets: Collection[str], at: datetime
) -> list[str]:
    """The `wallets` whose account exists and holds something at `at` (`_held_at`, the `hybrid`
    rule): those an empty report at `at` would change. A wallet whose last report is empty
    already, with no posting after it, holds nothing."""
    config = (await get_connection(session, connection_id)).config
    mapped = config.get(ACCOUNT_MAP_KEY)
    holding: list[str] = []
    for wallet in wallets:
        found = mapped.get(wallet) if isinstance(mapped, dict) else None
        if not isinstance(found, str):
            continue
        try:
            account = (await get_account(session, UUID(found))).id
        except (AccountNotFoundError, ValueError):
            continue
        if any(quantity != 0 for quantity in (await _held_at(session, account, at)).values()):
            holding.append(wallet)
    return holding


async def empty_reports(
    ctx: CommandContext, connection: ConnectionRecord, wallets: Collection[str], read: Read
) -> list[str]:
    """An empty, complete report at the `read`'s bracket for each of the wallets that has an
    account: another report holds their content now (ruling R21: Portfolio Margin on holds
    Cross Margin's and the futures wallets'; off, the classic wallets hold its), so what their
    last reports said must stop counting. Each instrument they held gets a snapshot of 0
    (`record_report`); the wallets reported."""
    reported: list[str] = []
    for wallet in wallets:
        account = await _mapped_account(ctx, connection, wallet)
        if account is not None:
            await record_report(ctx, connection, account, read.read_from, read.read_to, listed=())
            reported.append(wallet)
    return reported


class _Assets:
    """Asset codes to instruments, each code resolved once."""

    def __init__(self, ctx: CommandContext, connection: ConnectionRecord, flag: Flag) -> None:
        self._ctx, self._connection, self._flag = ctx, connection, flag
        self._found: dict[str, UUID | None] = {}
        # Legal money Lararium has no currency of: no instrument, and not counted.
        self.uncounted: set[str] = set()

    async def resolve(self, codes: Collection[str]) -> None:
        # Every other code before an `LD` one, so that the asset a mirror stands for is known
        # when the mirror is met (in another wallet, in the same sync).
        for code in sorted(codes, key=lambda c: (c.startswith(MIRROR), c)):
            if code not in self._found:
                found = await asset(self._ctx, self._connection.id, code, flag=self._flag)
                self._found[code] = found
                if found is None and await uncounted(self._ctx.session, self._connection.id, code):
                    self.uncounted.add(code)

    def unresolved(self, held: Mapping[str, Decimal]) -> str | None:
        """The first code held (not zero) that has no instrument and is neither an `LD` mirror
        nor a currency Lararium does not have (the codes `asset` gives none); None when every
        quantity has its instrument."""
        for code, quantity in sorted(held.items()):
            if quantity == 0 or self._found.get(code) is not None or code in self.uncounted:
                continue
            if not code.startswith(MIRROR):
                return code
        return None

    def holds_uncounted(self, held: Mapping[str, Decimal]) -> bool:
        """Whether a quantity (not zero) is of a currency Lararium does not have."""
        return any(quantity != 0 and code in self.uncounted for code, quantity in held.items())

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
            # A currency that is not counted is still held: the wallet list is not contradicted.
            derived[wallet.key] = bool(quantities) or assets.holds_uncounted(wallet.held)
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
        rows = parse(listed.label, as_api(listed.body)).rows
        for name in wallet_flags(rows, derived, unread_wallets(reads)):
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
    (`backfill_daily_snapshots`), which reports the days it writes on each wallet's account
    that exists now. Run, in the transaction that applies the records, after they made the accounts
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
    """The daily snapshots the connection fetched (on its first sync) of the types written as
    reports (`DAILY_WALLETS`)."""
    contents = await session.scalars(
        text(
            "select content from core.raw_records where connection_id = :c"
            " and record_type = :t and upper(content ->> 'type') = any(:k)"
        ),
        {"c": connection_id, "t": record_type(_DAILY), "k": list(DAILY_WALLETS)},
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


def _daily_kind(row: Row) -> str | None:
    kind = row.get("type")
    kind = kind.upper() if isinstance(kind, str) else None
    return kind if kind in DAILY_WALLETS else None


def _daily_day(row: Row, daily: _DailyWallet) -> tuple[datetime, dict[str, Decimal]]:
    """A daily snapshot's time and quantity per asset; a `BinanceDataError` naming the field
    when it cannot be read in full (its list not stated, an entry, an amount, its time)."""
    data = row.get("data")
    listed = data.get(daily.entries) if isinstance(data, dict) else None
    where = f"snapshotVos[].data.{daily.entries}"
    entries = _stated(_DAILY, where, listed)
    at = tables.time_value(_DAILY, "snapshotVos[].updateTime", row.get("updateTime"))
    return at, _per_asset(_DAILY, f"{where}[].", entries, daily.fields)


async def backfill_daily_snapshots(
    ctx: CommandContext,
    connection: ConnectionRecord,
    rows: Sequence[Row],
    *,
    flag: Flag | None = None,
) -> int:
    """Each daily snapshot row of a type in `DAILY_WALLETS` (Spot: `free + locked`, `LD` mirrors
    dropped; Cross Margin: `netAsset`; USDⓈ-M Futures: `walletBalance`) as `reported` snapshots
    of its wallet at its own `updateTime`, an asset held then that the day does not list at 0
    (`record_report`); how many were written. A snapshot already written is not written again.

    A day's report time (`record_report`) is written with its snapshots, by this pass alone: a
    report says every instrument it does not list was 0, so a day is reported only when the
    wallet's account exists and every quantity of the day has its instrument. A day with a code
    that has none (other than an `LD` mirror), or that cannot be read in full (any
    `BinanceDataError`: its list, an entry, an amount, its `updateTime`), is not written at all
    and is flagged: the rows are fetched on the first sync only, so a failure raised from one
    could never be healed by a later sync."""
    flag = flag or (lambda _: None)
    written = 0
    for kind, daily in DAILY_WALLETS.items():
        shown = wallet_name(daily.wallet)
        days: list[tuple[datetime, dict[str, Decimal]]] = []
        for row in rows:
            if _daily_kind(row) != kind:
                continue
            try:
                days.append(_daily_day(row, daily))
            except BinanceDataError as error:
                flag(
                    f"a daily {shown} snapshot was not written:"
                    f" {error.label}.{error.field} is unreadable"
                )
        days.sort(key=lambda day: day[0])
        assets = _Assets(ctx, connection, flag)
        await assets.resolve({code for _, held in days for code, q in held.items() if q != 0})
        account = await _mapped_account(ctx, connection, daily.wallet)
        for at, held in days:
            missing = assets.unresolved(held)
            if missing is not None:
                flag(f"a daily {shown} snapshot was not written: {missing} has no instrument")
                continue
            quantities = assets.per_instrument(held)
            if quantities and account is None:
                account = await wallet_account(ctx, connection, daily.wallet)
            if account is None:
                continue
            for instrument, quantity in quantities.items():
                if not await _reported_at(ctx.session, account, instrument, at, quantity):
                    await _snapshot(ctx, account, instrument, quantity, at)
                    written += 1
            written += len(await record_report(ctx, connection, account, at, at, listed=quantities))
    return written
