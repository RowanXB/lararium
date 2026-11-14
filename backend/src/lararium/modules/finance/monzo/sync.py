"""Monzo sync: fetch accounts, balances, pots and transactions into raw records, then
normalise what was fetched into the ledger (normalize.py) in the same transaction.
"""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from lararium.core.connections import get_connection, set_warnings
from lararium.core.connectors import BACKFILL_FAILED, Capability, SyncContext
from lararium.core.errors import CredentialRejectedError
from lararium.core.json_types import JSONValue
from lararium.core.raw_records import store_raw_record
from lararium.modules.finance.monzo.auth import access_token_for
from lararium.modules.finance.monzo.client import (
    EXPAND,
    PAGE_SIZE,
    AccessTokenExpiredError,
    MonzoApiError,
    MonzoClient,
    NotYetApprovedError,
)
from lararium.modules.finance.monzo.normalize import FetchedRecord, normalize, oldest_pending
from lararium.modules.finance.monzo.webhooks import ensure_webhooks

VERSION = "monzo-sync-1"
# Re-fetched on every incremental sync: pending transactions settle or change after creation.
OVERLAP = timedelta(days=14)
# Outside the approval window Monzo serves 90 days; stay one day inside it.
MAX_LOOKBACK = timedelta(days=89)
TRUNCATION_GRACE = timedelta(days=7)
# Replaced on every sync: the warning is current until registration next succeeds.
WEBHOOK_WARNING = "Monzo webhooks could not be updated"
FETCH_ATTEMPTS = 3


class BalanceKeptChangingError(Exception):
    """An account's balance changed during each attempt to fetch its transactions."""


def _parse(value: str) -> datetime:
    return datetime.fromisoformat(value).astimezone(UTC)


def _format(value: datetime) -> str:
    value = value.astimezone(UTC)
    return f"{value:%Y-%m-%dT%H:%M:%S}.{value.microsecond // 1000:03d}Z"


def _truncated(account_id: str) -> str:
    return f"Monzo account {account_id}: history may be truncated"


def _clamped(account_id: str) -> str:
    return f"Monzo account {account_id}: sync window limited to the last 89 days"


@dataclass(frozen=True)
class _AccountState:
    """What the cursor remembers of one account."""

    last_created: str | None
    # When the last sync fetched it; None in cursors written before this was recorded.
    fetched_at: str | None
    # Its full history was fetched.
    backfilled: bool
    # It has its opening balance adjustment, so it is checked against Monzo's balance.
    reconciled: bool


def _account_state(cursor: JSONValue, account_id: str) -> _AccountState | None:
    """The cursor's state for one account; None if no sync has seen it."""
    if not isinstance(cursor, dict):
        return None
    accounts = cursor.get("accounts")
    state = accounts.get(account_id) if isinstance(accounts, dict) else None
    if not isinstance(state, dict):
        return None
    last_created = state.get("last_created")
    fetched_at = state.get("fetched_at")
    backfilled = state.get("backfilled") is True
    return _AccountState(
        last_created=last_created if isinstance(last_created, str) else None,
        fetched_at=fetched_at if isinstance(fetched_at, str) else None,
        backfilled=backfilled,
        # A backfilled account got its opening with the backfill.
        reconciled=state.get("reconciled") is True or backfilled,
    )


async def _authorised(ctx: SyncContext) -> tuple[MonzoClient, list[dict[str, Any]]]:
    """A client with a token Monzo accepts, and the accounts it serves.

    The stored expiry is only a hint: Monzo can revoke a token before it (the owner removing
    the access in the app, say) and answer 401. Then the token is refreshed regardless, once;
    a refresh Monzo rejects raises CredentialRejectedError, which asks for authorising again.
    """
    client = MonzoClient(ctx.http, await access_token_for(ctx))
    try:
        return client, await client.accounts()
    except AccessTokenExpiredError:
        client = MonzoClient(ctx.http, await access_token_for(ctx, force_refresh=True))
    try:
        return client, await client.accounts()
    except AccessTokenExpiredError as exc:
        raise CredentialRejectedError(
            "Monzo rejected a freshly refreshed access token; authorise again"
        ) from exc


