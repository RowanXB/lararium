"""eToro sync and statement ingest: fetch the user's data, or read an uploaded account
statement, and store every item as a raw record; then normalise (normalize.py).

Per sync (design §5.2), in order:
1. `/me`, the read-only gate (design §5.1, Decision 13): a key holding any scope that is not
   read-only is refused before anything else is asked. Its scopes alone are kept (`/me`'s other
   fields may be another user's), committed on their own; nothing else is stored. A key whose
   user is not the connection's is refused too.
2. The reports: balances, PnL, aggregate portfolio, instrument breakdown.
3. eToro Money's transactions for each Cash account, newest first, until a page holds only what
   was seen (the newest id per account, `seen_cash`).
4. Trade history. The first sync walks back in windows whose `minDate` is 364 days apart (one
   window: every trade closed since `minDate`), until the window starts on or before the
   account's opening (the earliest stored statement's Date Created), or, with no statement
   uploaded, until a window brings no `positionId` the walk had not seen. Later syncs ask one
   window, from the latest stored close date (`trade_history_to`) less 7 days, however long
   ago that is.
5. `orders:lookup` once for each order of an open position or a closed trade (`looked_up`).
6. Balance history from the latest stored snapshot (`balance_history_to`) less 7 days; the
   first sync takes 365 days.
7. Once a day (`daily_at`): closed-events, stockback, the market data of the instruments held
   or traded, and daily candles of those held, from each one's first open (then from the last
   day fetched, `candles_to`, less 7 days).

Every item is stored as eToro sent it, its numbers as exact text; a report (`/me`, balances,
PnL, aggregate, breakdown, balance history) is also sighted (report_sightings), so it is dated
by the sync that saw it. Nothing here logs a value.

A statement upload (design §5.3) is checked before anything is stored: its Username must be
`/me`'s, its Currency the Trading account's, and the connection's first statement must start
on or before the account's Date Created. Each refusal is a `StatementRejected` naming no value
but that date (R27); one before the first sync has finished is a `StatementTooEarly`. Each row
is a raw record of its cells and key; the statement's period is the upload's request, so a row
two overlapping statements share is stored once.

Flags (Decision 4) from the normalizer and from reading a statement are connection warnings,
`eToro flagged: <reason>`, and the owner is told of each new one once (connector_flags, shared
with Trading 212). A sync (once it has fetched) and an upload each take the connection's lock
before they normalize, so one connection is normalized by one run at a time. The upload runs
outside a sync, so flags are read as they stand when they are published, and the upload
merges only the flag keys into the cursor.
"""

import asyncio
import logging
import threading
from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import cast
from uuid import UUID

from sqlalchemy import text

from lararium.core.commands import command_context
from lararium.core.connections import load_credential, lock_connection, merge_cursor
from lararium.core.connectors import Capability, SyncContext
from lararium.core.documents import record_document
from lararium.core.errors import CredentialRejectedError, DisplayableError
from lararium.core.json_types import JSONValue
from lararium.core.raw_records import store_raw_record
from lararium.modules.finance import connector_flags
from lararium.modules.finance.etoro.client import (
    BALANCE_HISTORY_DAYS,
    GATE_MESSAGE,
    TRADE_WINDOW_DAYS,
    EtoroClient,
    Item,
    ReadOnlyGateError,
    endpoint_path,
    write_scopes,
)
from lararium.modules.finance.etoro.fields import (
    ACCOUNT_SUMMARY,
    ENDPOINTS,
    LABEL,
    STATEMENT_SHEETS,
    VALUE,
)
from lararium.modules.finance.etoro.normalize import normalize
from lararium.modules.finance.etoro.statement import (
    STATEMENT_ZONE,
    EtoroStatement,
    StatementError,
    parse_statement,
    read_moment,
)
from lararium.modules.finance.readonly_http import JSONValue as ApiValue
from lararium.modules.finance.readonly_http import record_json
from lararium.modules.finance.report_sightings import record_sighting
from lararium.modules.finance.typed_tables import TypedValueError, parse_date

logger = logging.getLogger(__name__)

