import base64

import httpx
import pytest
from procrastinate import exceptions as procrastinate_exceptions
from procrastinate.testing import InMemoryConnector
from pydantic import SecretStr
from sqlalchemy import text

from lararium.config import Settings
from lararium.core.connections import (
    create_connection,
    get_connection,
    set_status,
)
from lararium.core.connectors import Connector, SyncContext
from lararium.core.errors import CredentialRejectedError
from lararium.core.json_types import JSONValue
from lararium.modules import registry
from lararium.worker import jobs
from lararium.worker.app import procrastinate_app

SETTINGS = Settings(
    database_url="postgresql+psycopg://x@localhost/x",
    auth_mode="dev",
    master_key=SecretStr(base64.b64encode(bytes(range(32))).decode()),
)


class FakeConnector:
    name = "fake"
    version = "1"
    capabilities = frozenset[str]()

    def __init__(self) -> None:
        self.error: Exception | None = None
        self.cursor: JSONValue = {"since": "t1"}
        self.seen: list[SyncContext] = []

    async def sync(self, ctx: SyncContext) -> JSONValue:
        self.seen.append(ctx)
        if self.error is not None:
            raise self.error
        return self.cursor


class RecordingNotifier:
    def __init__(self) -> None:
        self.messages: list[str] = []

    async def send(self, text: str) -> None:
        self.messages.append(text)


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeConnector:
    connector = FakeConnector()

    def lookup(name: str) -> Connector:
        assert name == "fake"
        return connector  # pyright: ignore[reportReturnType]

    monkeypatch.setattr(registry, "connector", lookup)
    return connector


@pytest.fixture
def notifier() -> RecordingNotifier:
    return RecordingNotifier()


async def _healthy_connection(run):
    connection = await run(create_connection, connector="fake", display_name="Fake bank", config={})
    await run(set_status, connection.id, "healthy")
    return connection


async def _run_sync(session, connection, notifier) -> None:
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(404))
    ) as http:
        await jobs.run_sync(session, connection.id, settings=SETTINGS, http=http, notifier=notifier)


async def test_run_sync_success_records_cursor(session, run, fake, notifier) -> None:
    connection = await _healthy_connection(run)

    await _run_sync(session, connection, notifier)

    after = await get_connection(session, connection.id)
    assert after.cursor == {"since": "t1"}
    assert after.status == "healthy"
    assert after.consecutive_failures == 0
    assert after.last_error is None
    assert after.last_success_at is not None
    assert notifier.messages == []
    ctx = fake.seen[0]
    assert (ctx.command.actor.type, ctx.command.actor.id) == ("connector", "fake")
    assert ctx.connection.id == connection.id
    assert ctx.master_key == bytes(range(32))
    audit = await session.scalar(
        text(
            "select count(*) from core.audit_log"
            " where actor_type = 'connector' and actor_id = 'fake'"
        )
    )
    assert audit == 1


async def test_failure_marks_failing(session, run, fake, notifier) -> None:
    connection = await _healthy_connection(run)
    fake.error = RuntimeError("upstream down")

    await _run_sync(session, connection, notifier)

    after = await get_connection(session, connection.id)
    assert (after.status, after.consecutive_failures) == ("failing", 1)
    assert after.last_error == "RuntimeError: upstream down"
    assert after.cursor is None
    assert notifier.messages == []


async def test_third_failure_notifies_once(session, run, fake, notifier) -> None:
    connection = await _healthy_connection(run)
    fake.error = RuntimeError("upstream down")

    for _ in range(3):
        await _run_sync(session, connection, notifier)
    assert len(notifier.messages) == 1
    assert "Fake bank" in notifier.messages[0]
    assert "3" in notifier.messages[0]

    await _run_sync(session, connection, notifier)
    assert len(notifier.messages) == 1
    assert (await get_connection(session, connection.id)).consecutive_failures == 4

    fake.error = None
    await _run_sync(session, connection, notifier)
    after = await get_connection(session, connection.id)
    assert (after.status, after.consecutive_failures, after.last_error) == ("healthy", 0, None)
    assert len(notifier.messages) == 2
    assert "recovered" in notifier.messages[1]
    assert "Fake bank" in notifier.messages[1]


