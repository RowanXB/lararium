import base64
import uuid
from collections.abc import Awaitable, Callable
from datetime import timedelta

import httpx
import pytest
from procrastinate import PsycopgConnector
from procrastinate import exceptions as procrastinate_exceptions
from procrastinate import utils as procrastinate_utils
from procrastinate.jobs import Status
from procrastinate.testing import InMemoryConnector
from pydantic import SecretStr
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from lararium.config import Settings
from lararium.core.actors import Actor
from lararium.core.commands import command_context
from lararium.core.connections import (
    create_connection,
    get_connection,
    set_status,
)
from lararium.core.connectors import BACKFILL_FAILED, Connector, SyncContext
from lararium.core.errors import CredentialRejectedError
from lararium.core.json_types import JSONValue
from lararium.db import create_engine, make_sessionmaker, psycopg_dsn
from lararium.modules import registry
from lararium.modules.finance.monzo.client import MonzoApiError
from lararium.worker import jobs
from lararium.worker.app import defer_sync, procrastinate_app

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
        # Runs in the middle of the sync, as a concurrent change would.
        self.during: Callable[[], Awaitable[None]] | None = None

    async def sync(self, ctx: SyncContext) -> JSONValue:
        self.seen.append(ctx)
        if self.during is not None:
            await self.during()
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


async def _run_sync(session, connection, notifier, session_factory=None, backfill=False) -> None:
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(404))
    ) as http:
        await jobs.run_sync(
            session,
            connection.id,
            settings=SETTINGS,
            http=http,
            notifier=notifier,
            session_factory=session_factory or async_sessionmaker(bind=session.bind),
            backfill=backfill,
        )


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


async def test_run_sync_passes_the_webhook_target(session, run, fake, notifier) -> None:
    connection = await _healthy_connection(run)
    settings = Settings(
        database_url="postgresql+psycopg://x@localhost/x",
        auth_mode="dev",
        master_key=SecretStr(base64.b64encode(bytes(range(32))).decode()),
        webhook_base_url="https://hooks.lararium.example/",
        webhook_secret=SecretStr("whsec-TEST-0123456789abcdef0123456789"),
    )
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(404))
    ) as http:
        await jobs.run_sync(
            session,
            connection.id,
            settings=settings,
            http=http,
            notifier=notifier,
            session_factory=async_sessionmaker(bind=session.bind),
        )

    webhook = fake.seen[0].webhook
    assert webhook is not None
    assert webhook.base_url == "https://hooks.lararium.example"
    assert webhook.secret.reveal() == "whsec-TEST-0123456789abcdef0123456789"
    assert "whsec-TEST-0123456789abcdef0123456789" not in repr(fake.seen[0])


async def test_run_sync_without_webhook_settings_passes_none(session, run, fake, notifier) -> None:
    connection = await _healthy_connection(run)

    await _run_sync(session, connection, notifier)

    assert fake.seen[0].webhook is None


async def test_failure_marks_failing(session, run, fake, notifier) -> None:
    connection = await _healthy_connection(run)
    fake.error = RuntimeError("upstream down")

    await _run_sync(session, connection, notifier)

    after = await get_connection(session, connection.id)
    assert (after.status, after.consecutive_failures) == ("failing", 1)
    assert after.last_error == "RuntimeError"
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


async def test_last_error_and_notification_carry_no_exception_text(
    session, run, fake, notifier
) -> None:
    connection = await _healthy_connection(run)
    # Exception messages can quote anything, such as an upstream response.
    fake.error = ValueError("account 12345678 sort code 04-00-04: unexpected field")

    for _ in range(3):
        await _run_sync(session, connection, notifier)

    after = await get_connection(session, connection.id)
    assert after.last_error == "ValueError"
    (message,) = notifier.messages
    assert "ValueError" in message
    for shown in (after.last_error, message):
        assert "12345678" not in shown and "04-00-04" not in shown


async def test_last_error_names_the_monzo_error_code(session, run, fake, notifier) -> None:
    connection = await _healthy_connection(run)
    fake.error = MonzoApiError(500, "internal_service")

    await _run_sync(session, connection, notifier)

    after = await get_connection(session, connection.id)
    assert after.last_error == "MonzoApiError: Monzo API error 500: internal_service"


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


