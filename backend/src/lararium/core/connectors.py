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

Capability = Literal[
    "balances", "holdings", "transactions", "webhook", "device_push", "outbound_actions"
]


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


class Connector(Protocol):
    name: str
    version: str
    capabilities: frozenset[Capability]

    async def sync(self, ctx: SyncContext) -> JSONValue:
        """Fetch and store new data; return the connection's new cursor."""
        ...
