"""IBKR Flex sync: fetch each window's statement into a document and one raw record per row,
then normalise (normalize.py). Rows are stored as IBKR sent them, personal fields included;
nothing here logs or reports a row's values.
"""

import logging
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Any, cast
from uuid import UUID
from zoneinfo import ZoneInfo

from lararium.core.commands import command_context
from lararium.core.connections import load_credential, update_config
from lararium.core.connectors import Capability, SyncContext
from lararium.core.documents import store_document
from lararium.core.errors import CredentialRejectedError
from lararium.core.json_types import JSONValue
from lararium.core.raw_records import store_raw_record
from lararium.modules.finance.ibkr.client import FlexClient
from lararium.modules.finance.ibkr.fields import SECTIONS
from lararium.modules.finance.ibkr.normalize import normalize
from lararium.modules.finance.ibkr.parser import FlexDataError, Statement, parse_statement

logger = logging.getLogger(__name__)

VERSION = "ibkr-sync-1"
DOCUMENT_SOURCE = "finance.ibkr"
TOKEN_PURPOSE = "flex_token"
EXPIRY_NOTIFIED_KEY = "expiry_notified_for"
LONDON = ZoneInfo("Europe/London")
# One request covers at most a year.
WINDOW_DAYS = 365
# Re-fetched on every sync: late corrections and statements IBKR finished after the last one.
OVERLAP_DAYS = 6
# The Flex Web Service serves about this many years back.
HISTORY_YEARS = 4
# The owner is told this long before the token expires.
REMIND_DAYS = 14


@dataclass(frozen=True)
class IngestResult:
    raw_record_ids: list[UUID]
    # The parser's flags ("<Section>.<attribute>"), for the normalizer and the upload route.
    flagged: frozenset[str]
    # (accountId, section) of every section the statement lists (Statement.sections).
    sections: frozenset[tuple[str, str]] = frozenset()


def _cursor_text(cursor: JSONValue, key: str) -> str | None:
    value = cursor.get(key) if isinstance(cursor, dict) else None
    return value if isinstance(value, str) else None


def _cursor_date(cursor: JSONValue, key: str) -> date | None:
    value = _cursor_text(cursor, key)
    return date.fromisoformat(value) if value else None


def due(cursor: JSONValue, now: datetime) -> bool:
    """Whether a sync should fetch: none has run since the latest 06:00 London at or before now."""
    fetched_at = _cursor_text(cursor, "fetched_at")
    if fetched_at is None:
        return True
    local = now.astimezone(LONDON)
    day = local.date() if local.time() >= time(6) else local.date() - timedelta(days=1)
    boundary = datetime.combine(day, time(6), tzinfo=LONDON)
    return datetime.fromisoformat(fetched_at) < boundary


def windows(cursor: JSONValue, today: date, opened: date | None) -> list[tuple[date, date]]:
    """The date ranges to fetch, oldest first. `today` is the London date.

    The first sync fetches the last year and, for an account opened before it, earlier years
    back to its opening (at most HISTORY_YEARS); later ones repeat the last OVERLAP_DAYS + 1.
    """
    yesterday = today - timedelta(days=1)
    span = timedelta(days=WINDOW_DAYS - 1)
    fetched_to = _cursor_date(cursor, "fetched_to")
    pieces: list[tuple[date, date]] = []
    if fetched_to is not None:
        start = fetched_to - timedelta(days=OVERLAP_DAYS)
        while start <= yesterday:
            end = min(start + span, yesterday)
            pieces.append((start, end))
            start = end + timedelta(days=1)
        return pieces
    start = yesterday - span
    pieces.append((start, yesterday))
    floor = max(opened, date(today.year - HISTORY_YEARS, 1, 1)) if opened else None
    while floor is not None and start > floor:
        end = start - timedelta(days=1)
        start = max(end - span, floor)
        pieces.append((start, end))
    return pieces[::-1]


def _filename(request: dict[str, JSONValue]) -> str:
    upload = request.get("upload")
    if isinstance(upload, str):
        return upload
    return f"ibkr-flex-{request.get('fd')}-{request.get('td')}.xml"


async def _ingest(
    ctx: SyncContext, body: bytes, request: dict[str, JSONValue]
) -> tuple[IngestResult, Statement]:
    # Parsed first: a statement that is refused leaves no file behind.
    statement = parse_statement(body)
    if ctx.documents is None:
        raise RuntimeError("no document store in the sync context")
    document = await store_document(
        ctx.command,
        ctx.documents,
        body,
        filename=_filename(request),
        media_type="application/xml",
        source=DOCUMENT_SOURCE,
    )
    stored_request: dict[str, JSONValue] = {**request, "document_sha256": document.sha256}
    ids: list[UUID] = []
    for row in statement.rows:
        content: JSONValue = {
            "account": row.account,
            "period": {"from": row.period_from.isoformat(), "to": row.period_to.isoformat()},
            "attributes": dict(row.attributes),
        }
        ids.append(
            await store_raw_record(
                ctx.command,
                connection_id=ctx.connection.id,
                record_type=SECTIONS[row.section].record_type,
                request=stored_request,
                content=content,
                connector_version=VERSION,
                sync_id=ctx.sync_id,
            )
        )
    # The same row can occur more than once (a conversion rate repeats for every account).
    result = IngestResult(list(dict.fromkeys(ids)), statement.flagged, statement.sections)
    return result, statement


