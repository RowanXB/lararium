"""Running the Binance connector against `FakeBinance`: a connection with its key, a clock that
only sleeping moves, and the sync run through the worker as it runs in production."""

import base64
import json
import uuid
from collections.abc import Callable, Mapping
from typing import Any

import httpx
from pydantic import SecretStr
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from lararium.config import Settings
from lararium.core.actors import Actor
from lararium.core.commands import CommandContext
from lararium.core.connections import (
    ConnectionRecord,
    create_connection,
    get_connection,
    store_credential,
)
from lararium.core.connectors import SyncContext
from lararium.core.json_types import JSONValue
from lararium.core.secrets import Secret
from lararium.modules.finance.binance.catalogue import ENDPOINTS
from lararium.modules.finance.binance.client import BinanceClient, HmacSigner
from lararium.modules.finance.binance.fetch import CURSORS_KEY
from lararium.modules.finance.binance.sync import HISTORY_LABELS
from lararium.worker import jobs

from .fixtures import NOW_MS, build_response

KEY = bytes(range(32))
API_KEY = "ak-TEST-do-not-log-0123"
API_SECRET = "as-TEST-do-not-log-4567"
CONNECTOR = Actor("connector", "binance")
SETTINGS = Settings(
    database_url="postgresql+psycopg://x@localhost/x",
    auth_mode="dev",
    master_key=SecretStr(base64.b64encode(KEY).decode()),
)
MINUTE_MS = 60_000
# The account the fake's spot-account answers for.
_uid = build_response("spot-account", [])
assert isinstance(_uid, dict)
UID = _uid["uid"]


class Clock:
    """Epoch milliseconds that only sleeping (or a test) moves."""

    def __init__(self, ms: int = NOW_MS) -> None:
        self.ms = ms

    def __call__(self) -> int:
        return self.ms

    async def sleep(self, ms: int) -> None:
        self.ms += ms


class RecordingNotifier:
    def __init__(self) -> None:
        self.messages: list[str] = []

    async def send(self, text: str) -> None:
        self.messages.append(text)


def complete_cursors(fetched_to: int) -> dict[str, JSONValue]:
    """Cursors of a connection whose backfill is done, every history label fetched to then."""
    state: JSONValue = {"fetched_to": fetched_to, "backfilled_to": 0, "complete": True}
    cursors: dict[str, JSONValue] = {}
    for label in HISTORY_LABELS:
        variants = ENDPOINTS[label].variants
        keys = [f"{label}:{v}" for _, values in variants for v in values] or [label]
        cursors.update(dict.fromkeys(keys, state))
    return cursors


async def binance_connection(
    run,
    *,
    config: Mapping[str, JSONValue] | None = None,
    cursor: JSONValue = None,
    status: str = "healthy",
) -> ConnectionRecord:
    async def setup(ctx: CommandContext) -> ConnectionRecord:
        connection = await create_connection(
            ctx,
            connector="binance",
            display_name="Binance",
            config={"key_type": "hmac", "uid": UID, **(config or {})},
        )
        await store_credential(ctx, KEY, connection.id, "api_key", API_KEY, None)
        await store_credential(ctx, KEY, connection.id, "api_secret", API_SECRET, None)
        await ctx.session.execute(
            text(
                "update core.connections set status = :s, cursor = cast(:c as jsonb) where id = :id"
            ),
            {"s": status, "c": _json(cursor), "id": connection.id},
        )
        return connection

    return await run(setup)


def _json(value: JSONValue) -> str:
    return json.dumps(value)


def later_config(fetched_to: int = NOW_MS - 3_600_000) -> dict[str, JSONValue]:
    """The config of a connection whose first sync is done."""
    return {CURSORS_KEY: complete_cursors(fetched_to), "first_sync_done": True}


def client_for(clock: Clock, http: httpx.AsyncClient) -> BinanceClient:
    return BinanceClient(
        http, Secret(API_KEY), HmacSigner(Secret(API_SECRET)), clock=clock, sleep=clock.sleep
    )


async def context(
    session: AsyncSession, connection_id: uuid.UUID, http: httpx.AsyncClient
) -> SyncContext:
    return SyncContext(
        command=CommandContext(session, CONNECTOR, uuid.uuid4()),
        connection=await get_connection(session, connection_id),
        sync_id=uuid.uuid4(),
        master_key=KEY,
        http=http,
        notifier=RecordingNotifier(),
        session_factory=async_sessionmaker(bind=session.bind),
    )


async def run_binance_sync(
    session: AsyncSession,
    connection_id: uuid.UUID,
    transport: httpx.MockTransport | Callable[[httpx.Request], httpx.Response],
    *,
    notifier: Any = None,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
    settings: Settings = SETTINGS,
) -> ConnectionRecord:
    """One sync through the worker's run_sync; the connection after it."""
    if not isinstance(transport, httpx.MockTransport):
        transport = httpx.MockTransport(transport)
    async with httpx.AsyncClient(transport=transport) as http:
        await jobs.run_sync(
            session,
            connection_id,
            settings=settings,
            http=http,
            notifier=notifier or RecordingNotifier(),
            session_factory=session_factory or async_sessionmaker(bind=session.bind),
        )
    return await get_connection(session, connection_id)