async def test_a_backfill_keeps_the_connection_authorising_until_stored(
    session, run, fake, notifier
) -> None:
    connection = await run(create_connection, connector="fake", display_name="Fake bank", config={})
    await run(set_status, connection.id, "authorising")
    seen: list[str] = []

    async def status_during() -> None:
        seen.append((await get_connection(session, connection.id)).status)

    fake.during = status_during

    await _run_sync(session, connection, notifier, backfill=True)

    assert seen == ["authorising"]
    assert fake.seen[0].full_backfill
    assert (await get_connection(session, connection.id)).status == "healthy"
    assert notifier.messages == []


async def test_a_failed_backfill_needs_authorising_again(session, run, fake, notifier) -> None:
    connection = await run(create_connection, connector="fake", display_name="Fake bank", config={})
    await run(set_status, connection.id, "authorising")
    fake.error = RuntimeError("upstream down")

    await _run_sync(session, connection, notifier, backfill=True)

    after = await get_connection(session, connection.id)
    assert (after.status, after.last_error) == ("needs_reauth", BACKFILL_FAILED)
    assert len(notifier.messages) == 1 and BACKFILL_FAILED in notifier.messages[0]


async def test_only_the_approval_backfill_fetches_all_history(session, run, fake, notifier) -> None:
    connection = await _healthy_connection(run)

    await _run_sync(session, connection, notifier)
    # A backfill runs only for a connection waiting on its approval.
    await _run_sync(session, connection, notifier, backfill=True)

    assert [ctx.full_backfill for ctx in fake.seen] == [False]


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
    assert crons == {
        "schedule_syncs": "*/30 * * * *",
        "process_webhook_inbox": "* * * * *",
        "retry_stalled_jobs": "*/5 * * * *",
    }


@pytest.mark.parametrize("worker_gone", ["dead", "pruned"])
async def test_a_sync_left_doing_by_a_lost_worker_is_retried(worker_gone: str) -> None:
    with procrastinate_app.replace_connector(InMemoryConnector()) as app:
        connector: InMemoryConnector = app.connector  # pyright: ignore[reportAssignmentType]
        manager = app.job_manager
        assert await defer_sync("c1")
        lost = await manager.register_worker()
        stalled = await manager.fetch_job(queues=None, worker_id=lost)
        assert stalled is not None
        # The worker is killed mid-job: its heartbeat stops, and a restarted worker may
        # prune it, which leaves the job `doing` with no worker.
        connector.workers[lost] = procrastinate_utils.utcnow() - timedelta(minutes=5)
        if worker_gone == "pruned":
            await manager.prune_stalled_workers(seconds_since_heartbeat=30)
        live = await manager.register_worker()
        # The stalled job still holds the connection's lock.
        assert await manager.fetch_job(queues=None, worker_id=live) is None

        assert await jobs.retry_stalled(manager) == 1

        retried = await manager.fetch_job(queues=None, worker_id=live)
        assert retried is not None and retried.id == stalled.id
        await manager.finish_job(retried, status=Status.SUCCEEDED, delete_job=False)
        # The connection syncs again: a new sync can be queued and run.
        assert await defer_sync("c1")
        assert await manager.fetch_job(queues=None, worker_id=live) is not None


