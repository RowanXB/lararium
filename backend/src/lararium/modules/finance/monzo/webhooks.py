"""Monzo webhooks: registering ours on each sync (Decision 12), and turning the deliveries
lararium.webhook_in stored in core.webhook_inbox into syncs.

The registered URL carries the webhook secret, so neither it nor Monzo's webhook listings
are logged, stored as raw records or put into a warning. A delivery only says which account
changed: its body triggers a sync and never reaches the ledger.
"""

from collections.abc import Awaitable, Callable, Iterable
from typing import Literal
from uuid import UUID

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from lararium.core.actors import Actor
from lararium.core.commands import command_context
from lararium.core.connections import list_connections
from lararium.core.json_types import JSONValue
from lararium.core.models import WebhookInboxEntry
from lararium.core.secrets import Secret
from lararium.modules.finance.monzo.client import MonzoClient
from lararium.modules.finance.monzo.normalize import ACCOUNT_MAP_KEY

SOURCE = "monzo"
# Deliveries claimed per run; the rest wait for the next minute.
BATCH_SIZE = 100

Outcome = Literal["queued", "duplicate", "unknown_account", "bad_body"]


def webhook_url(base_url: str, secret: Secret) -> str:
    return f"{base_url}/monzo/{secret.reveal()}"


async def ensure_webhooks(
    client: MonzoClient, account_ids: Iterable[str], base_url: str, secret: Secret
) -> None:
    """Make our current URL the one webhook under `base_url` for each account.

    Deletes webhooks under `base_url` with any other URL (an old secret, a duplicate) and
    registers ours where it is missing. Webhooks elsewhere are someone else's: left alone.
    """
    url = webhook_url(base_url, secret)
    ours = f"{base_url}/"
    for account_id in account_ids:
        registered = False
        for hook in await client.webhooks(account_id):
            hook_url = hook.get("url")
            if hook_url == url and not registered:
                registered = True
            elif isinstance(hook_url, str) and hook_url.startswith(ours):
                await client.delete_webhook(str(hook["id"]))
        if not registered:
            await client.register_webhook(account_id, url)


def _account_id(body: JSONValue) -> str | None:
    data = body.get("data") if isinstance(body, dict) else None
    account_id = data.get("account_id") if isinstance(data, dict) else None
    return account_id if isinstance(account_id, str) else None


async def _connections_by_account(session: AsyncSession) -> dict[str, list[UUID]]:
    """Each Monzo account (and pot) id mapped to the connections that sync it."""
    by_account: dict[str, list[UUID]] = {}
    for connection in await list_connections(session):
        account_map = connection.config.get(ACCOUNT_MAP_KEY)
        if connection.connector != SOURCE or not isinstance(account_map, dict):
            continue
        for monzo_id in account_map:
            by_account.setdefault(monzo_id, []).append(connection.id)
    return by_account


async def process_inbox(
    session: AsyncSession,
    *,
    defer: Callable[[UUID], Awaitable[bool]],
    batch_size: int = BATCH_SIZE,
) -> int:
    """Queue a sync for each unprocessed Monzo delivery's connection; return how many
    deliveries were processed.

    Rows are claimed with FOR UPDATE SKIP LOCKED, so concurrent runs never take the same
    delivery. `defer` returns False when that connection's sync is already queued, which
    marks the delivery `duplicate`. If anything fails, the whole batch is left for the next
    run; a sync queued twice that way is harmless.
    """
    inbox = WebhookInboxEntry
    claimed = (
        await session.execute(
            select(inbox.id, inbox.body)
            .where(inbox.source == SOURCE, inbox.processed_at.is_(None))
            .order_by(inbox.received_at, inbox.id)
            .limit(batch_size)
            .with_for_update(skip_locked=True)
        )
    ).all()
    if not claimed:
        # Nothing to do, so nothing to audit.
        await session.rollback()
        return 0

    connections = await _connections_by_account(session)
    actor = Actor("connector", SOURCE)
    async with command_context(
        session, actor, "webhook_inbox.process", {"deliveries": len(claimed)}
    ) as ctx:
        for delivery_id, body in claimed:
            outcome: Outcome
            account_id = _account_id(body)
            if account_id is None:
                outcome = "bad_body"
            elif account_id not in connections:
                outcome = "unknown_account"
            else:
                queued = [await defer(connection_id) for connection_id in connections[account_id]]
                outcome = "queued" if any(queued) else "duplicate"
            await session.execute(
                update(inbox)
                .where(inbox.id == delivery_id)
                .values(processed_at=func.now(), outcome=outcome)
            )
            ctx.record_change(f"webhook_inbox:{delivery_id}", {"outcome": outcome})
    return len(claimed)
