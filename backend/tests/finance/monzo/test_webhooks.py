"""Monzo webhooks: registration on each sync, and turning inbox deliveries into syncs."""

import json
import uuid
from collections.abc import Iterator
from typing import Any
from urllib.parse import parse_qs

import httpx
import pytest
from procrastinate.testing import InMemoryConnector
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from lararium.core.commands import CommandContext
from lararium.core.connections import ConnectionRecord, get_connection
from lararium.core.connectors import SyncContext, WebhookTarget
from lararium.core.json_types import JSONValue
from lararium.core.notify import NullNotifier
from lararium.core.secrets import Secret
from lararium.modules.finance.monzo.client import MonzoClient
from lararium.modules.finance.monzo.sync import MonzoConnector
from lararium.modules.finance.monzo.webhooks import ensure_webhooks, process_inbox
from lararium.worker.app import defer_sync, procrastinate_app

from .fixtures import ACCOUNT_ID, make_account
from .test_sync import CONNECTOR, KEY, FakeMonzo, _connection

BASE = "https://hooks.lararium.example"
SECRET = "whsec-TEST-0123456789abcdef0123456789"
OUR_URL = f"{BASE}/monzo/{SECRET}"
OTHER_ACCOUNT = "acc_TEST2"


class WebhookMonzo(FakeMonzo):
    """FakeMonzo plus GET, POST and DELETE /webhooks over an in-memory list."""

    def __init__(self, hooks: list[dict[str, Any]] | None = None, **kwargs: Any) -> None:
        super().__init__([], **kwargs)
        self.hooks = list(hooks or [])
        self.webhook_status = 200

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if not path.startswith("/webhooks"):
            return super().__call__(request)
        self.requests.append(request)
        if self.webhook_status != 200:
            return httpx.Response(self.webhook_status, json={"code": "internal_service"})
        if request.method == "GET":
            account_id = request.url.params["account_id"]
            return httpx.Response(
                200, json={"webhooks": [h for h in self.hooks if h["account_id"] == account_id]}
            )
        if request.method == "POST":
            form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
            hook = {
                "id": f"webhook_TEST{len(self.requests)}",
                "account_id": form["account_id"],
                "url": form["url"],
            }
            self.hooks.append(hook)
            return httpx.Response(200, json={"webhook": hook})
        assert request.method == "DELETE", request.method
        hook_id = path.removeprefix("/webhooks/")
        assert hook_id in [h["id"] for h in self.hooks], hook_id
        self.hooks = [h for h in self.hooks if h["id"] != hook_id]
        return httpx.Response(200, json={})

    def webhook_requests(self) -> list[tuple[str, str]]:
        return [(r.method, r.url.path) for r in self.requests if r.url.path.startswith("/webhooks")]


def _hook(hook_id: str, url: str, account_id: str = ACCOUNT_ID) -> dict[str, Any]:
    return {"id": hook_id, "account_id": account_id, "url": url}


async def test_ensure_webhooks_replaces_stale_and_registers_missing() -> None:
    fake = WebhookMonzo(
        [
            _hook("webhook_STALE", f"{BASE}/monzo/old-secret"),
            _hook("webhook_FOREIGN", "https://elsewhere.example/monzo/hook"),
            # Shares the base as a string prefix, but isn't under it.
            _hook("webhook_LOOKALIKE", f"{BASE}.evil.example/monzo/x"),
            _hook("webhook_CURRENT", OUR_URL, OTHER_ACCOUNT),
            _hook("webhook_DUPLICATE", OUR_URL, OTHER_ACCOUNT),
        ]
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(fake)) as http:
        client = MonzoClient(http, Secret("at-TEST"))
        await ensure_webhooks(client, [ACCOUNT_ID, OTHER_ACCOUNT], BASE, Secret(SECRET))

    assert sorted((h["account_id"], h["url"]) for h in fake.hooks) == sorted(
        [
            (ACCOUNT_ID, "https://elsewhere.example/monzo/hook"),
            (ACCOUNT_ID, f"{BASE}.evil.example/monzo/x"),
            (ACCOUNT_ID, OUR_URL),
            (OTHER_ACCOUNT, OUR_URL),
        ]
    )
    assert {h["id"] for h in fake.hooks if h["account_id"] == OTHER_ACCOUNT} == {"webhook_CURRENT"}
    assert ("DELETE", "/webhooks/webhook_STALE") in fake.webhook_requests()
    # Already registered: nothing posted for the other account.
    posted = [parse_qs(r.content.decode()) for r in fake.requests if r.method == "POST"]
    assert posted == [{"account_id": [ACCOUNT_ID], "url": [OUR_URL]}]


