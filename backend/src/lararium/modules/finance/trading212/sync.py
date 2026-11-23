"""Trading 212 sync: fetch one account's data and store every item as a raw record, then
normalise (normalize.py).

Per sync (design §5.2): the account summary, positions and pending orders; each history
newest first, until a page holds only items seen before (the first sync and a weekly re-walk
read to the end); once a day, instrument and exchange metadata and the pies. A history walk
stores each page as it arrives. One that reaches the client's MAX_PAGES keeps what it stored
and the next page's path (`resume`), and the next sync goes on from there after its own
newest-first pass. Items are stored as Trading 212 sent them; nothing here logs a value.

An optional part skipped (a permission not ticked, the deprecated pies endpoints gone) is
flagged as a connection warning, and the owner is told once (`publish_flags`).
"""

import logging
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID

from lararium.core.connections import load_credential, set_warnings
from lararium.core.connectors import Capability, SyncContext
from lararium.core.errors import CredentialRejectedError, DisplayableError
from lararium.core.json_types import JSONValue
from lararium.core.raw_records import store_raw_record
from lararium.modules.finance.readonly_http import HttpForbiddenError, record_json
from lararium.modules.finance.trading212.client import (
    PIES_REMOVED,
    HistoryKind,
    HistoryNotEndedError,
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
_MISSING = f"{FLAGGED_PREFIX}missing permission "
# The flags about the pies, which are fetched only on metadata days.
_PIE_FLAGS = frozenset({f"{_MISSING}pies:read", f"{FLAGGED_PREFIX}{PIES_REMOVED}"})


class Trading212SyncError(DisplayableError):
    """The sync cannot go on; the message names what to fix, never a value."""


class AccountMismatchError(Trading212SyncError, CredentialRejectedError):
    """The key's account is not the connection's: the owner has to enter the right key."""


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


def _account_id(summary: Item) -> str:
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


def _own(warning: str) -> bool:
    """A flag the sync raises. Other `Trading 212 flagged:` warnings (the normalizer's) are
    left to their writer."""
    return warning.startswith(_MISSING) or warning in _PIE_FLAGS


async def publish_flags(ctx: SyncContext, skipped: Iterable[str], pies_checked: bool) -> None:
    """Replace the sync's own flags with this run's, keeping every other warning; the pies'
    flags stand on a day the pies were not fetched. The owner is told, in one message, of each
    flag not among the previous warnings; a flag whose cause is gone clears without a word."""
    connection = ctx.connection
    previous = connection.warnings
    flags = [FLAGGED_PREFIX + name for name in sorted(set(skipped))]
    if not pies_checked:
        flags += [w for w in previous if w in _PIE_FLAGS and w not in flags]
    warnings = [w for w in previous if not _own(w)] + flags
    new = [flag.removeprefix(FLAGGED_PREFIX) for flag in flags if flag not in previous]
    if new:
        try:
            await ctx.notifier.send(f"{connection.display_name}: {FLAGGED_PREFIX}{', '.join(new)}")
        except Exception as error:
            # The class only: the message could carry the notifier's webhook URL.
            logger.warning("sending a Trading 212 notification failed (%s)", type(error).__name__)
    if warnings != previous:
        await set_warnings(ctx.command, connection.id, warnings)


class _Run:
    """One sync: the client, and the raw records it has stored."""

    def __init__(self, ctx: SyncContext, client: Trading212Client) -> None:
        self.ctx = ctx
        self.client = client
        self.ids: list[UUID] = []
        self.pies_checked = False

    async def store(
        self,
        endpoint: str,
        item: Item,
        request_path: str | None = None,
        params: Mapping[str, str] | None = None,
    ) -> None:
        request: dict[str, JSONValue] = {
            "endpoint": request_path or endpoint_path(endpoint),
            "params": dict(params or {}),
        }
        self.ids.append(
            await store_raw_record(
                self.ctx.command,
                connection_id=self.ctx.connection.id,
                record_type=ENDPOINTS[endpoint].record_type,
                request=request,
                content=record_json(item),
                connector_version=VERSION,
                sync_id=self.ctx.sync_id,
            )
        )

    async def _pages(
        self, kind: HistoryKind, seen: _Seen | None, start: str | None
    ) -> tuple[str | None, str | None]:
        """Store a walk's pages; (the newest item's key, the page to resume from when the
        walk reached the cap)."""
        newest: str | None = None
        try:
            async for page in self.client.history(kind, seen or _never, start=start):
                for item in page.items:
                    newest = newest or _key(kind, item)
                    await self.store(kind, item, params=page.params)
        except HistoryNotEndedError as capped:
            return newest, capped.resume
        return newest, None

    async def walk(
        self, kind: HistoryKind, marker: str | None, resume: str | None
    ) -> tuple[str | None, str | None]:
        """Walk a history newest first, stopping at `marker` (None: to the end), then on from
        `resume` when the walk stopped there; a walk to the end has passed what `resume`
        points to. (The newest key, the resume left.)"""
        seen = _Seen(kind, marker)
        newest, capped = await self._pages(kind, seen, None)
        if capped is None and seen.reached and resume is not None:
            _, capped = await self._pages(kind, None, resume)
        return newest, capped

    async def metadata(self) -> None:
        self.pies_checked = True
        for item in await self.client.instruments():
            await self.store("instruments", item)
        for item in await self.client.exchanges():
            await self.store("exchanges", item)
        for pie in await self.client.pies() or []:
            await self.store("pies", pie)
            pie_id = pie.get("id")
            if not isinstance(pie_id, Decimal):
                raise Trading212SyncError("Trading 212's pies answer has no pie id")
            try:
                detail = await self.client.pie(pie_id)
            except ValueError:
                raise Trading212SyncError("Trading 212's pies answer has no pie id") from None
            if detail is not None:
                await self.store("pie", detail, f"{endpoint_path('pie')}/{int(pie_id)}")


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
        try:
            cursor = await self._sync(run, datetime.now(UTC))
        except HttpForbiddenError as error:
            if error.cloudflare_block or error.scope is None:
                raise
            # The scope is a plain token (readonly_http keeps no other text of the answer).
            raise Trading212SyncError(
                f"Trading 212 key lacks the {error.scope} permission"
            ) from None
        await normalize(ctx, list(dict.fromkeys(run.ids)))
        await publish_flags(ctx, run.client.skipped, run.pies_checked)
        return cursor

    async def _sync(self, run: _Run, now: datetime) -> JSONValue:
        before = run.ctx.connection.cursor
        cursor: Mapping[str, JSONValue] = before if isinstance(before, dict) else {}
        client = run.client
        summary = await client.summary()
        account_id = _account_id(summary)
        known = cursor.get("account_id")
        if isinstance(known, str) and known != account_id:
            raise AccountMismatchError("this key belongs to another Trading 212 account")
        await run.store("summary", summary)
        for item in await client.positions():
            await run.store("positions", item)
        for item in await client.pending_orders() or []:
            await run.store("pending_orders", item)

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
