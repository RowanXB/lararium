"""Fetching Binance history into raw records: windows, cursors, paging and the trade symbol
sets (plan Decision 7).

A label's first walk goes backwards, newest window first, so that a refused older window can
end it safely: everything newer is stored by then. Later walks go forwards from the last
fetch minus 7 days. Each window is stored in a short transaction of its own, with its raw
records and its cursor, so an interrupted walk continues where it stopped.

`config["cursors"]` maps a cursor key to `{"fetched_to": ms, "backfilled_to": ms,
"complete": bool}` (a label, or `<label>:<value>` per variant, `:<pair>` per isolated margin
pair) or, for trades, `<label>:<symbol>` (`:isolated` for an isolated pair's margin trades) to
the last trade id. `config["not_present"]` maps a key Binance said it does not serve to
when it said so (epoch ms); such a key is asked again after 7 days.
"""

import itertools
import json
from collections import Counter
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import cast
from uuid import UUID

from sqlalchemy import text

from lararium.core.commands import command_context
from lararium.core.connections import get_connection, lock_connection, update_config
from lararium.core.connectors import SyncContext
from lararium.core.errors import DisplayableError
from lararium.core.json_types import JSONValue
from lararium.core.raw_records import store_raw_record
from lararium.modules.finance.binance import layout, notices, tables
from lararium.modules.finance.binance.catalogue import (
    ENDPOINTS,
    FIELDS,
    ISOLATED_PARAMS,
    ISOLATED_TRANSFER_TYPES,
    Endpoint,
    paging_params,
    time_range,
)
from lararium.modules.finance.binance.client import BinanceClient, BinanceError, BinanceNotPresent
from lararium.modules.finance.binance.parse import (
    ApiValue,
    BinanceDataError,
    Parsed,
    Row,
    as_api,
    parse,
    unknown_flags,
)
from lararium.modules.finance.binance.tables import moment, store_response

VERSION = "binance-sync-1"
CURSORS_KEY = "cursors"
NOT_PRESENT_KEY = "not_present"

DAY_MS = 86_400_000
# 2017-07-01 00:00 UTC: Binance opened in July 2017; no history is older.
LAUNCH_MS = 1_498_867_200_000
# A later walk repeats the last 7 days: a deposit confirms, a withdrawal fails, later.
OVERLAP_MS = 7 * DAY_MS
NOT_PRESENT_RETRY_MS = 7 * DAY_MS
# The oldest window starts this much inside the lookback: Binance's clock moves on while the
# request travels, and a start just past the lookback would be refused.
LOOKBACK_MARGIN_MS = 60_000
# Fetched on the first sync only: Binance serves the last 30 days of them.
FIRST_SYNC_ONLY = frozenset({"snapshot"})
RECORD_NAMES = {"my-trades": "spot_trade"}
TRADES_LABEL = "my-trades"
MARGIN_TRADES = "margin-trades"
# The trade histories: walked by symbol and trade id (`walk_trades`), not by window.
TRADE_LABELS = (TRADES_LABEL, MARGIN_TRADES)
# The longest time range a trade request may ask (end minus start, in ms): the catalogue's
# my-trades says it "can't be longer than 24 hours", margin-trades "less than 24 hours".
TRADE_SPAN_MS = DAY_MS - 1
# The margin trade request's flag for an isolated pair ("TRUE"/"FALSE" text, default FALSE).
ISOLATED_FLAG = "isIsolated"
# What Binance answers a margin trade request for a symbol it does not margin trade (its margin
# error-code page): -3021 "Margin account are not allowed to trade this trading pair", -3028
# "Not a valid margin pair". For margin trades only, such a refusal means the symbol has none.
MARGIN_PAIR_REFUSALS = frozenset({-3021, -3028})
ISOLATED_MARGIN_LABEL = "margin-isolated-account"