VERSION = "etoro-sync-1"
KEY_PURPOSE = "public_api_key"
USER_KEY_PURPOSE = "user_key"
DOCUMENT_SOURCE = "finance.etoro"
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
FLAGGED_PREFIX = "eToro flagged: "
# The parts flags are published under: the normalizer's, those it finds in full every run
# (instruments untyped, of a type the ledger does not hold or of no known currency; positions
# of no known settlement), and the latest statement's reading.
FIELDS = "fields"
IN_FULL = "in_full"
STATEMENT = "statement"
DAILY_EVERY = timedelta(hours=24)
# Trade history, balance history and candles are asked again from this far before the latest
# day stored, for rows eToro adds late.
OVERLAP = timedelta(days=7)
# A first walk back that has not ended after this many windows (40 years) is a fault.
MAX_WINDOWS = 40
# What eToro reports now, rather than records: each storing is sighted.
REPORTS = frozenset({"me", "balances", "pnl", "aggregate", "breakdown", "balance_history"})
# A refused key's scopes, without the rest of its `/me` (not an endpoint's record: the
# normalizer has nothing to map in it).
REFUSED_KEY = "etoro.refused_key"


class EtoroSyncError(DisplayableError):
    """The sync cannot go on; the message names an endpoint and a field, never a value."""


class AccountMismatchError(EtoroSyncError, CredentialRejectedError):
    """The key's user is not the connection's: the owner has to enter the right keys."""


class StatementRejected(DisplayableError, ValueError):
    """The upload is not a statement of this connection's eToro Trading account, or cannot be
    read as one; the message names a sheet, a column or a rule, never a value (422)."""


class StatementTooEarly(DisplayableError):
    """A statement uploaded before the connection's first sync has finished (409, design
    §5.3): its checks need the user and accounts that sync stores."""


@dataclass(frozen=True)
class IngestResult:
    raw_record_ids: list[UUID]
    # The reader's flags ("<sheet>.<column>", an unknown sheet, an unknown label).
    flagged: frozenset[str]
    # The statement's document, whose bytes the caller writes last (record_document).
    document_sha256: str


def _shape(endpoint: str, field: str) -> EtoroSyncError:
    return EtoroSyncError(f"eToro's {endpoint} answer has an unexpected shape: {field}")


def _id_text(value: ApiValue, endpoint: str, field: str) -> str:
    """A positive whole id as text."""
    if (
        isinstance(value, Decimal)
        and value.is_finite()
        and value == value.to_integral_value()
        and value > 0
        and value.adjusted() < 19
    ):
        return str(int(value))
    raise _shape(endpoint, field)


def me_ids(me: Item) -> tuple[str, str]:
    """`/me`'s (gcid, realCid), each a positive whole id as text."""
    return _id_text(me.get("gcid"), "/me", "gcid"), _id_text(me.get("realCid"), "/me", "realCid")


def _optional_id(value: ApiValue, endpoint: str, field: str) -> str | None:
    """An id that may be absent: none or 0 is no id."""
    if value is None or (isinstance(value, Decimal) and value == 0):
        return None
    return _id_text(value, endpoint, field)


def _objects(container: Mapping[str, ApiValue], key: str, endpoint: str) -> list[Item]:
    """The objects of a list field; an absent or null one is none (the normalizer checks what
    is required)."""
    value = container.get(key)
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise _shape(endpoint, key)
    return [item for item in value if isinstance(item, dict)]


def _object(container: Mapping[str, ApiValue], key: str) -> Item:
    value = container.get(key)
    return value if isinstance(value, dict) else {}


def _day(value: ApiValue, endpoint: str, field: str) -> date:
    if not isinstance(value, str):
        raise _shape(endpoint, field)
    try:
        return parse_date(value, f"{endpoint} {field}", "T")
    except TypedValueError:
        raise _shape(endpoint, field) from None


def _cursor_date(cursor: Mapping[str, JSONValue], key: str) -> date | None:
    return _iso_date(cursor.get(key))


def _iso_date(value: JSONValue) -> date | None:
    if not isinstance(value, str):
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def _cursor_texts(cursor: Mapping[str, JSONValue], key: str) -> dict[str, str]:
    value = cursor.get(key)
    if not isinstance(value, dict):
        return {}
    return {k: v for k, v in value.items() if isinstance(v, str)}


def _due(cursor: Mapping[str, JSONValue], key: str, every: timedelta, now: datetime) -> bool:
    value = cursor.get(key)
    if not isinstance(value, str):
        return True
    try:
        last = datetime.fromisoformat(value)
    except ValueError:
        return True
    return last.tzinfo is None or now - last >= every