async def test_credential_rejected_sets_needs_reauth(session, run, fake, notifier) -> None:
    connection = await _healthy_connection(run)
    fake.error = CredentialRejectedError("refresh token rejected")

    await _run_sync(session, connection, notifier)

    after = await get_connection(session, connection.id)
    assert after.status == "needs_reauth"
    assert after.last_error == "refresh token rejected"
    assert len(notifier.messages) == 1
    assert "Fake bank" in notifier.messages[0]
    assert "refresh token rejected" in notifier.messages[0]


@pytest.mark.parametrize("status", ["created", "authorising", "needs_reauth", "disabled"])
async def test_sync_skips_connections_that_cannot_sync(
    session, run, fake, notifier, status: str
) -> None:
    connection = await run(create_connection, connector="fake", display_name="F", config={})
    await run(set_status, connection.id, status)

    await _run_sync(session, connection, notifier)

    assert fake.seen == []
    assert (await get_connection(session, connection.id)).status == status


async def test_notifier_errors_never_fail_the_job(session, run, fake) -> None:
    class BrokenNotifier:
        async def send(self, text: str) -> None:
            raise RuntimeError("feishu is down")

    connection = await _healthy_connection(run)
    fake.error = CredentialRejectedError("rejected")

    await _run_sync(session, connection, BrokenNotifier())

    assert (await get_connection(session, connection.id)).status == "needs_reauth"


async def test_schedule_defers_one_sync_per_syncable_connection(session, run) -> None:
    healthy = await _healthy_connection(run)
    failing = await _healthy_connection(run)
    await run(set_status, failing.id, "failing")
    other = await run(create_connection, connector="fake", display_name="Z", config={})
    await run(set_status, other.id, "needs_reauth")

    with procrastinate_app.replace_connector(InMemoryConnector()) as app:
        assert await jobs.defer_due_syncs(session) == 2
        # Already queued: a second pass adds nothing.
        assert await jobs.defer_due_syncs(session) == 0
        queued = list(app.connector.jobs.values())  # pyright: ignore[reportAttributeAccessIssue]
        assert sorted(j["args"]["connection_id"] for j in queued) == sorted(
            [str(healthy.id), str(failing.id)]
        )
        assert {j["lock"] for j in queued} == {f"sync:{healthy.id}", f"sync:{failing.id}"}
        assert {j["queueing_lock"] for j in queued} == {f"sync:{healthy.id}", f"sync:{failing.id}"}


async def test_queueing_lock_rejects_duplicate_defer() -> None:
    with procrastinate_app.replace_connector(InMemoryConnector()):
        await jobs.sync_connection.configure(queueing_lock="sync:a", lock="sync:a").defer_async(
            connection_id="a"
        )
        with pytest.raises(procrastinate_exceptions.AlreadyEnqueued):
            await jobs.sync_connection.configure(queueing_lock="sync:a", lock="sync:a").defer_async(
                connection_id="a"
            )


async def test_periodic_schedules() -> None:
    crons = {
        task.task.name: task.cron
        for task in procrastinate_app.periodic_registry.periodic_tasks.values()
    }
    assert crons == {"schedule_syncs": "*/30 * * * *", "process_webhook_inbox": "* * * * *"}


async def test_procrastinate_schema_present(session) -> None:
    assert (
        await session.scalar(text("select to_regclass('procrastinate.procrastinate_jobs')"))
        is not None
    )
    await session.execute(
        text(
            "insert into procrastinate.procrastinate_jobs (queue_name, task_name, args)"
            " values ('default', 'sync_connection', '{}')"
        )
    )
    count = await session.scalar(text("select count(*) from procrastinate.procrastinate_jobs"))
    assert count == 1