# Locked Simple Earn, the flexible collateral record, BFUSD and RWUSD, ETH and SOL staking,
# on-chain yields, soft staking, dual investment and discount buy.
EARN_PRODUCTS = (
    "earn-locked-subscriptions",
    "earn-locked-redemptions",
    "earn-locked-rewards",
    "earn-flexible-collateral",
    "bfusd-subscriptions",
    "bfusd-redemptions",
    "bfusd-rewards",
    "rwusd-subscriptions",
    "rwusd-redemptions",
    "rwusd-rewards",
    "eth-stakes",
    "eth-redemptions",
    "wbeth-rewards",
    "wbeth-wraps",
    "wbeth-unwraps",
    "sol-stakes",
    "sol-redemptions",
    "bnsol-rewards",
    "sol-boost-rewards",
    "onchain-subscriptions",
    "onchain-redemptions",
    "onchain-rewards",
    "soft-staking-rewards",
    "earn-locked-position",
    "bfusd-account",
    "rwusd-account",
    "eth-staking-account",
    "sol-staking-account",
    "sol-unclaimed-rewards",
    "onchain-account",
    "onchain-locked-position",
    "soft-staking-list",
    "dci-accounts",
    "dci-positions",
    "discount-accounts",
    "discount-positions",
)
# Flexible and VIP crypto loans: the histories, then the ongoing orders (read one after the
# other: together they are the Crypto Loans wallet's report, `balances.LOAN_READS`) and the VIP
# collateral accounts.
LOAN_PRODUCTS = (
    "loan-flexible-borrows",
    "loan-flexible-repays",
    "loan-flexible-ltv-adjustments",
    "loan-flexible-liquidations",
    "vip-loan-repays",
    "vip-loan-interest",
    "vip-loan-requests",
    "loan-flexible-ongoing",
    "vip-loan-ongoing",
    "vip-loan-collateral-account",
)
# Cross and isolated margin: the histories, then the two accounts (each a wallet's report) and
# the liquidation loan. Margin trades are walked by symbol (`MARGIN_TRADES`), not here.
MARGIN_PRODUCTS = (
    "margin-transfers",
    "margin-borrow-repay",
    "margin-interest",
    "margin-capital-flow",
    "margin-liquidations",
    "margin-small-liability",
    "margin-liquidation-loan-repays",
    "margin-account",
    ISOLATED_MARGIN_LABEL,
    "margin-liquidation-loan",
)
# USDⓈ-M and COIN-M futures: the income histories, then each wallet's balance, account (its
# report, `balances.FUTURES_SOURCES`) and positions. The trades are walked per symbol the income
# rows name (`FUTURES_TRADES`), each host's exchange information read first (`FUTURES_INFO`).
FUTURES_PRODUCTS = (
    "um-income",
    "cm-income",
    "um-balance",
    "um-account",
    "um-positions",
    "cm-balance",
    "cm-account",
    "cm-positions",
)
# Each futures trade label and the income history whose rows name its symbols (design 5.2): a
# trade's commission is an income row of its symbol, so every symbol traded is named there.
FUTURES_TRADES = {"um-trades": "um-income", "cm-trades": "cm-income"}
# Each futures host's exchange information: its `rateLimits` give the host's request weight
# per minute (plan Decision 4), read at the start of a sync before any other request to it.
FUTURES_INFO = ("um-exchange-info", "cm-exchange-info")
# The Phase B labels built so far; each Phase B task adds its own here, and a sync then reads
# or walks them as it does Phase A's.
PHASE_B_BUILT = (
    "universal-transfer",
    "dust",
    "cloud-mining",
    "travel-deposits",
    "travel-withdrawals",
    # The other Earn products (Task 16): histories, then positions and accounts.
    *EARN_PRODUCTS,
    # Crypto loans (Task 17).
    *LOAN_PRODUCTS,
    # The margin wallets (Task 18).
    *MARGIN_PRODUCTS,
    # The futures wallets (Task 19).
    *FUTURES_PRODUCTS,
)
UNIVERSAL_TRANSFER = "universal-transfer"


def _built() -> list[Endpoint]:
    """The endpoints built so far: Phase A's in the catalogue's order, then Phase B's in the
    order they were built."""
    return [
        *(e for e in ENDPOINTS.values() if e.phase == "A"),
        *(ENDPOINTS[label] for label in PHASE_B_BUILT),
    ]


# What a sync reads (kind account, balance or position) and walks.
BALANCE_LABELS = [e.label for e in _built() if e.kind in ("account", "balance", "position")]
# Trades are walked by symbol and trade id (walk_trades), not by window.
HISTORY_LABELS = [e.label for e in _built() if e.kind == "history" and e.label not in TRADE_LABELS]


def record_type(label: str) -> str:
    """The raw record type of a label's rows: `binance.<record>`, a history label's record in
    the singular (`deposits` → `binance.deposit`)."""
    name = RECORD_NAMES.get(label)
    if name is None:
        name = label.replace("-", "_")
        if ENDPOINTS[label].kind == "history":
            name = name.removesuffix("s")
    return f"binance.{name}"


def to_json(value: ApiValue) -> JSONValue:
    """An answer as stored: a `Decimal` becomes its string."""
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, list):
        return [to_json(item) for item in value]
    if isinstance(value, dict):
        return {key: to_json(item) for key, item in value.items()}
    return value