async def publish_flags(
    ctx: SyncContext, found: Iterable[tuple[str, str]], checked: Collection[str]
) -> dict[str, JSONValue]:
    """Publish this run's flags (connector_flags), the cursor's as the connection stands now:
    an upload runs outside any sync, so a sync must see what one published meanwhile. A part
    not `checked` keeps its flags (a sync keeps the statement's, an upload the normalizer's
    unless it replayed). Returns the cursor's `flags` and `notified`."""
    return await connector_flags.publish_flags(
        ctx, found, checked, cursor=None, prefix=FLAGGED_PREFIX, label="an eToro"
    )


class _Seen:
    """`seen` for a walk newest first: the item the cursor remembers, and every one after it,
    which is older. Without one (a first sync) nothing is seen."""

    def __init__(self, marker: str | None) -> None:
        self.marker = marker
        self.reached = False

    def __call__(self, item: Item) -> bool:
        if not self.reached and self.marker is not None:
            self.reached = item.get("id") == self.marker
        return self.reached


class _Run:
    """One sync: the client, the cursor it began with, and the raw records it has stored."""

    def __init__(
        self, ctx: SyncContext, client: EtoroClient, cursor: Mapping[str, JSONValue]
    ) -> None:
        self.ctx = ctx
        self.client = client
        self.cursor = cursor
        self.ids: list[UUID] = []

    async def store(
        self,
        endpoint: str,
        item: Item,
        *,
        path: str | None = None,
        params: Mapping[str, str] | None = None,
        seen_at: datetime | None = None,
    ) -> UUID:
        """Store the item; a report (`seen_at`: when its response arrived) is also sighted,
        stored now or before (report_sightings)."""
        raw_record_id = await store_raw_record(
            self.ctx.command,
            connection_id=self.ctx.connection.id,
            record_type=ENDPOINTS[endpoint].record_type,
            request=_request(path or endpoint_path(endpoint), params),
            content=record_json(item),
            connector_version=VERSION,
            sync_id=self.ctx.sync_id,
        )
        self.ids.append(raw_record_id)
        if endpoint in REPORTS:
            assert seen_at is not None, "a report is stored with the time it was seen"
            await record_sighting(
                self.ctx.command,
                connection_id=self.ctx.connection.id,
                raw_record_id=raw_record_id,
                sync_id=self.ctx.sync_id,
                seen_at=seen_at,
            )
        return raw_record_id

    async def keep_refused(self, scopes: list[str]) -> None:
        """Record a refused key's scopes, and nothing else of its `/me` (the key may be another
        user's), in a command of its own, which the sync's failure does not roll back."""
        ctx = self.ctx
        args: dict[str, JSONValue] = {"connection_id": str(ctx.connection.id)}
        async with (
            ctx.session_factory() as session,
            command_context(session, ctx.command.actor, "etoro.keep_refused_key", args) as cmd,
        ):
            await store_raw_record(
                cmd,
                connection_id=ctx.connection.id,
                record_type=REFUSED_KEY,
                request=_request(endpoint_path("me"), None),
                content={"scopes": list[JSONValue](scopes)},
                connector_version=VERSION,
                sync_id=ctx.sync_id,
            )

    async def gate(self) -> tuple[str, str]:
        """`/me` and the read-only gate; the user's (gcid, realCid)."""
        me = await self.client.me()
        arrived = datetime.now(UTC)
        scopes = me.get("scopes")
        # The client refuses a missing or empty list; the connector checks the scopes itself.
        named = [s for s in scopes if isinstance(s, str)] if isinstance(scopes, list) else []
        if not isinstance(scopes, list) or write_scopes(named):
            try:
                await self.keep_refused(named)
            except Exception as error:
                # The refusal stands whatever happens to its record; the class only is logged.
                logger.warning("recording a refused eToro key failed (%s)", type(error).__name__)
            raise ReadOnlyGateError(GATE_MESSAGE)
        gcid, real_cid = me_ids(me)
        known = self.cursor.get("gcid")
        if isinstance(known, str) and known != gcid:
            raise AccountMismatchError("this key belongs to another eToro user")
        await self.store("me", me, seen_at=arrived)
        return gcid, real_cid

    async def reports(self) -> tuple[Item, Item, Item, Item]:
        """Balances, PnL, aggregate portfolio and instrument breakdown, each sighted when its
        response arrived."""
        balances = await self.client.balances()
        await self.store("balances", balances, params=_BALANCES, seen_at=datetime.now(UTC))
        pnl = await self.client.pnl()
        await self.store("pnl", pnl, seen_at=datetime.now(UTC))
        aggregate = await self.client.aggregate()
        await self.store("aggregate", aggregate, seen_at=datetime.now(UTC))
        breakdown = await self.client.breakdown()
        await self.store("breakdown", breakdown, params=_BREAKDOWN, seen_at=datetime.now(UTC))
        return balances, pnl, aggregate, breakdown

    async def cash(self, balances: Item) -> dict[str, JSONValue]:
        """Each Cash account's transactions, newest first, to the newest seen before."""
        markers = _cursor_texts(self.cursor, "seen_cash")
        seen: dict[str, JSONValue] = {}
        for account in _objects(balances, "balances", "balances"):
            if account.get("accountType") != "Cash":
                continue
            account_id = account.get("accountId")
            if not isinstance(account_id, str):
                raise _shape("balances", "balances[].accountId")
            try:
                items = await self.client.cash_transactions(
                    account_id, _Seen(markers.get(account_id))
                )
            except ValueError:
                raise _shape("balances", "balances[].accountId") from None
            path = f"{endpoint_path('cash_transactions')}/{account_id}/transactions"
            for item in items:
                if not isinstance(item.get("id"), str):
                    raise _shape("cash transactions", "id")
                await self.store("cash_transactions", item, path=path)
            newest = items[0].get("id") if items else markers.get(account_id)
            if isinstance(newest, str):
                seen[account_id] = newest
        return seen

    async def window(self, min_date: date) -> list[Item]:
        """One trade-history window, stored as it arrives."""
        rows = await self.client.trade_history(min_date)
        for row in rows:
            await self.store("trade_history", row, params={"minDate": min_date.isoformat()})
        return rows

    async def opening(self) -> date | None:
        """The account's opening: the earliest Date Created of the statements stored."""
        values = await self.ctx.command.session.scalars(
            text(
                "select content->'cells'->>:value from core.raw_records"
                " where connection_id = :c and record_type = :t"
                " and btrim(content->'cells'->>:label) = 'Date Created'"
            ),
            {
                "c": self.ctx.connection.id,
                "t": STATEMENT_SHEETS[ACCOUNT_SUMMARY].record_type,
                "label": LABEL,
                "value": VALUE,
            },
        )
        days = [
            read_moment(value.strip(), "Date Created", STATEMENT_ZONE).date()
            for value in values
            if isinstance(value, str)
        ]
        return min(days, default=None)

    async def trades(self, today: date) -> tuple[date, list[Item]]:
        """The trade history this sync asks (design §5.2, step 4); (the latest close date
        stored, or today when there is none, and every row fetched)."""
        latest = _cursor_date(self.cursor, "trade_history_to")
        span = timedelta(days=TRADE_WINDOW_DAYS)
        if latest is not None:
            # From the latest close less the overlap, however long ago: a cap at a year would
            # skip what closed during a long outage.
            rows = await self.window(latest - OVERLAP)
            return _latest_close(rows, latest), rows
        opening = await self.opening()
        seen: set[str] = set()
        fetched: list[Item] = []
        for k in range(1, MAX_WINDOWS + 1):
            min_date = today - span * k
            rows = await self.window(min_date)
            fetched.extend(rows)
            ids = {_id_text(row.get("positionId"), "trade history", "positionId") for row in rows}
            new = ids - seen
            seen |= ids
            if (min_date <= opening) if opening is not None else not new:
                return _latest_close(fetched, today if not fetched else None), fetched
        raise EtoroSyncError("eToro's trade history did not end")

    async def orders(self, pnl: Item, trades: list[Item]) -> list[JSONValue]:
        """`orders:lookup` for each order not looked up before; every order looked up."""
        before = self.cursor.get("looked_up")
        looked_up: set[str] = (
            {v for v in before if isinstance(v, str)} if isinstance(before, list) else set()
        )
        wanted: set[str] = set()
        for position in _objects(_object(pnl, "clientPortfolio"), "positions", "pnl"):
            order_id = _optional_id(position.get("orderID"), "pnl", "positions[].orderID")
            if order_id is not None:
                wanted.add(order_id)
        for row in trades:
            order_id = _optional_id(row.get("orderId"), "trade history", "orderId")
            if order_id is not None:
                wanted.add(order_id)
        for order_id in sorted(wanted - looked_up, key=int):
            order = await self.client.order_lookup(Decimal(order_id))
            if order is not None:
                await self.store("order_lookup", order, params={"orderId": order_id})
            # An order eToro does not know (404) is not asked again either.
            looked_up.add(order_id)
        return list[JSONValue](sorted(looked_up, key=int))

    async def balance_history(self, today: date) -> str | None:
        """Snapshots since the latest stored less 7 days (the first sync: 365 days); the
        latest snapshot date stored."""
        latest = _cursor_date(self.cursor, "balance_history_to")
        oldest = today - timedelta(days=BALANCE_HISTORY_DAYS - 1)
        start = oldest if latest is None else min(max(latest - OVERLAP, oldest), today)
        body = await self.client.balance_history(start, today)
        params = {"fromDate": start.isoformat(), "toDate": today.isoformat()}
        await self.store("balance_history", body, params=params, seen_at=datetime.now(UTC))
        days = [
            _day(s.get("date"), "balances/history", "snapshots[].date")
            for s in _objects(body, "snapshots", "balances/history")
        ]
        if latest is not None:
            days.append(latest)
        return max(days).isoformat() if days else None

    async def daily(
        self, today: date, pnl: Item, aggregate: Item, breakdown: Item, trades: list[Item]
    ) -> dict[str, JSONValue]:
        """Closed-events, stockback, market data of the instruments held or traded, and
        candles of those held; the cursor's `candles_to`."""
        for item in await self.client.closed_events():
            await self.store("closed_events", item)
        for item in await self.client.stockback():
            await self.store("stockback", item)

        opened: dict[str, date] = {}
        for position in _objects(_object(pnl, "clientPortfolio"), "positions", "pnl"):
            instrument = _id_text(position.get("instrumentID"), "pnl", "positions[].instrumentID")
            day = position.get("openDateTime")
            first = _day(day, "pnl", "positions[].openDateTime") if day is not None else today
            opened[instrument] = min(opened.get(instrument, first), first)
        held = set(opened)
        for item in _objects(aggregate, "instrumentAggregates", "aggregate-portfolio"):
            held.add(_id_text(item.get("instrumentId"), "aggregate-portfolio", "instrumentId"))
        for item in _objects(breakdown, "instruments", "instrument-breakdown"):
            held.add(_id_text(item.get("instrumentId"), "instrument-breakdown", "instrumentId"))
        traded = {_id_text(r.get("instrumentId"), "trade history", "instrumentId") for r in trades}

        for item in await self.client.instruments([int(i) for i in sorted(held | traded, key=int)]):
            await self.store("instruments", item)
        for item in await self.client.instrument_types():
            await self.store("instrument_types", item)
        for item in await self.client.exchanges():
            await self.store("exchanges", item)

        fetched = _cursor_texts(self.cursor, "candles_to")
        candles_to: dict[str, JSONValue] = dict(fetched)
        for instrument in sorted(held, key=int):
            last = _iso_date(fetched.get(instrument))
            start = (
                min(last - OVERLAP, today) if last is not None else opened.get(instrument, today)
            )
            path = f"{endpoint_path('candles')}/{instrument}/candles"
            params = {"from": start.isoformat(), "to": today.isoformat()}
            for item in await self.client.candles(int(instrument), start, today):
                # A candle names no instrument; its page's instrumentId is the one asked for.
                candle: Item = {**item, "instrumentId": Decimal(instrument)}
                await self.store("candles", candle, path=path, params=params)
            candles_to[instrument] = today.isoformat()
        return candles_to

    async def sync(self, now: datetime) -> dict[str, JSONValue]:
        today = now.date()
        gcid, real_cid = await self.gate()
        balances, pnl, aggregate, breakdown = await self.reports()
        seen_cash = await self.cash(balances)
        trade_history_to, trades = await self.trades(today)
        looked_up = await self.orders(pnl, trades)
        balance_history_to = await self.balance_history(today)
        daily = _due(self.cursor, "daily_at", DAILY_EVERY, now)
        candles_to: JSONValue = self.cursor.get("candles_to")
        if daily:
            candles_to = await self.daily(today, pnl, aggregate, breakdown, trades)
        return {
            "gcid": gcid,
            "real_cid": real_cid,
            "trade_history_to": trade_history_to.isoformat(),
            "balance_history_to": balance_history_to,
            "seen_cash": seen_cash,
            "daily_at": now.isoformat() if daily else self.cursor.get("daily_at"),
            "looked_up": looked_up,
            "candles_to": candles_to if isinstance(candles_to, dict) else {},
        }


