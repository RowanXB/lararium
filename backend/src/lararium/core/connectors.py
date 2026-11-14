"""The connector interface: what a module implements to sync one kind of connection."""

from dataclasses import dataclass, field
from typing import Literal, Protocol
from uuid import UUID

import httpx
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from lararium.core.commands import CommandContext
from lararium.core.connections import ConnectionRecord
from lararium.core.json_types import JSONValue
from lararium.core.notify import Notifier
from lararium.core.secrets import Secret

# A connection's last_error when the full backfill after its approval failed; until it is
# authorised again, which repeats the backfill, its history is incomplete.
BACKFILL_FAILED = "Full history could not be fetched during approval; authorise again to retry"

Capability = Literal[
    "balances", "holdings", "transactions", "webhook", "device_push", "outbound_actions"
]


@dataclass(frozen=True)
class WebhookTarget:
    """Where providers deliver webhooks: the receiver's public base URL and the secret in its
    path. The secret is a credential: it never goes into logs, raw records or warnings."""

    base_url: str
    secret: Secret


@dataclass
class SyncContext:
    """Everything one sync run gets: its command, its connection, and the means to fetch."""

    command: CommandContext
    connection: ConnectionRecord
    sync_id: UUID
    master_key: bytes = field(repr=False)
    http: httpx.AsyncClient
    notifier: Notifier
    # For writes that must commit on their own, outside the sync's transaction (such as
    # rotated single-use tokens, which must survive the sync failing).
    session_factory: async_sessionmaker[AsyncSession] = field(repr=False)
    # Set when webhooks are configured; the connector then registers them with the provider.
    webhook: WebhookTarget | None = None
    # Fetch all history: only for the sync that runs right after the owner approves a new
    # authorisation, the one time the provider serves it (see run_await_approval).
    full_backfill: bool = False


class Connector(Protocol):
    name: str
    version: str
    capabilities: frozenset[Capability]

    async def sync(self, ctx: SyncContext) -> JSONValue:
        """Fetch and store new data; return the connection's new cursor."""
        ...

    async def check_access(self, ctx: SyncContext) -> bool:
        """Whether the provider serves data for the stored credentials yet.

        False while the owner still has to approve them with the provider; credentials the
        provider rejects raise CredentialRejectedError. It writes nothing through ctx.command.
        """
        ...