def _int(value: JSONValue) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _floor(endpoint: Endpoint, now_ms: int) -> int:
    floor = LAUNCH_MS
    if endpoint.lookback_days is not None:
        floor = max(floor, now_ms - endpoint.lookback_days * DAY_MS + LOOKBACK_MARGIN_MS)
    return floor


def windows(
    endpoint: Endpoint, cursor: Mapping[str, JSONValue] | None, now_ms: int
) -> list[tuple[int, int]]:
    """The (start, end) windows to ask, in epoch ms, both inclusive, in the order they are asked.

    While the backfill is not complete: newest first, from now (or from below `backfilled_to`)
    back to July 2017 or the endpoint's lookback. Afterwards: oldest first, from `fetched_to`
    minus 7 days to now. No window spans more than `window_days`.
    """
    floor = _floor(endpoint, now_ms)
    span = endpoint.window_days * DAY_MS if endpoint.window_days is not None else None
    cursor = cursor or {}
    pieces: list[tuple[int, int]] = []
    fetched_to = _int(cursor.get("fetched_to"))
    if cursor.get("complete") is True and fetched_to is not None:
        start = max(fetched_to - OVERLAP_MS, floor)
        while start <= now_ms:
            end = now_ms if span is None else min(start + span, now_ms)
            pieces.append((start, end))
            start = end + 1
        return pieces
    backfilled_to = _int(cursor.get("backfilled_to"))
    end = backfilled_to - 1 if backfilled_to is not None else now_ms
    while end >= floor:
        start = floor if span is None else max(end - span, floor)
        pieces.append((start, end))
        end = start - 1
    return pieces


async def fresh_config(ctx: SyncContext) -> dict[str, JSONValue]:
    """The connection's config as committed now: walks change it as the sync runs."""
    async with ctx.session_factory() as session:
        return (await get_connection(session, ctx.connection.id)).config


def _mapping(value: JSONValue) -> dict[str, JSONValue]:
    return dict(value) if isinstance(value, dict) else {}


def absent(config: Mapping[str, JSONValue], key: str, now_ms: int) -> bool:
    """Whether Binance said, less than 7 days ago, that it does not serve this key."""
    since = _int(_mapping(config.get(NOT_PRESENT_KEY)).get(key))
    return since is not None and now_ms - since < NOT_PRESENT_RETRY_MS


@dataclass(frozen=True)
class Page:
    """One answer of a window: the window it asked (epoch ms, both inclusive), its 0-based
    index within it, the body and its rows."""

    label: str
    start: int
    end: int
    index: int
    body: ApiValue
    rows: list[Row]
    # The flags for what the parser found unknown in it (`unknown_flags`).
    unknown: tuple[str, ...] = ()


async def commit(
    ctx: SyncContext,
    key: str,
    *,
    record: str | None = None,
    request: dict[str, JSONValue] | None = None,
    contents: Sequence[JSONValue] = (),
    cursor: JSONValue = None,
    present: bool = False,
    absent_since: int | None = None,
    responses: Sequence[Page] = (),
) -> list[UUID]:
    """Store raw records and the key's cursor in one short transaction of its own.

    `present` takes the key off `not_present`; `absent_since` puts it there from that time.
    `responses` are the answers the records came in: what each says beside its records goes
    to the label's response table, which raw records (rows only) do not keep.
    """
    connection_id = ctx.connection.id
    args: dict[str, JSONValue] = {"connection_id": str(connection_id), "key": key}
    async with (
        ctx.session_factory() as session,
        command_context(session, ctx.command.actor, "binance.fetch", args) as cmd,
    ):
        # Under the row lock: what is merged below is the config as it is now.
        config = (await lock_connection(cmd, connection_id)).config
        ids = [
            await store_raw_record(
                cmd,
                connection_id=connection_id,
                record_type=record,
                request=request or {},
                content=content,
                connector_version=VERSION,
                sync_id=ctx.sync_id,
            )
            for content in contents
            if record is not None
        ]
        for page in responses:
            if page.label in layout.TABLE_LABELS:
                window = (moment(page.start), moment(page.end))
                await store_response(
                    cmd, connection_id, page.label, page.body, window=window, page=page.index
                )
        unknown = [flag for page in responses for flag in page.unknown]
        if unknown:
            await notices.publish(cmd, connection_id, unknown)
        values: dict[str, JSONValue] = {}
        if cursor is not None:
            cursors = _mapping(config.get(CURSORS_KEY))
            cursors[key] = cursor
            values[CURSORS_KEY] = cursors
        missing = _mapping(config.get(NOT_PRESENT_KEY))
        if present and key in missing:
            del missing[key]
            values[NOT_PRESENT_KEY] = missing
        if absent_since is not None:
            missing[key] = absent_since
            values[NOT_PRESENT_KEY] = missing
        if values:
            await update_config(cmd, connection_id, values)
    return ids