# The queries the client sends for balances and the breakdown, noted on their raw records.
_BALANCES = {"includeZeroBalances": "true", "includeSubAccounts": "true", "expand": "equityDetails"}
_BREAKDOWN = {"positionLevel": "Normal", "orderLevel": "Normal", "mirrorLevel": "Details"}


def _request(path: str, params: Mapping[str, str] | None) -> dict[str, JSONValue]:
    return {"endpoint": path, "params": dict(params or {})}


def _latest_close(rows: list[Item], floor: date | None) -> date:
    """The latest close date among the rows, or `floor` (one of them is given)."""
    days = [_day(r.get("closeTimestamp"), "trade history", "closeTimestamp") for r in rows]
    if floor is not None:
        days.append(floor)
    return max(days)


class EtoroConnector:
    name = "etoro"
    version = VERSION
    capabilities: frozenset[Capability] = frozenset({"balances", "holdings", "transactions"})

    async def check_access(self, ctx: SyncContext) -> bool:
        # There is no approval step: a key works as soon as it is created.
        return True

    async def sync(self, ctx: SyncContext) -> JSONValue:
        connection = ctx.connection
        session = ctx.command.session
        api_key = await load_credential(session, ctx.master_key, connection.id, KEY_PURPOSE)
        user_key = await load_credential(session, ctx.master_key, connection.id, USER_KEY_PURPOSE)
        if api_key is None or user_key is None:
            raise CredentialRejectedError("The eToro Public API Key or User Key is missing")
        before = connection.cursor
        cursor: Mapping[str, JSONValue] = before if isinstance(before, dict) else {}
        run = _Run(ctx, EtoroClient(ctx.http, api_key, user_key), cursor)
        after = await run.sync(datetime.now(UTC))
        # Locked from here until the sync commits: an upload normalizes the same connection
        # one at a time with it (each sees the other's committed rows; two at once could
        # deadlock on its review items), and its flags published meanwhile are read here,
        # never overwritten after.
        await lock_connection(ctx.command, connection.id)
        report = await normalize(ctx, list(dict.fromkeys(run.ids)))
        checked = {FIELDS, IN_FULL} if report.replayed else {IN_FULL}
        found = {(FIELDS, flag) for flag in report.flagged}
        found |= {(IN_FULL, flag) for flag in report.flags_in_full}
        after |= await publish_flags(ctx, found, checked)
        # Its cursor replaces the row's: it keeps what reconciliation recorded.
        after |= report.cursor
        return after