async def test_a_stalled_sync_whose_replacement_is_queued_frees_the_lock(
    backend_url: str, owner_url: str, migrated: None
) -> None:
    # On PostgreSQL, whose unique index allows one `todo` job per queueing lock: the
    # in-memory connector doesn't enforce it.
    connector = PsycopgConnector(
        conninfo=psycopg_dsn(backend_url), kwargs={"options": "-c search_path=procrastinate"}
    )
    connection_id = f"stalled-{uuid.uuid4()}"
    engine = create_engine(owner_url)
    with procrastinate_app.replace_connector(connector) as app:
        await app.open_async()
        manager = app.job_manager
        workers: list[int] = []
        try:
            assert await defer_sync(connection_id)
            lost = await manager.register_worker()
            workers.append(lost)
            stalled = await manager.fetch_job(queues=None, worker_id=lost)
            assert stalled is not None
            async with engine.begin() as conn:
                await conn.execute(
                    text(
                        "update procrastinate.procrastinate_workers"
                        " set last_heartbeat = now() - interval '5 minutes' where id = :id"
                    ),
                    {"id": lost},
                )
            # A webhook queues a sync while the stalled one still holds the lock.
            assert await defer_sync(connection_id)

            assert await jobs.retry_stalled(manager) == 1

            live = await manager.register_worker()
            workers.append(live)
            replacement = await manager.fetch_job(queues=None, worker_id=live)
            assert replacement is not None and replacement.id != stalled.id
            async with engine.connect() as conn:
                status = await conn.scalar(
                    text("select status from procrastinate.procrastinate_jobs where id = :id"),
                    {"id": stalled.id},
                )
            assert status == "failed"
        finally:
            async with engine.begin() as conn:
                ids = (
                    "select id from procrastinate.procrastinate_jobs"
                    " where args->>'connection_id' = :c"
                )
                params = {"c": connection_id}
                await conn.execute(
                    text(f"delete from procrastinate.procrastinate_events where job_id in ({ids})"),
                    params,
                )
                await conn.execute(
                    text(f"delete from procrastinate.procrastinate_jobs where id in ({ids})"),
                    params,
                )
                await conn.execute(
                    text("delete from procrastinate.procrastinate_workers where id = any(:ids)"),
                    {"ids": workers},
                )
            await app.close_async()
            await engine.dispose()


async def test_jobs_of_live_workers_are_not_retried() -> None:
    with procrastinate_app.replace_connector(InMemoryConnector()) as app:
        manager = app.job_manager
        assert await defer_sync("c1")
        worker = await manager.register_worker()
        assert await manager.fetch_job(queues=None, worker_id=worker) is not None

        assert await jobs.retry_stalled(manager) == 0


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


@pytest.mark.parametrize(
    "error",
    [None, RuntimeError("upstream down"), CredentialRejectedError("rejected")],
    ids=["success", "failure", "rejected"],
)
async def test_disable_during_sync_is_kept(
    backend_url: str, owner_url: str, migrated: None, fake, notifier, error: Exception | None
) -> None:
    # Real commits through their own engine: the disable must commit while the sync runs,
    # outside its transaction, as the owner's request would.
    engine = create_engine(backend_url)
    sessions = make_sessionmaker(engine)
    connection_id = None
    owner = Actor("user", "dev")
    try:
        async with sessions() as setup, command_context(setup, owner, "test.setup", {}) as ctx:
            connection = await create_connection(
                ctx, connector="fake", display_name=f"Fake {uuid.uuid4()}", config={}
            )
            connection_id = connection.id
            await set_status(ctx, connection_id, "failing")

        async def disable() -> None:
            args = {"connection_id": str(connection_id)}
            async with (
                sessions() as other,
                command_context(other, owner, "disable_connection", args) as ctx,
            ):
                await set_status(ctx, connection.id, "disabled")
            fake.error = error

        fake.during = disable
        # From failing: a success would mark it healthy and say it recovered; a rejection
        # would mark it needs_reauth and notify.
        async with sessions() as session:
            await _run_sync(session, connection, notifier, sessions)

        async with sessions() as fresh:
            assert (await get_connection(fresh, connection_id)).status == "disabled"
        assert notifier.messages == []
    finally:
        if connection_id is not None:
            await _delete_connection(engine, owner_url, connection_id)
        await engine.dispose()


async def _delete_connection(engine, owner_url: str, connection_id: uuid.UUID) -> None:
    async with engine.begin() as conn:
        params = {"id": connection_id}
        await conn.execute(text("delete from core.events where subject_id = :id"), params)
        await conn.execute(text("delete from core.connections where id = :id"), params)
    owner = create_engine(owner_url)
    try:
        # The audit log is append-only for the backend role.
        async with owner.begin() as conn:
            await conn.execute(
                text(
                    "delete from core.audit_log where args->>'connection_id' = :id"
                    " or changes ? ('connection:' || :id)"
                ),
                {"id": str(connection_id)},
            )
    finally:
        await owner.dispose()