def _contents(endpoint: Endpoint, rows: Sequence[Row]) -> list[JSONValue]:
    """The raw record contents of one response set: a row with no id carries its occurrence
    among the rows identical to it (plan Decision 5), so identical rows stay two records."""
    stored = [to_json(dict(row)) for row in rows]
    if endpoint.id_fields:
        return stored
    seen: Counter[str] = Counter()
    contents: list[JSONValue] = []
    for row in stored:
        canonical = json.dumps(row, sort_keys=True, separators=(",", ":"))
        seen[canonical] += 1
        contents.append({"row": row, "occurrence": seen[canonical]})
    return contents


def typed(label: str, body: ApiValue) -> Parsed:
    """An answer parsed, and each of its rows and what it says beside them converted as the
    typed tables will convert them, and a history record's ledger key checked: a row that
    cannot be typed or keyed is never stored raw, so every stored record can be normalised.
    The `BinanceDataError` names the field.

    The answer is checked twice: as Binance sent it (a text field sent as the number 1.5 is
    wrong there, though stored it would read as the text "1.5"), then as it is stored and read
    back (`to_json`, then `as_api`): a JSON `1.0` is a whole Decimal now but the text "1.0"
    once stored. The parsed answer returned is the stored form, the one its callers store."""
    parse(label, body)
    body = as_api(to_json(body))
    parsed = parse(label, body)
    endpoint = ENDPOINTS[label]
    if label in layout.TABLE_LABELS:
        for row in tables.records(label, parsed):
            tables.check(label, row)
            if endpoint.kind == "history":
                # The record's ledger key, as the normaliser makes it.
                for field in endpoint.id_fields:
                    tables.key_part(label, row, field)
        tables.check_response(label, body)
    return parsed


async def _ask(
    client: BinanceClient, endpoint: Endpoint, params: Mapping[str, str | int]
) -> list[Row]:
    return typed(endpoint.label, await client.call(endpoint, params)).rows


def _beside(endpoint: Endpoint, body: ApiValue, name: str) -> ApiValue:
    """The field `name` beside an answer's record list, when the catalogue lists it there for
    the label (`moreData`, `total`); otherwise None."""
    *parents, _ = endpoint.rows.split(".")
    if ".".join([*parents, name]) not in {spec.path for spec in FIELDS[endpoint.label]}:
        return None
    for step in parents:
        body = body.get(step) if isinstance(body, dict) else None
    return body.get(name) if isinstance(body, dict) else None


def _counts_records_it_leaves_out(endpoint: Endpoint, body: ApiValue) -> bool:
    """Whether an answer leaves its record list out while its `total` says it has records.
    A window's first answer that does has no reading but a missing list. (A later page can:
    the page asked past a full one has no records, while `total` counts every page.)"""
    total = _beside(endpoint, body, "total")
    if isinstance(total, str) and total.isdecimal():
        total = int(total)
    if not isinstance(total, int) or isinstance(total, bool) or total <= 0:
        return False
    *parents, last = endpoint.rows.split(".")
    for step in parents:
        body = body.get(step) if isinstance(body, dict) else None
    return isinstance(body, dict) and last not in body


class RefusedInside(DisplayableError):
    """Binance refused a request inside a window that had answered: not where its history
    ends."""


def _refused_inside(endpoint: Endpoint) -> RefusedInside:
    return RefusedInside(
        f"Binance refused part of a {endpoint.label} window it had answered:"
        " the walk stops there and asks again next time"
    )


