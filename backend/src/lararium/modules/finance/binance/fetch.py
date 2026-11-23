"""Fetching Binance history into raw records: windows, cursors, paging and the trade symbol
sets (plan Decision 7).

A label's first walk goes backwards, newest window first, so that a refused older window can
end it safely: everything newer is stored by then. Later walks go forwards from the last
fetch minus 7 days. Each window is stored in a short transaction of its own, with its raw
records and its cursor, so an interrupted walk continues where it stopped.

`config["cursors"]` maps a cursor key to `{"fetched_to": ms, "backfilled_to": ms,
"complete": bool}` (a label, or `<label>:<value>` per variant) or, for trades, `<label>:<symbol>`
to the last trade id. `config["not_present"]` maps a key Binance said it does not serve to
when it said so (epoch ms); such a key is asked again after 7 days.
"""

import itertools
import json
from collections import Counter
from collections.abc import Collection, Mapping, Sequence
from decimal import Decimal
from typing import cast
from uuid import UUID

from lararium.core.commands import command_context
from lararium.core.connections import get_connection, lock_connection, update_config
from lararium.core.connectors import SyncContext
from lararium.core.json_types import JSONValue
from lararium.core.raw_records import store_raw_record
from lararium.modules.finance.binance.catalogue import ENDPOINTS, Endpoint, paging_params
from lararium.modules.finance.binance.client import BinanceClient, BinanceNotPresent
from lararium.modules.finance.binance.parse import ApiValue, Row, parse

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
) -> list[UUID]:
    """Store raw records and the key's cursor in one short transaction of its own.

    `present` takes the key off `not_present`; `absent_since` puts it there from that time.
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


async def _ask(
    client: BinanceClient, endpoint: Endpoint, params: Mapping[str, str | int]
) -> list[Row]:
    return parse(endpoint.label, await client.call(endpoint, params)).rows


async def window_rows(
    client: BinanceClient,
    endpoint: Endpoint,
    fixed: Mapping[str, str],
    start: int,
    end: int,
) -> list[Row]:
    """Every row of one window, through every page; an endpoint that cannot page and answers
    a full page has its window split in two until each half fits."""
    low, high = endpoint.time_params
    params: dict[str, str | int] = {**fixed, low: start, high: end}
    cursor_name, size_name, first = paging_params(endpoint)
    size = endpoint.page_size
    if size_name and size:
        params[size_name] = size
    if endpoint.paging == "none":
        rows = await _ask(client, endpoint, params)
        if size and len(rows) >= size and end > start:
            middle = start + (end - start) // 2
            return await window_rows(client, endpoint, fixed, middle + 1, end) + (
                await window_rows(client, endpoint, fixed, start, middle)
            )
        return rows
    if endpoint.paging == "from_id":
        raise ValueError(f"{endpoint.label} is walked by trade id, not by window")
    rows: list[Row] = []
    position = first
    while True:
        page = await _ask(client, endpoint, {**params, cursor_name: position})
        rows.extend(page)
        if len(page) < size or not page:
            return rows
        position += len(page) if endpoint.paging == "offset" else 1


def _keys(endpoint: Endpoint) -> list[tuple[str, dict[str, str]]]:
    """One cursor key and its fixed parameters per variant (`snapshot:SPOT`, ...)."""
    if not endpoint.variants:
        return [(endpoint.label, {})]
    names = [name for name, _ in endpoint.variants]
    return [
        (":".join([endpoint.label, *values]), dict(zip(names, values, strict=True)))
        for values in itertools.product(*(values for _, values in endpoint.variants))
    ]


async def walk(ctx: SyncContext, client: BinanceClient, label: str) -> list[UUID]:
    """Fetch one history label (every variant) from its cursor; the raw record ids stored."""
    endpoint = ENDPOINTS[label]
    ids: list[UUID] = []
    for key, fixed in _keys(endpoint):
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
            rows = await window_rows(client, endpoint, fixed, start, end)
        except BinanceNotPresent:
            if backfilling and not (newest and index == 0):
                # History ends here; every newer window is stored (Decision 7).
                state = {**cursor, "fetched_to": fetched_to, "complete": True}
                await commit(ctx, key, cursor=state, present=True)
            else:
                await commit(ctx, key, absent_since=now_ms)
            return ids
        low, high = endpoint.time_params
        request: dict[str, JSONValue] = {**fixed, low: start, high: end}
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
            contents=_contents(endpoint, rows),
            cursor=state,
            present=retrying,
        )
        retrying = False
    return ids


def trade_symbols(
    exchange_info: ApiValue,
    seen: Collection[str],
    *,
    everything: bool = False,
    either_side_of: str | None = None,
) -> list[str]:
    """The symbols to ask for trades, in `exchangeInfo`'s order.

    `either_side_of`: every symbol with that asset as base or quote. `everything`: every
    symbol (the first sync). Otherwise: the symbols both of whose assets are in `seen`.
    """
    listed = exchange_info.get("symbols") if isinstance(exchange_info, dict) else None
    chosen: list[str] = []
    for entry in listed if isinstance(listed, list) else []:
        if not isinstance(entry, dict):
            continue
        symbol, base, quote = entry.get("symbol"), entry.get("baseAsset"), entry.get("quoteAsset")
        if not isinstance(symbol, str):
            continue
        if either_side_of is not None:
            wanted = either_side_of in (base, quote)
        else:
            wanted = everything or (base in seen and quote in seen)
        if wanted:
            chosen.append(symbol)
    return chosen


async def walk_trades(
    ctx: SyncContext,
    client: BinanceClient,
    label: str,
    symbols: Sequence[str],
    since_ms: int | None = None,
) -> list[UUID]:
    """Fetch each symbol's trades after its last trade id (or, with `since_ms`, from that
    time); the raw record ids stored. Each page is stored with its symbol's cursor."""
    endpoint = ENDPOINTS[label]
    cursor_name, size_name, first = paging_params(endpoint)
    id_field = endpoint.id_fields[-1]
    size = endpoint.page_size
    low, _ = endpoint.time_params
    cursors = _mapping((await fresh_config(ctx)).get(CURSORS_KEY))
    record = record_type(label)
    ids: list[UUID] = []
    for symbol in symbols:
        key = f"{label}:{symbol}"
        last = _int(cursors.get(key))
        params: dict[str, str | int] = {"symbol": symbol, size_name: size}
        if since_ms is not None:
            params[low] = since_ms
        else:
            params[cursor_name] = last + 1 if last is not None else first
        while True:
            rows = await _ask(client, endpoint, params)
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
            params = {"symbol": symbol, size_name: size, cursor_name: top + 1}
    return ids