async def _sync(
    session: AsyncSession, connection: ConnectionRecord, fake: FakeMonzo, *, webhook: bool = True
) -> JSONValue:
    async with httpx.AsyncClient(transport=httpx.MockTransport(fake)) as http:
        sync_ctx = SyncContext(
            command=CommandContext(session, CONNECTOR, uuid.uuid4()),
            connection=await get_connection(session, connection.id),
            sync_id=uuid.uuid4(),
            master_key=KEY,
            http=http,
            notifier=NullNotifier(),
            session_factory=async_sessionmaker(bind=session.bind),
            webhook=WebhookTarget(BASE, Secret(SECRET)) if webhook else None,
        )
        return await MonzoConnector().sync(sync_ctx)


async def _stored_text(session: AsyncSession) -> str:
    """Everything a sync may have written that could carry the secret, as text."""
    stored = await session.scalar(
        text(
            "select coalesce((select string_agg(request::text || content::text, ' ')"
            " from core.raw_records), '')"
            " || coalesce((select string_agg(args::text || changes::text, ' ')"
            " from core.audit_log), '')"
            " || coalesce((select string_agg(data::text, ' ') from core.events), '')"
            " || coalesce((select string_agg(config::text || warnings::text"
            " || coalesce(last_error, ''), ' ') from core.connections), '')"
        )
    )
    return stored or ""


async def test_sync_registers_webhooks_without_storing_the_secret(session, run) -> None:
    fake = WebhookMonzo()
    connection = await _connection(run)

    await _sync(session, connection, fake)

    assert [(h["account_id"], h["url"]) for h in fake.hooks] == [(ACCOUNT_ID, OUR_URL)]
    assert SECRET not in await _stored_text(session)
    assert (await get_connection(session, connection.id)).warnings == []


async def test_sync_skips_closed_accounts(session, run) -> None:
    fake = WebhookMonzo(account=make_account(closed=True))
    connection = await _connection(run)

    await _sync(session, connection, fake)

    assert fake.webhook_requests() == []


async def test_sync_without_webhook_settings_leaves_webhooks_alone(session, run) -> None:
    fake = WebhookMonzo()
    connection = await _connection(run)

    await _sync(session, connection, fake, webhook=False)

    assert fake.webhook_requests() == []


async def test_webhook_errors_warn_without_failing_the_sync(session, run) -> None:
    fake = WebhookMonzo()
    fake.webhook_status = 500
    connection = await _connection(run)

    cursor = await _sync(session, connection, fake)

    assert isinstance(cursor, dict) and ACCOUNT_ID in cursor["accounts"]
    warnings = (await get_connection(session, connection.id)).warnings
    assert len(warnings) == 1
    assert warnings[0].startswith("Monzo webhooks could not be updated")
    assert "500" in warnings[0]
    assert SECRET not in await _stored_text(session)

    # Once registration works again, the warning goes.
    fake.webhook_status = 200
    await _sync(session, connection, fake)
    assert (await get_connection(session, connection.id)).warnings == []
    assert [h["url"] for h in fake.hooks] == [OUR_URL]


@pytest.fixture
def queue() -> Iterator[Any]:
    with procrastinate_app.replace_connector(InMemoryConnector()) as app:
        yield app.connector


async def _deliver(session: AsyncSession, body: JSONValue, source: str = "monzo") -> None:
    await session.execute(
        text("insert into core.webhook_inbox (source, body) values (:s, cast(:b as jsonb))"),
        {"s": source, "b": json.dumps(body)},
    )