async def window_pages(
    client: BinanceClient,
    endpoint: Endpoint,
    fixed: Mapping[str, str],
    start: int,
    end: int,
) -> list[Page]:
    """Every answer of one window, through every page; an endpoint that cannot page and answers
    a full page, or says there is more (`moreData`), has its window split in two until each
    half fits (the full answer is kept as an answer, its rows are not). A window of one
    millisecond that still does not fit fails: what it holds cannot all be asked. An endpoint
    paged by id inside its window (the margin capital flow) is split the same way: the order of
    an answer asked without an id is not documented, a time range and a `limit` are.

    Only the window's first request being refused can mean its history ends there. A later
    one (a half of a split window, a later page) is refused inside a window that answered,
    so it fails: the walk keeps its cursor and asks again. A refusal seen on a later sync is
    not known to follow an answer (design §6)."""
    params: dict[str, str | int] = {**fixed, **time_range(endpoint, start, end)}
    cursor_name, size_name, first = paging_params(endpoint)
    size = endpoint.page_size
    if size_name and size:
        params[size_name] = size

    async def ask(index: int, asked: Mapping[str, str | int]) -> Page:
        body = await client.call(endpoint, asked)
        parsed = typed(endpoint.label, body)
        if index == 0 and _counts_records_it_leaves_out(endpoint, parsed.body):
            raise BinanceDataError(endpoint.label, endpoint.rows)
        unknown = tuple(unknown_flags(endpoint.label, parsed))
        return Page(endpoint.label, start, end, index, parsed.body, parsed.rows, unknown)

    if endpoint.paging == "from_id" and endpoint.label in TRADE_LABELS:
        raise ValueError(f"{endpoint.label} is walked by trade id, not by window")
    if endpoint.paging in ("none", "from_id"):
        answer = await ask(0, params)
        more = _beside(endpoint, answer.body, "moreData") is True
        if (size and len(answer.rows) >= size) or more:
            if end == start:
                raise DisplayableError(
                    f"Binance has more {endpoint.label} records in one millisecond"
                    " than one answer holds"
                )
            middle = start + (end - start) // 2
            full = Page(answer.label, start, end, 0, answer.body, [], answer.unknown)
            try:
                return [
                    full,
                    *await window_pages(client, endpoint, fixed, middle + 1, end),
                    *await window_pages(client, endpoint, fixed, start, middle),
                ]
            except BinanceNotPresent:
                raise _refused_inside(endpoint) from None
        return [answer]
    pages: list[Page] = []
    position = first
    while True:
        try:
            page = await ask(len(pages), {**params, cursor_name: position})
        except BinanceNotPresent:
            if not pages:
                raise
            raise _refused_inside(endpoint) from None
        pages.append(page)
        if len(page.rows) < size or not page.rows:
            return pages
        position += len(page.rows) if endpoint.paging == "offset" else 1


async def window_rows(
    client: BinanceClient,
    endpoint: Endpoint,
    fixed: Mapping[str, str],
    start: int,
    end: int,
) -> list[Row]:
    """Every row of one window, through every page (`window_pages`' rows)."""
    pages = await window_pages(client, endpoint, fixed, start, end)
    return [row for page in pages for row in page.rows]


def cursor_keys(endpoint: Endpoint) -> list[tuple[str, dict[str, str]]]:
    """One cursor key and its fixed parameters per variant (`snapshot:SPOT`, ...). A parameter
    with one value only is always sent and splits nothing, so it is not in the key."""
    names = [name for name, _ in endpoint.variants]
    split = [len(values) > 1 for _, values in endpoint.variants]
    return [
        (
            ":".join(
                [endpoint.label, *(v for v, named in zip(values, split, strict=True) if named)]
            ),
            dict(zip(names, values, strict=True)),
        )
        for values in itertools.product(*(values for _, values in endpoint.variants))
    ]


def walk_keys(label: str, isolated: Collection[str] = ()) -> list[tuple[str, dict[str, str]]]:
    """Every cursor key of a label and its fixed parameters: one per variant (`cursor_keys`)
    and, for `universal-transfer`, each isolated-margin type once per isolated pair in
    `isolated` (`universal-transfer:ISOLATEDMARGIN_MARGIN:<pair>`), once per ordered pair of
    two pairs for a transfer from one isolated pair to another; for a margin history that
    takes a pair (`ISOLATED_PARAMS`), each variant again per pair (`<key>:<pair>`)."""
    keys = cursor_keys(ENDPOINTS[label])
    symbols = sorted(isolated)
    if label in ISOLATED_PARAMS:
        name = ISOLATED_PARAMS[label]
        return [
            *keys,
            *(
                (f"{key}:{symbol}", {**fixed, name: symbol})
                for key, fixed in cursor_keys(ENDPOINTS[label])
                for symbol in symbols
            ),
        ]
    if label != UNIVERSAL_TRANSFER:
        return keys
    for symbol in symbols:
        for kind, names in ISOLATED_TRANSFER_TYPES.items():
            if len(names) == 1:
                keys.append((f"{label}:{kind}:{symbol}", {"type": kind, names[0]: symbol}))
    for kind, names in ISOLATED_TRANSFER_TYPES.items():
        if len(names) == 2:
            for pair in itertools.permutations(symbols, 2):
                fixed = {"type": kind, **dict(zip(names, pair, strict=True))}
                keys.append((":".join([label, kind, *pair]), fixed))
    return keys


