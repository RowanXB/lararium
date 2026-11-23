"""Trading 212 sync: fetch one account's data and store every item as a raw record, then
normalise (normalize.py).

Per sync (design §5.2): the account summary, positions and pending orders; each history
newest first, until a page holds only items seen before (the first sync and a weekly re-walk
read to the end); once a day, instrument and exchange metadata and the pies. A history walk
stores each page as it arrives. One that reaches the client's MAX_PAGES keeps what it stored
and the next page's path (`resume`), and the next sync goes on from there after its own
newest-first pass. Items are stored as Trading 212 sent them; nothing here logs a value.

A history page after the first that answers 404 (design §3.2) stops that walk the same way,
its path kept to ask again, and is flagged. An optional part skipped (a permission not ticked,
the deprecated pies list gone) is flagged too: a connection warning, the owner told once
(`publish_flags`). A required permission missing rejects the key: it needs a new one.

The normalizer's flags (fields, values and instrument types it does not know, a TRANSFER) are
published the same way, as the part `fields`. A run normalizes only what it fetched, so they
add to the ones before; a run that replays every record (a new normalizer version) has them
all, and replaces them. The instruments still without a type are found in full by every run,
as the part `instrument_types`.
"""

import logging
from collections.abc import Collection, Iterable, Mapping
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID

from lararium.core.connections import get_connection, load_credential, set_warnings
from lararium.core.connectors import Capability, SyncContext
from lararium.core.errors import CredentialRejectedError, DisplayableError
from lararium.core.json_types import JSONValue
from lararium.core.raw_records import store_raw_record
from lararium.modules.finance.readonly_http import HttpForbiddenError, record_json
from lararium.modules.finance.report_sightings import record_sighting
from lararium.modules.finance.trading212.client import (
    PIES_REMOVED,
    HistoryKind,
    HistoryNotEndedError,
    HistoryStoppedError,
    Item,
    Trading212Client,
    endpoint_path,
)
from lararium.modules.finance.trading212.fields import ENDPOINTS
from lararium.modules.finance.trading212.normalize import normalize

logger = logging.getLogger(__name__)

VERSION = "trading212-sync-1"
KEY_PURPOSE = "api_key"
SECRET_PURPOSE = "api_secret"
# Decision 6: the history is walked to its end again weekly; metadata and pies are daily.
REWALK_EVERY = timedelta(days=7)
METADATA_EVERY = timedelta(hours=24)
KINDS: tuple[HistoryKind, ...] = ("orders", "dividends", "transactions")
FLAGGED_PREFIX = "Trading 212 flagged: "
# The part the normalizer's flags are published under.
FIELDS = "fields"
INSTRUMENT_TYPES = "instrument_types"
_STOPPED = "history stopped early (404)"
# What Trading 212 reports now, rather than records: each storing is sighted.
REPORTS = frozenset({"summary", "positions", "pending_orders", "pies", "pie"})


class Trading212SyncError(DisplayableError):
    """The sync cannot go on; the message names what to fix, never a value."""


class AccountMismatchError(Trading212SyncError, CredentialRejectedError):
    """The key's account is not the connection's: the owner has to enter the right key."""


class MissingPermissionError(Trading212SyncError, CredentialRejectedError):
    """The key lacks a permission the sync needs. Trading 212 sets a key's permissions when it
    is created, so the owner has to create a new key (design §5.13: told at once)."""


def _text(value: object) -> str | None:
    if isinstance(value, Decimal):
        return str(value)
    return value if isinstance(value, str) else None


def _key(kind: HistoryKind, item: Item) -> str | None:
    """What the cursor remembers of an item: an order's fill id (an order without a fill has
    none), a dividend's or transaction's reference."""
    if kind == "orders":
        fill = item.get("fill")
        return _text(fill.get("id")) if isinstance(fill, dict) else None
    return _text(item.get("reference"))


class _Seen:
    """`seen` for a walk newest first: the item the cursor remembers, and every one after it,
    which is older. Without one (a first sync, a re-walk) nothing is seen."""

    def __init__(self, kind: HistoryKind, marker: str | None) -> None:
        self.kind: HistoryKind = kind
        self.marker = marker
        self.reached = False

    def __call__(self, item: Item) -> bool:
        if not self.reached and self.marker is not None:
            self.reached = _key(self.kind, item) == self.marker
        return self.reached


def _never(item: Item) -> bool:
    return False


def summary_account_id(summary: Item) -> str:
    value = summary.get("id")
    if (
        not isinstance(value, Decimal)
        or not value.is_finite()
        or value.as_tuple().exponent != 0
        or value <= 0
    ):
        raise Trading212SyncError("Trading 212's account/summary answer has no account id")
    return str(value)


def _due(cursor: Mapping[str, JSONValue], key: str, every: timedelta, now: datetime) -> bool:
    value = cursor.get(key)
    if not isinstance(value, str):
        return True
    try:
        last = datetime.fromisoformat(value)
    except ValueError:
        return True
    return last.tzinfo is None or now - last >= every