# A statement is decoded in full in memory, so it is parsed off the event loop, one at a time,
# as IBKR's statements are.
_PARSING = threading.Lock()


def _parse_alone(body: bytes) -> EtoroStatement:
    with _PARSING:
        return parse_statement(body, STATEMENT_ZONE)


async def _latest_report(ctx: SyncContext, record_type: str) -> Mapping[str, JSONValue] | None:
    """The content of the report record of this type a sync saw last: only sighted records
    count, so a refused key (stored as `etoro.refused_key`, never sighted) has no say."""
    content = await ctx.command.session.scalar(
        text(
            "select r.content from core.raw_records r"
            " join finance.report_sightings s"
            " on s.raw_record_id = r.id and s.connection_id = r.connection_id"
            " where r.connection_id = :c and r.record_type = :t"
            " order by s.seen_at desc, s.sync_id desc limit 1"
        ),
        {"c": ctx.connection.id, "t": record_type},
    )
    return cast(dict[str, JSONValue], content) if isinstance(content, dict) else None


def _trading_currencies(balances: Mapping[str, JSONValue]) -> list[JSONValue]:
    """The currency of each Trading account a stored balances record lists."""
    accounts = balances.get("balances")
    if not isinstance(accounts, list):
        return []
    return [
        account.get("currency")
        for account in accounts
        if isinstance(account, dict) and account.get("accountType") == "Trading"
    ]