def created_pairs(body: ApiValue) -> frozenset[str]:
    """The pairs an isolated margin account answer lists as created: each `assets[].symbol`
    whose `isolatedCreated` is not false (a pair not created holds nothing and has no
    records)."""
    listed = body.get("assets") if isinstance(body, dict) else None
    found: set[str] = set()
    for entry in listed if isinstance(listed, list) else []:
        if not isinstance(entry, dict):
            continue
        symbol = entry.get("symbol")
        if isinstance(symbol, str) and entry.get("isolatedCreated") is not False:
            found.add(symbol)
    return frozenset(found)


async def isolated_symbols(ctx: SyncContext) -> frozenset[str]:
    """The isolated margin pairs the account has (ruling R1): those the latest stored read of
    the isolated margin account lists as created. A sync stores its reads before it walks, so
    its walks ask for the pairs it has just read."""
    async with ctx.session_factory() as session:
        content = await session.scalar(
            text(
                "select content from core.raw_records where connection_id = :c"
                " and record_type = :t order by id desc limit 1"
            ),
            {"c": ctx.connection.id, "t": record_type(ISOLATED_MARGIN_LABEL)},
        )
    stored = cast(JSONValue, content)
    body = stored.get("body") if isinstance(stored, dict) else None
    return created_pairs(as_api(body))


def backfill_complete(config: Mapping[str, JSONValue], isolated: Collection[str] = ()) -> bool:
    """Whether every history label's first walk has ended: each of its keys complete, or not
    served by Binance (`not_present`). The keys of the isolated pairs in `isolated` (those
    known when it is asked, ruling R17) count; a pair that appears after the first sync walks
    its own history from then on."""
    cursors = _mapping(config.get(CURSORS_KEY))
    missing = _mapping(config.get(NOT_PRESENT_KEY))
    return all(
        key in missing or _mapping(cursors.get(key)).get("complete") is True
        for label in HISTORY_LABELS
        for key, _ in walk_keys(label, isolated)
    )


async def walk(ctx: SyncContext, client: BinanceClient, label: str) -> list[UUID]:
    """Fetch one history label (every key `walk_keys` gives it) from its cursor; the raw record
    ids stored."""
    endpoint = ENDPOINTS[label]
    paired = label == UNIVERSAL_TRANSFER or label in ISOLATED_PARAMS
    isolated = await isolated_symbols(ctx) if paired else frozenset[str]()
    ids: list[UUID] = []
    for key, fixed in walk_keys(label, isolated):
        ids += await _walk_key(ctx, client, endpoint, key, fixed)
    return ids


async def _walk_key(
    ctx: SyncContext,
    client: BinanceClient,
    endpoint: Endpoint,
    key: str,
    fixed: Mapping[str, str],
) -> list[UUID]:
    config = await fresh_config(ctx)
    now_ms = client.now_ms()
    if absent(config, key, now_ms):
        return []
    retrying = key in _mapping(config.get(NOT_PRESENT_KEY))
    cursor = _mapping(_mapping(config.get(CURSORS_KEY)).get(key))
    complete = cursor.get("complete") is True
    if complete and endpoint.label in FIRST_SYNC_ONLY:
        return []
    pieces = windows(endpoint, cursor, now_ms)
    backfilling = not complete
    # The first walk's fetched_to is when it started, kept across interruptions: the walks
    # after the backfill start from there.
    fetched_to = _int(cursor.get("fetched_to")) if backfilling else None
    fetched_to = fetched_to if fetched_to is not None else now_ms
    newest = backfilling and _int(cursor.get("backfilled_to")) is None
    ids: list[UUID] = []
    record = record_type(endpoint.label)
    if not pieces:
        # Already at the floor: only the end of the backfill is left to record.
        await commit(ctx, key, cursor={**cursor, "complete": True}, present=True)
        return ids
    for index, (start, end) in enumerate(pieces):
        try:
            pages = await window_pages(client, endpoint, fixed, start, end)
        except RefusedInside:
            if backfilling:
                raise
            # As a refused window after the backfill: the cursor stays, and the next walk
            # asks again.
            return ids
        except BinanceNotPresent:
            if not backfilling:
                # A label whose history was walked in full is served: a refusal now (a
                # passing 404) is nothing this time, and the next walk asks from its cursor.
                return ids
            if not (newest and index == 0):
                # History ends here; every newer window is stored (Decision 7).
                state = {**cursor, "fetched_to": fetched_to, "complete": True}
                await commit(ctx, key, cursor=state, present=True)
            else:
                # Refused before it ever answered: the product is not present.
                await commit(ctx, key, absent_since=now_ms)
            return ids
        request: dict[str, JSONValue] = {**fixed, **time_range(endpoint, start, end)}
        if backfilling:
            state: dict[str, JSONValue] = {
                "fetched_to": fetched_to,
                "backfilled_to": start,
                "complete": index == len(pieces) - 1,
            }
        else:
            state = {**cursor, "fetched_to": end}
        cursor = state
        ids += await commit(
            ctx,
            key,
            record=record,
            request=request,
            contents=_contents(endpoint, [row for page in pages for row in page.rows]),
            cursor=state,
            present=retrying,
            responses=pages,
        )
        retrying = False
    return ids


