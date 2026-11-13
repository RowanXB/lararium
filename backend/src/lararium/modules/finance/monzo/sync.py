"""Monzo sync: fetch accounts, balances, pots and transactions into raw records, then
normalise what was fetched into the ledger (normalize.py) in the same transaction.
"""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from lararium.core.connections import get_connection, set_warnings, update_config
from lararium.core.connectors import BACKFILL_FAILED, Capability, SyncContext
from lararium.core.json_types import JSONValue
from lararium.core.raw_records import store_raw_record
from lararium.modules.finance.monzo.auth import access_token_for
from lararium.modules.finance.monzo.client import (
    EXPAND,
    PAGE_SIZE,
    MonzoApiError,
    MonzoClient,
    NotYetApprovedError,
)
from lararium.modules.finance.monzo.normalize import FetchedRecord, normalize
from lararium.modules.finance.monzo.webhooks import ensure_webhooks

VERSION = "monzo-sync-1"
# Re-fetched on every incremental sync: pending transactions settle or change after creation.
OVERLAP = timedelta(days=14)
# Outside the approval window Monzo serves 90 days; stay one day inside it.
MAX_LOOKBACK = timedelta(days=89)
TRUNCATION_GRACE = timedelta(days=7)
# Replaced on every sync: the warning is current until registration next succeeds.
WEBHOOK_WARNING = "Monzo webhooks could not be updated"


def _parse(value: str) -> datetime:
    return datetime.fromisoformat(value).astimezone(UTC)


def _format(value: datetime) -> str:
    value = value.astimezone(UTC)
    return f"{value:%Y-%m-%dT%H:%M:%S}.{value.microsecond // 1000:03d}Z"


def _truncated(account_id: str) -> str:
    return f"Monzo account {account_id}: history may be truncated"


@dataclass(frozen=True)
class _AccountState:
    """What the cursor remembers of one account."""

    last_created: str | None
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
    backfilled = state.get("backfilled") is True
    return _AccountState(
        last_created=last_created if isinstance(last_created, str) else None,
        backfilled=backfilled,
        # A backfilled account got its opening with the backfill.
        reconciled=state.get("reconciled") is True or backfilled,
    )


class MonzoConnector:
    name = "monzo"
    version = VERSION
    capabilities: frozenset[Capability] = frozenset({"balances", "transactions", "webhook"})

    async def check_access(self, ctx: SyncContext) -> bool:
        # Monzo answers 403 until the owner approves the new token in the app (SCA).
        client = MonzoClient(ctx.http, await access_token_for(ctx))
        try:
            await client.accounts()
        except NotYetApprovedError:
            return False
        return True

    async def sync(self, ctx: SyncContext) -> JSONValue:
        # First, before anything locks the connection row: it may commit rotated tokens in a
        # transaction of its own.
        token = await access_token_for(ctx)
        client = MonzoClient(ctx.http, token)
        connection = ctx.connection
        fetched: list[FetchedRecord] = []
        full_backfill = bool(connection.config.get("backfill_requested"))

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
        accounts = await client.accounts()
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

            await store("balance", {"account_id": account_id}, await client.balance(account_id))
            await store("pots", {"current_account_id": account_id}, await client.pots(account_id))

            since: str | None = None
            if not full_backfill:
                floor = datetime.now(UTC) - MAX_LOOKBACK
                wanted = _parse(last_created) - OVERLAP if last_created else floor
                if wanted < floor:
                    warnings.append(
                        f"Monzo account {account_id}: sync window limited to the last 89 days"
                    )
                since = _format(max(wanted, floor))

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
                "backfilled": backfilled,
                "reconciled": reconciled,
            }

        # Only now, with every page fetched, is the backfill request done.
        current = await get_connection(ctx.command.session, connection.id)
        if full_backfill:
            await update_config(ctx.command, connection.id, remove=["backfill_requested"])
        kept = [w for w in current.warnings if not w.startswith(WEBHOOK_WARNING)]
        if full_backfill:
            # What a completed backfill settles; a truncation that still applies is in
            # `warnings` again.
            settled = {BACKFILL_FAILED} | {_truncated(a["id"]) for a in accounts}
            kept = [w for w in kept if w not in settled]
        merged = kept + [w for w in warnings if w not in kept]
        if merged != current.warnings:
            await set_warnings(ctx.command, connection.id, merged)
        await normalize(ctx, fetched, reconciled=reconciled_accounts, openings=openings)
        return {"accounts": cursor_accounts}