def _texts(cursor: Mapping[str, JSONValue], key: str) -> dict[str, str]:
    value = cursor.get(key)
    if not isinstance(value, dict):
        return {}
    return {k: v for k, v in value.items() if isinstance(v, str)}


def _flags(cursor: Mapping[str, JSONValue]) -> dict[str, list[str]]:
    """The sync's flags the cursor keeps: part → reasons."""
    value = cursor.get("flags")
    if not isinstance(value, dict):
        return {}
    return {
        part: [r for r in reasons if isinstance(r, str)]
        for part, reasons in value.items()
        if isinstance(reasons, list)
    }


def _own(warning: str, kept: Collection[str]) -> bool:
    """A flag the sync publishes: one the cursor keeps (the normalizer's among them), or one
    of the sync's shapes. Other `Trading 212 flagged:` warnings are left to their writer."""
    reason = warning.removeprefix(FLAGGED_PREFIX)
    return warning.startswith(FLAGGED_PREFIX) and (
        reason in kept
        or reason.startswith("missing permission ")
        or reason == PIES_REMOVED
        or reason.endswith(f" {_STOPPED}")
    )


async def publish_flags(
    ctx: SyncContext,
    found: Iterable[tuple[str, str]],
    checked: Collection[str],
    cursor: Mapping[str, JSONValue],
) -> dict[str, JSONValue]:
    """Replace the sync's own flags among the connection's warnings with this run's, keeping
    every other warning, as it stands now (normalize, say, may have written some). `found`:
    (part, reason) of this run; a part not `checked` this run (the pies, but on metadata days)
    keeps the flags the cursor holds for it. The owner is told, in one message, of each flag not
    yet told: a flag counts as told only once a send succeeds, so a failed one is retried next
    sync; one whose cause is gone clears without a word. Returns the cursor's `flags` and
    `notified`."""
    before = _flags(cursor)
    parts: dict[str, set[str]] = {}
    for part, reason in found:
        parts.setdefault(part, set()).add(reason)
    for part, reasons in before.items():
        if part not in checked:
            parts.setdefault(part, set()).update(reasons)
    reasons = sorted({reason for found_reasons in parts.values() for reason in found_reasons})
    told_before = cursor.get("notified")
    told = set(told_before) if isinstance(told_before, list) else set[JSONValue]()
    notified = [reason for reason in reasons if reason in told]
    new = [reason for reason in reasons if reason not in told]
    connection = ctx.connection
    if new:
        try:
            await ctx.notifier.send(f"{connection.display_name}: {FLAGGED_PREFIX}{', '.join(new)}")
            notified = reasons
        except Exception as error:
            # The class only: the message could carry the notifier's webhook URL.
            logger.warning("sending a Trading 212 notification failed (%s)", type(error).__name__)
    kept = {reason for old in before.values() for reason in old}
    previous = (await get_connection(ctx.command.session, connection.id)).warnings
    warnings = [w for w in previous if not _own(w, kept)] + [FLAGGED_PREFIX + r for r in reasons]
    if warnings != previous:
        await set_warnings(ctx.command, connection.id, warnings)
    return {
        "flags": {
            part: list[JSONValue](sorted(found_reasons))
            for part, found_reasons in sorted(parts.items())
        },
        "notified": list[JSONValue](notified),
    }