class MonzoConnector:
    name = "monzo"
    version = VERSION
    capabilities: frozenset[Capability] = frozenset({"balances", "transactions", "webhook"})

    async def check_access(self, ctx: SyncContext) -> bool:
        # Monzo answers 403 until the owner approves the new token in the app (SCA).
        try:
            await _authorised(ctx)
        except NotYetApprovedError:
            return False
        return True

    async def sync(self, ctx: SyncContext) -> JSONValue:
        # First, before anything locks the connection row: it may commit rotated tokens in a
        # transaction of its own.
        client, accounts = await _authorised(ctx)
        connection = ctx.connection
        session = ctx.command.session
        fetched: list[FetchedRecord] = []
        full_backfill = ctx.full_backfill

        async def store(
            record_type: str,
            request: dict[str, JSONValue],
            content: list[dict[str, Any]] | dict[str, Any],
        ) -> None:
            record_id = await store_raw_record(
                ctx.command,
                connection_id=connection.id,
                record_type=record_type,
                request=request,
                content=cast(JSONValue, content),
                connector_version=VERSION,
                sync_id=ctx.sync_id,
            )
            # Called right after each response arrives, so this is its fetch time.
            fetched.append(FetchedRecord(record_id, request, datetime.now(UTC)))

        warnings: list[str] = []
        await store("accounts", {}, accounts)
        if ctx.webhook is not None:
            # Not stored as raw records: the registered URLs carry the secret.
            open_accounts = [a["id"] for a in accounts if not a.get("closed")]
            try:
                await ensure_webhooks(
                    client, open_accounts, ctx.webhook.base_url, ctx.webhook.secret
                )
            except Exception as exc:
                # Best effort: without webhooks, syncs still run on the schedule. A
                # MonzoApiError names only the status and Monzo's code; any other error only
                # its type, as its message may quote the URL.
                reason = str(exc) if isinstance(exc, MonzoApiError) else type(exc).__name__
                warnings.append(f"{WEBHOOK_WARNING} ({reason}); syncing on the schedule only")
        # Accounts missing from this response keep their state, in case they come back.
        cursor = connection.cursor
        previous = cursor.get("accounts") if isinstance(cursor, dict) else None
        cursor_accounts: dict[str, JSONValue] = dict(previous) if isinstance(previous, dict) else {}
        reconciled_accounts: list[str] = []
        openings: list[str] = []

        for account in accounts:
            account_id: str = account["id"]
            state = _account_state(connection.cursor, account_id)
            last_created = state.last_created if state else None
            backfilled = state.backfilled if state else False
            reconciled = state.reconciled if state else False

            since: str | None = None
            if not full_backfill:
                floor = datetime.now(UTC) - MAX_LOOKBACK
                # From 14 days before the newest transaction seen, so recent ones that settle
                # or change are fetched again, and from the oldest one the ledger still holds
                # as pending, however old, so it is fetched until it settles or is voided.
                pending = await oldest_pending(
                    session, connection.id, connection.config, account_id
                )
                starts: list[datetime] = [_parse(last_created) - OVERLAP] if last_created else []
                if pending is not None:
                    # A second early, in case `since` excludes the instant it names.
                    starts.append(pending - timedelta(seconds=1))
                wanted = min(starts, default=floor)
                since = _format(max(wanted, floor))
                # Something is lost only if what is needed is older than Monzo serves: the
                # transactions since the last fetch (since its creation, for an account never
                # fetched), or a pending one. A quiet account whose newest transaction is old
                # has lost nothing.
                last_fetched = (
                    (state.fetched_at or state.last_created) if state else account["created"]
                )
                needed = [d for d in (_parse(last_fetched) if last_fetched else None, pending) if d]
                if any(d < floor for d in needed):
                    warnings.append(_clamped(account_id))

            # The balance must describe the same moment as the transactions, or a payment
            # arriving between the two requests would be absorbed into the opening adjustment
            # (or show as a mismatch). It is read again after the transactions, and the
            # attempt is kept only if it didn't change.
            for _ in range(FETCH_ATTEMPTS):
                attempt_start = len(fetched)
                fetched_at = datetime.now(UTC)
                balance = await client.balance(account_id)
                await store("balance", {"account_id": account_id}, balance)
                pots = await client.pots(account_id)
                await store("pots", {"current_account_id": account_id}, pots)
                earliest: str | None = None
                newest = last_created
                page_since = since
                async for page in client.transactions(account_id, since=since):
                    page_request: dict[str, JSONValue] = {
                        "account_id": account_id,
                        "since": page_since,
                        "limit": str(PAGE_SIZE),
                        "expand[]": EXPAND,
                    }
                    await store("transactions_page", page_request, page)
                    page_since = page[-1]["id"]
                    created = [t["created"] for t in page]
                    earliest = min(created + ([earliest] if earliest else []), key=_parse)
                    newest = max(created + ([newest] if newest else []), key=_parse)
                if await client.balance(account_id) == balance:
                    break
                # Stored all the same, as fetched; only the consistent attempt is normalised.
                del fetched[attempt_start:]
            else:
                raise BalanceKeptChangingError(account_id)

            if full_backfill:
                backfilled = True
                if earliest and _parse(earliest) - _parse(account["created"]) > TRUNCATION_GRACE:
                    warnings.append(_truncated(account_id))
            # The opening adjustment is recomputed after every full backfill, which may have
            # fetched older history, and made for an account first seen outside one (a joint
            # account opened later, say). From then on the account is reconciled.
            if full_backfill or state is None:
                openings.append(account_id)
                reconciled = True
            elif reconciled:
                reconciled_accounts.append(account_id)

            cursor_accounts[account_id] = {
                "last_created": newest or account["created"],
                "fetched_at": _format(fetched_at),
                "backfilled": backfilled,
                "reconciled": reconciled,
            }

        current = await get_connection(session, connection.id)
        kept = [w for w in current.warnings if not w.startswith(WEBHOOK_WARNING)]
        if full_backfill:
            # What a completed backfill settles; a truncation that still applies is in
            # `warnings` again.
            settled = {BACKFILL_FAILED} | {
                w for a in accounts for w in (_truncated(a["id"]), _clamped(a["id"]))
            }
            kept = [w for w in kept if w not in settled]
        merged = kept + [w for w in warnings if w not in kept]
        if merged != current.warnings:
            await set_warnings(ctx.command, connection.id, merged)
        await normalize(ctx, fetched, reconciled=reconciled_accounts, openings=openings)
        return {"accounts": cursor_accounts}