async def _check_statement(ctx: SyncContext, statement: EtoroStatement) -> None:
    """The statement must be the connection's Trading account's (design §5.3, §5.5)."""
    me = await _latest_report(ctx, ENDPOINTS["me"].record_type)
    balances = await _latest_report(ctx, ENDPOINTS["balances"].record_type)
    if me is None or balances is None:
        raise StatementTooEarly("wait for the eToro connection's first sync to finish")
    username = me.get("username")
    if not isinstance(username, str) or (
        statement.username.strip().casefold() != username.strip().casefold()
    ):
        raise StatementRejected("this statement is for another eToro user")
    currencies = _trading_currencies(balances)
    currency = statement.account_currency  # the first code (R41): the second is GBP's figures
    if not currencies or any(
        not isinstance(c, str) or c.strip().upper() != currency for c in currencies
    ):
        raise StatementRejected(
            "this statement's currency is not the eToro Trading account's currency"
        )
    earlier = await ctx.command.session.scalar(
        text(
            "select exists (select 1 from core.raw_records"
            " where connection_id = :c and record_type = :t)"
        ),
        {"c": ctx.connection.id, "t": STATEMENT_SHEETS[ACCOUNT_SUMMARY].record_type},
    )
    if not earlier and statement.period_from > statement.created:
        # The first statement and the API together must cover the account's whole life. The
        # date (Date Created, the day only) is shown to its owner alone (R27).
        raise StatementRejected(
            "The first eToro statement must start on or before"
            f" {statement.created.date().isoformat()}."
        )