def _delivery(account_id: str = ACCOUNT_ID, **data: Any) -> dict[str, Any]:
    return {"type": "transaction.created", "data": {"account_id": account_id} | data}


async def _outcomes(session: AsyncSession) -> list[tuple[str, str | None, bool]]:
    rows = await session.execute(
        text(
            "select source, outcome, processed_at is not null from core.webhook_inbox"
            " order by received_at, id"
        )
    )
    return [tuple(r) for r in rows]  # pyright: ignore[reportReturnType]


async def _monzo_connection(run, account_map: dict[str, JSONValue]) -> ConnectionRecord:
    return await _connection(run, config={"account_map": account_map})


async def test_duplicate_deliveries_one_sync(session, run, queue) -> None:
    connection = await _monzo_connection(run, {ACCOUNT_ID: str(uuid.uuid4())})
    await _deliver(session, _delivery(id="tx_TEST0001"))
    await _deliver(session, _delivery(id="tx_TEST0002"))

    assert await process_inbox(session, defer=defer_sync) == 2

    jobs = list(queue.jobs.values())
    assert [(j["task_name"], j["args"]) for j in jobs] == [
        ("sync_connection", {"connection_id": str(connection.id)})
    ]
    assert jobs[0]["queueing_lock"] == f"sync:{connection.id}"
    assert await _outcomes(session) == [
        ("monzo", "queued", True),
        ("monzo", "duplicate", True),
    ]
    # Processed rows aren't claimed again.
    assert await process_inbox(session, defer=defer_sync) == 0


async def test_unknown_account_marked(session, run, queue) -> None:
    await _monzo_connection(run, {ACCOUNT_ID: str(uuid.uuid4())})
    await _deliver(session, _delivery("acc_UNKNOWN"))

    assert await process_inbox(session, defer=defer_sync) == 1

    assert queue.jobs == {}
    assert await _outcomes(session) == [("monzo", "unknown_account", True)]


@pytest.mark.parametrize(
    "body",
    [[1, 2], "text", {"type": "transaction.created"}, {"data": "x"}, {"data": {"account_id": 7}}],
)
async def test_bad_body_marked(session, run, queue, body: JSONValue) -> None:
    await _monzo_connection(run, {ACCOUNT_ID: str(uuid.uuid4())})
    await _deliver(session, body)

    assert await process_inbox(session, defer=defer_sync) == 1

    assert queue.jobs == {}
    assert await _outcomes(session) == [("monzo", "bad_body", True)]


async def test_inbox_batches_are_bounded_and_other_sources_left_alone(session, run, queue) -> None:
    connection = await _monzo_connection(run, {ACCOUNT_ID: str(uuid.uuid4())})
    await _deliver(session, _delivery(), source="other")
    for _ in range(3):
        await _deliver(session, _delivery())

    assert await process_inbox(session, defer=defer_sync, batch_size=2) == 2
    assert await process_inbox(session, defer=defer_sync, batch_size=2) == 1

    assert len(queue.jobs) == 1
    assert [j["args"]["connection_id"] for j in queue.jobs.values()] == [str(connection.id)]
    assert [o for o in await _outcomes(session) if o[0] == "other"] == [("other", None, False)]


async def test_body_never_reaches_the_ledger_or_audit(session, run, queue) -> None:
    await _monzo_connection(run, {ACCOUNT_ID: str(uuid.uuid4())})
    marker = "TEST-BODY-MARKER"
    await _deliver(session, _delivery(id="tx_TEST0001", description=marker))

    await process_inbox(session, defer=defer_sync)

    assert marker not in await _stored_text(session)
    ledger = await session.scalar(
        text("select count(*) from finance.transactions where description like :m"),
        {"m": f"%{marker}%"},
    )
    assert ledger == 0


async def test_empty_inbox_writes_nothing(session, queue) -> None:
    before = await session.scalar(text("select count(*) from core.audit_log"))

    assert await process_inbox(session, defer=defer_sync) == 0

    assert await session.scalar(text("select count(*) from core.audit_log")) == before