def trade_symbols(
    exchange_info: ApiValue,
    seen: Collection[str],
    *,
    everything: bool = False,
    either_side_of: str | None = None,
    margin: bool = False,
) -> list[str]:
    """The symbols to ask for trades, in `exchangeInfo`'s order.

    `either_side_of`: every symbol with that asset as base or quote. `everything`: every
    symbol (the first sync). Otherwise: the symbols both of whose assets are in `seen`.
    `margin`: only those `exchangeInfo` says are margin traded (`isMarginTradingAllowed`), the
    symbols cross margin trades can be in (ruling R18).
    """
    listed = exchange_info.get("symbols") if isinstance(exchange_info, dict) else None
    chosen: list[str] = []
    for entry in listed if isinstance(listed, list) else []:
        if not isinstance(entry, dict):
            continue
        symbol, base, quote = entry.get("symbol"), entry.get("baseAsset"), entry.get("quoteAsset")
        if not isinstance(symbol, str):
            continue
        if margin and entry.get("isMarginTradingAllowed") is not True:
            continue
        if either_side_of is not None:
            wanted = either_side_of in (base, quote)
        else:
            wanted = everything or (base in seen and quote in seen)
        if wanted:
            chosen.append(symbol)
    return chosen


@dataclass(frozen=True)
class TradeWalk:
    """The trades a sync walks of one trade label: its symbols, and whether they are isolated
    margin pairs (asked with `isIsolated=TRUE`)."""

    label: str
    symbols: tuple[str, ...]
    isolated: bool = False


def trade_cursor_key(label: str, symbol: str, *, isolated: bool = False) -> str:
    """The cursor of a symbol's trades: `<label>:<symbol>`, an isolated pair's margin trades
    `<label>:<symbol>:isolated` (their ids are counted apart from the cross ones')."""
    return f"{label}:{symbol}:isolated" if isolated else f"{label}:{symbol}"


def _trade_params(symbol: str, isolated: bool) -> dict[str, str | int]:
    return {"symbol": symbol, ISOLATED_FLAG: "TRUE"} if isolated else {"symbol": symbol}


async def walk_trades(
    ctx: SyncContext,
    client: BinanceClient,
    label: str,
    symbols: Sequence[str],
    since_ms: int | None = None,
    until_ms: int | None = None,
    *,
    isolated: bool = False,
) -> list[UUID]:
    """Fetch each symbol's trades after its last trade id; the raw record ids stored. Each
    page is stored with its symbol's cursor. `isolated`: the symbols are isolated margin pairs,
    whose margin trades are asked apart (`isIsolated=TRUE`).

    With `since_ms`, the sweep instead: every trade of each symbol from `since_ms` to
    `until_ms` (now when None), whatever the interval's length, by time windows Binance
    accepts (`TRADE_SPAN_MS`). The sweep leaves the cursors as they are: the walk by trade id
    still asks everything after a symbol's last id, so nothing before the interval is skipped.

    A margin trade request Binance refuses as not served (`BinanceNotPresent`) or as not a
    margin pair (`MARGIN_PAIR_REFUSALS`) leaves that symbol out and the walk goes on with the
    next: a symbol Binance does not margin trade has no margin trades (ruling R18).
    """
    if since_ms is not None:
        until = until_ms if until_ms is not None else client.now_ms()
        return await _sweep_trades(ctx, client, label, symbols, since_ms, until, isolated)
    endpoint = ENDPOINTS[label]
    cursor_name, size_name, first = paging_params(endpoint)
    id_field = endpoint.id_fields[-1]
    size = endpoint.page_size
    cursors = _mapping((await fresh_config(ctx)).get(CURSORS_KEY))
    record = record_type(label)
    ids: list[UUID] = []
    for symbol in symbols:
        key = trade_cursor_key(label, symbol, isolated=isolated)
        last = _int(cursors.get(key))
        fixed = _trade_params(symbol, isolated)
        params: dict[str, str | int] = {
            **fixed,
            size_name: size,
            cursor_name: last + 1 if last is not None else first,
        }
        while True:
            try:
                rows = await _ask(client, endpoint, params)
            except BinanceError as error:
                if not _no_margin_trades(label, error):
                    raise
                break
            if not rows:
                break
            top = max(int(str(row[id_field])) for row in rows)
            last = top if last is None else max(last, top)
            ids += await commit(
                ctx,
                key,
                record=record,
                request=cast(dict[str, JSONValue], dict(params)),
                contents=_contents(endpoint, rows),
                cursor=last,
            )
            if len(rows) < size:
                break
            params = {**fixed, size_name: size, cursor_name: top + 1}
    return ids