async def parse_upload(body: bytes) -> EtoroStatement:
    """Read an uploaded statement, off the event loop; one that cannot be read is a
    `StatementRejected`. Needs no database: a caller may parse before it takes any lock."""
    try:
        return await asyncio.to_thread(_parse_alone, body)
    except StatementError as error:
        raise StatementRejected(str(error)) from None


async def ingest_statement(
    ctx: SyncContext, body: bytes, filename: str, statement: EtoroStatement | None = None
) -> IngestResult:
    """Check an uploaded statement, record it as a document and store one raw record per row,
    then normalise those records and publish the reader's and the normalizer's flags.
    `statement` is `body` as `parse_upload` read it, when the caller has already.

    The document's bytes are not written here: the caller writes them
    (`store.put(result.document_sha256, body)`) once the rest of its command is done, just
    before it commits (record_document), so a command that fails leaves no file behind."""
    # Parsed and checked first: a statement that is refused leaves nothing behind.
    if statement is None:
        statement = await parse_upload(body)
    await _check_statement(ctx, statement)
    document = await record_document(
        ctx.command, body, filename=filename, media_type=XLSX, source=DOCUMENT_SOURCE
    )
    # The period is the upload's, not the row's: a row two statements share is one record.
    period: dict[str, JSONValue] = {
        "from": statement.period_from.isoformat(),
        "to": statement.period_to.isoformat(),
    }
    request: dict[str, JSONValue] = {
        "upload": filename,
        "document_sha256": document.sha256,
        "period": period,
    }
    ids: list[UUID] = []
    for row in statement.rows:
        content: JSONValue = {"cells": dict(row.cells), "key": row.key}
        ids.append(
            await store_raw_record(
                ctx.command,
                connection_id=ctx.connection.id,
                record_type=STATEMENT_SHEETS[row.sheet].record_type,
                request=request,
                content=content,
                connector_version=VERSION,
                sync_id=ctx.sync_id,
            )
        )
    ids = list(dict.fromkeys(ids))
    # Locked from here until the upload commits: a sync normalizes the same connection one at
    # a time with it, and the flags read here are the ones replaced; only the flag keys are
    # merged, so no other cursor key is overwritten.
    await lock_connection(ctx.command, ctx.connection.id)
    report = await normalize(ctx, ids)
    found = {(STATEMENT, flag) for flag in statement.flagged}
    found |= {(FIELDS, flag) for flag in report.flagged}
    found |= {(IN_FULL, flag) for flag in report.flags_in_full}
    checked = {STATEMENT, IN_FULL} | ({FIELDS} if report.replayed else set[str]())
    flags = await publish_flags(ctx, found, checked)
    await merge_cursor(ctx.command, ctx.connection.id, flags)
    return IngestResult(ids, statement.flagged, document.sha256)