class _Run:
    """One sync: the client, and the raw records it has stored."""

    def __init__(self, ctx: SyncContext, client: Trading212Client) -> None:
        self.ctx = ctx
        self.client = client
        self.ids: list[UUID] = []
        # The parts whose flags this run decides, and the (part, reason) flags it found.
        self.checked: set[str] = set()
        self.found: set[tuple[str, str]] = set()

    async def store(
        self,
        endpoint: str,
        item: Item,
        request_path: str | None = None,
        params: Mapping[str, str] | None = None,
        *,
        seen_at: datetime | None = None,
    ) -> None:
        """Store the item; a report (`seen_at`: when its response arrived) is also sighted,
        stored now or before (report_sightings)."""
        request: dict[str, JSONValue] = {
            "endpoint": request_path or endpoint_path(endpoint),
            "params": dict(params or {}),
        }
        raw_record_id = await store_raw_record(
            self.ctx.command,
            connection_id=self.ctx.connection.id,
            record_type=ENDPOINTS[endpoint].record_type,
            request=request,
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

    async def _pages(
        self, kind: HistoryKind, seen: _Seen | None, start: str | None
    ) -> tuple[str | None, str | None]:
        """Store a walk's pages; (the newest item's key, the page to go on from when the walk
        reached the cap or a page after the first answered 404, which is flagged)."""
        newest: str | None = None
        try:
            async for page in self.client.history(kind, seen or _never, start=start):
                for item in page.items:
                    newest = newest or _key(kind, item)
                    await self.store(kind, item, params=page.params)
        except HistoryNotEndedError as capped:
            return newest, capped.resume
        except HistoryStoppedError as stopped:
            self.found.add((kind, f"{kind} {_STOPPED}"))
            return newest, stopped.resume
        return newest, None

    async def walk(
        self, kind: HistoryKind, marker: str | None, resume: str | None
    ) -> tuple[str | None, str | None]:
        """Walk a history newest first, stopping at `marker` (None: to the end), then on from
        `resume` when the walk stopped there. (The newest key, the resume left.)

        A walk that did not reach `marker` and was not cut short drops `resume`. When it ended
        at a null `nextPagePath` it has passed every page `resume` points to. When it ended at a
        page repeating the one before (a pagination fault) it may not have: the weekly re-walk
        is what finds what lay beyond."""
        self.checked.add(kind)
        seen = _Seen(kind, marker)
        newest, left = await self._pages(kind, seen, None)
        if left is None and seen.reached and resume is not None:
            _, left = await self._pages(kind, None, resume)
        return newest, left

    async def metadata(self) -> None:
        self.checked.add("pies")
        for item in await self.client.instruments():
            await self.store("instruments", item)
        for item in await self.client.exchanges():
            await self.store("exchanges", item)
        pies = await self.client.pies() or []
        arrived = datetime.now(UTC)
        for pie in pies:
            await self.store("pies", pie, seen_at=arrived)
            pie_id = pie.get("id")
            if not isinstance(pie_id, Decimal):
                raise Trading212SyncError("Trading 212's pies answer has no pie id")
            try:
                detail = await self.client.pie(pie_id)
            except ValueError:
                raise Trading212SyncError("Trading 212's pies answer has no pie id") from None
            if detail is not None:
                await self.store(
                    "pie",
                    detail,
                    f"{endpoint_path('pie')}/{int(pie_id)}",
                    seen_at=datetime.now(UTC),
                )


class Trading212Connector:
    name = "trading212"
    version = VERSION
    capabilities: frozenset[Capability] = frozenset({"balances", "holdings", "transactions"})

    async def check_access(self, ctx: SyncContext) -> bool:
        # There is no approval step: a key works as soon as it is created.
        return True

    async def sync(self, ctx: SyncContext) -> JSONValue:
        connection = ctx.connection
        session = ctx.command.session
        key = await load_credential(session, ctx.master_key, connection.id, KEY_PURPOSE)
        secret = await load_credential(session, ctx.master_key, connection.id, SECRET_PURPOSE)
        if key is None or secret is None:
            raise CredentialRejectedError("The Trading 212 API key or secret is missing")
        run = _Run(ctx, Trading212Client(ctx.http, key, secret))
        before = connection.cursor
        cursor: Mapping[str, JSONValue] = before if isinstance(before, dict) else {}
        try:
            after = await self._sync(run, cursor, datetime.now(UTC))
        except HttpForbiddenError as error:
            if error.cloudflare_block or error.scope is None:
                raise
            # The client keeps a scope only when it is a known permission name.
            raise MissingPermissionError(
                f"Trading 212 key lacks the {error.scope} permission"
            ) from None
        report = await normalize(ctx, list(dict.fromkeys(run.ids)))
        if report.replayed:
            run.checked.add(FIELDS)
        run.checked.add(INSTRUMENT_TYPES)
        found = run.found | run.client.skipped | {(FIELDS, flag) for flag in report.flagged}
        found |= {(INSTRUMENT_TYPES, flag) for flag in report.instrument_flags}
        after |= await publish_flags(ctx, found, run.checked, cursor)
        return after

    async def _sync(
        self, run: _Run, cursor: Mapping[str, JSONValue], now: datetime
    ) -> dict[str, JSONValue]:
        client = run.client
        summary = await client.summary()
        # When the summary arrived: what it reports holds then (the reconciliation's and the
        # opening's cut-off), not at the transaction's start, before the request.
        arrived = datetime.now(UTC)
        account_id = summary_account_id(summary)
        known = cursor.get("account_id")
        if isinstance(known, str) and known != account_id:
            raise AccountMismatchError("this key belongs to another Trading 212 account")
        await run.store("summary", summary, seen_at=arrived)
        positions = await client.positions()
        arrived = datetime.now(UTC)
        for item in positions:
            await run.store("positions", item, seen_at=arrived)
        run.checked.add("pending_orders")
        pending = await client.pending_orders() or []
        arrived = datetime.now(UTC)
        for item in pending:
            await run.store("pending_orders", item, seen_at=arrived)

        rewalk = _due(cursor, "rewalk_at", REWALK_EVERY, now)
        seen_before = _texts(cursor, "seen")
        resume_before = _texts(cursor, "resume")
        seen: dict[str, JSONValue] = {}
        resume: dict[str, JSONValue] = {}
        for kind in KINDS:
            marker = seen_before.get(kind)
            newest, left = await run.walk(kind, None if rewalk else marker, resume_before.get(kind))
            if (latest := newest or marker) is not None:
                seen[kind] = latest
            if left is not None:
                resume[kind] = left

        metadata = _due(cursor, "metadata_at", METADATA_EVERY, now)
        if metadata:
            await run.metadata()
        return {
            "account_id": account_id,
            "metadata_at": now.isoformat() if metadata else cursor.get("metadata_at"),
            "rewalk_at": now.isoformat() if rewalk else cursor.get("rewalk_at"),
            "seen": seen,
            "resume": resume,
        }