def _no_margin_trades(label: str, error: BinanceError) -> bool:
    """Whether a refused trade request says the symbol has no margin trades: only for margin
    trades, a "not present" code or a margin pair refusal."""
    if label != MARGIN_TRADES:
        return False
    return isinstance(error, BinanceNotPresent) or error.code in MARGIN_PAIR_REFUSALS


async def _sweep_trades(
    ctx: SyncContext,
    client: BinanceClient,
    label: str,
    symbols: Sequence[str],
    since_ms: int,
    until_ms: int,
    isolated: bool,
) -> list[UUID]:
    endpoint = ENDPOINTS[label]
    record = record_type(label)
    ids: list[UUID] = []
    for symbol in symbols:
        start = since_ms
        while start <= until_ms:
            end = min(start + TRADE_SPAN_MS, until_ms)
            try:
                rows = await _trades_between(client, endpoint, symbol, start, end, isolated)
            except BinanceError as error:
                if not _no_margin_trades(label, error):
                    raise
                break
            if rows:
                _, size_name, _ = paging_params(endpoint)
                request: dict[str, JSONValue] = {
                    **_trade_params(symbol, isolated),
                    **time_range(endpoint, start, end),
                    size_name: endpoint.page_size,
                }
                ids += await commit(
                    ctx,
                    trade_cursor_key(label, symbol, isolated=isolated),
                    record=record,
                    request=request,
                    contents=_contents(endpoint, rows),
                )
            start = end + 1
    return ids


async def _trades_between(
    client: BinanceClient, endpoint: Endpoint, symbol: str, start: int, end: int, isolated: bool
) -> list[Row]:
    """Every trade of the symbol in [start, end]. A time range cannot be paged by id (the
    catalogue lists no such combination), so a full answer has its range split in two until
    each half fits, whichever trades of the range Binance gave first."""
    _, size_name, _ = paging_params(endpoint)
    size = endpoint.page_size
    params: dict[str, str | int] = {
        **_trade_params(symbol, isolated),
        **time_range(endpoint, start, end),
        size_name: size,
    }
    rows = await _ask(client, endpoint, params)
    if len(rows) < size:
        return rows
    if end == start:
        raise DisplayableError(
            f"Binance has more trades of {symbol} in one millisecond than one answer holds"
        )
    middle = start + (end - start) // 2
    return [
        *await _trades_between(client, endpoint, symbol, start, middle, isolated),
        *await _trades_between(client, endpoint, symbol, middle + 1, end, isolated),
    ]


async def income_symbols(ctx: SyncContext, income_label: str) -> list[str]:
    """The symbols the connection's stored rows of a futures income history name, sorted; a
    row with no symbol (a transfer) names none."""
    async with ctx.session_factory() as session:
        found = await session.scalars(
            text(
                "select distinct content ->> 'symbol' from core.raw_records"
                " where connection_id = :c and record_type = :t"
            ),
            {"c": ctx.connection.id, "t": record_type(income_label)},
        )
        return sorted({symbol.strip() for symbol in found if symbol and symbol.strip()})


def futures_trade_key(label: str, symbol: str) -> str:
    """The cursor of a symbol's futures trades: `<label>:<symbol>`, a window cursor."""
    return f"{label}:{symbol}"


async def walk_futures_trades(ctx: SyncContext, client: BinanceClient, label: str) -> list[UUID]:
    """Walk a futures trade label for every symbol its income history names (design 5.2); the
    raw record ids stored. Binance takes a time range of at most 7 days and keeps 3 months
    (the catalogue), so each symbol is walked by windows as a history label is, from its own
    cursor (`futures_trade_key`): its first walk back 3 months, later ones from the last minus 7
    days."""
    endpoint = ENDPOINTS[label]
    ids: list[UUID] = []
    for symbol in await income_symbols(ctx, FUTURES_TRADES[label]):
        key = futures_trade_key(label, symbol)
        ids += await _walk_key(ctx, client, endpoint, key, {"symbol": symbol})
    return ids