async def ingest_statement(
    ctx: SyncContext, body: bytes, request: dict[str, JSONValue]
) -> IngestResult:
    """Store a statement as a document and one raw record per row it holds.

    Used for a statement fetched from the Flex Web Service and for an uploaded one alike.
    """
    result, _ = await _ingest(ctx, body, request)
    return result


def _opened(statement: Statement) -> date | None:
    """The earliest dateOpened among the statement's accounts."""
    dates: list[date] = []
    for row in statement.rows:
        if row.section != "AccountInformation":
            continue
        try:
            dates.append(date.fromisoformat(row.attributes["dateOpened"]))
        except (KeyError, ValueError):
            # The field only: its value is the owner's data.
            raise FlexDataError("AccountInformation.dateOpened is not a date") from None
    return min(dates, default=None)


async def notify_expiry(ctx: SyncContext) -> None:
    """Tell the owner once that the token is about to expire.

    The reminder is remembered by the expiry it was sent for, so a new token (a new expiry)
    is reminded in its turn; the mark is committed on its own, so a sync that fails later
    does not repeat the reminder.
    """
    connection = ctx.connection
    expires_at = connection.credential_expires_at
    if expires_at is None:
        return
    marker = expires_at.astimezone(UTC).isoformat()
    if connection.config.get(EXPIRY_NOTIFIED_KEY) == marker:
        return
    now = datetime.now(UTC)
    if expires_at - now > timedelta(days=REMIND_DAYS):
        return
    day = expires_at.astimezone(LONDON).date().isoformat()
    verb = "expires" if expires_at > now else "expired"
    try:
        await ctx.notifier.send(
            f"{connection.display_name}: the IBKR Flex token {verb} on {day}."
            " Create a new token in Client Portal and update the connection."
        )
    except Exception:
        logger.warning("sending the IBKR token reminder failed", exc_info=True)
        return
    args = {"connection_id": str(connection.id)}
    async with (
        ctx.session_factory() as session,
        command_context(session, ctx.command.actor, "connection.mark_expiry_notified", args) as cmd,
    ):
        await update_config(cmd, connection.id, {EXPIRY_NOTIFIED_KEY: marker})


class IbkrConnector:
    name = "ibkr"
    version = VERSION
    capabilities: frozenset[Capability] = frozenset({"balances", "holdings", "transactions"})

    async def check_access(self, ctx: SyncContext) -> bool:
        # There is no approval step: the token works as soon as it is created.
        return True

    async def sync(self, ctx: SyncContext) -> JSONValue:
        await notify_expiry(ctx)
        connection = ctx.connection
        now = datetime.now(UTC)
        if not due(connection.cursor, now):
            return connection.cursor
        token = await load_credential(
            ctx.command.session, ctx.master_key, connection.id, TOKEN_PURPOSE
        )
        query_id = connection.config.get("query_id")
        if token is None or not isinstance(query_id, str) or not query_id:
            raise CredentialRejectedError("The IBKR Flex token or query id is missing")
        client = FlexClient(ctx.http, token)
        today = now.astimezone(LONDON).date()
        ids: list[UUID] = []
        flagged: set[str] = set()
        sections: set[tuple[str, str]] = set()
        fetched: list[tuple[date, date]] = []

        async def fetch(window: tuple[date, date]) -> Statement:
            fd, td = window
            body = await client.fetch(query_id, fd, td)
            request: dict[str, JSONValue] = {
                "query_id": query_id,
                "fd": fd.isoformat(),
                "td": td.isoformat(),
            }
            result, statement = await _ingest(ctx, body, request)
            ids.extend(result.raw_record_ids)
            flagged.update(result.flagged)
            sections.update(result.sections)
            fetched.append(window)
            return statement

        statements = [await fetch(window) for window in windows(connection.cursor, today, None)]
        if _cursor_date(connection.cursor, "fetched_to") is None:
            # A first sync: the latest year is fetched first, as it says how far back to go.
            opened = _opened(statements[-1]) if statements else None
            # All of this is one transaction: if an older window fails, every raw record of
            # this sync rolls back and the retry fetches every window again. That cost buys
            # atomicity. Documents written meanwhile are content-addressed, so the retry
            # reuses any orphaned file rather than duplicating it.
            for window in windows(connection.cursor, today, opened)[:-1]:
                await fetch(window)
        await normalize(ctx, ids, frozenset(flagged), frozenset(sections))
        history_from = min(
            [w[0] for w in fetched]
            + [d for d in [_cursor_date(connection.cursor, "history_from")] if d]
        )
        cursor: dict[str, Any] = {
            "history_from": history_from.isoformat(),
            "fetched_to": (today - timedelta(days=1)).isoformat(),
            "fetched_at": now.isoformat(),
        }
        return cast(JSONValue, cursor)
