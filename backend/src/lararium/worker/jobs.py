"""Worker jobs: syncing connections, waiting for a new one's approval, and the periodic
tasks that schedule them."""

import asyncio
import logging
import time
from collections.abc import AsyncGenerator, Awaitable, Callable
from contextlib import asynccontextmanager
from uuid import UUID

import httpx
from procrastinate.manager import JobManager
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from lararium.config import Settings, get_settings, load_master_key
from lararium.core.actors import Actor
from lararium.core.commands import CommandContext, command_context
from lararium.core.connections import (
    get_connection,
    record_sync_failure,
    record_sync_success,
    transition_status,
    update_config,
)
from lararium.core.connectors import BACKFILL_FAILED, SyncContext, WebhookTarget
from lararium.core.errors import CredentialRejectedError
from lararium.core.models import Connection
from lararium.core.notify import Notifier, notifier_from_settings
from lararium.core.secrets import Secret
from lararium.db import create_engine, make_sessionmaker
from lararium.modules import registry
from lararium.modules.finance.monzo.client import MonzoApiError
from lararium.modules.finance.monzo.webhooks import process_inbox
from lararium.worker.app import defer_sync, procrastinate_app

logger = logging.getLogger(__name__)

# Consecutive failed syncs before the owner is told.
NOTIFY_AFTER_FAILURES = 3
# Only these can sync: the others have no usable credentials, or the owner turned them off.
SYNCABLE = ("healthy", "failing")
MAX_ERROR_LENGTH = 500
# After the OAuth callback (Decision 7): poll every 10 s for up to 4 minutes, which leaves the
# full backfill inside the 5 minutes after approval in which Monzo serves all history.
APPROVAL_POLL_SECONDS = 10
APPROVAL_WINDOW_SECONDS = 4 * 60
APPROVAL_EXPIRED = "Approve the Monzo request in the app within 4 minutes, then authorise again"
BACKFILL_REQUESTED = "backfill_requested"


async def _notify(notifier: Notifier, text: str) -> None:
    """Send a notification; whatever goes wrong with it never fails the job."""
    try:
        await notifier.send(text)
    except Exception:
        logger.warning("sending a notification failed", exc_info=True)


async def run_sync(
    session: AsyncSession,
    connection_id: UUID,
    *,
    settings: Settings,
    http: httpx.AsyncClient,
    notifier: Notifier,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Sync one connection and record the outcome, notifying the owner when it matters.

    A failing sync is recorded on the connection, not raised: the schedule tries again. A
    status changed while the sync ran (the owner disabled the connection, say) is kept.
    """
    connection = await get_connection(session, connection_id)
    if connection.status not in SYNCABLE:
        logger.info("not syncing connection %s: status is %s", connection_id, connection.status)
        return
    master_key = _master_key(settings)
    connector = registry.connector(connection.connector)
    actor = Actor("connector", connector.name)
    args = {"connection_id": str(connection_id), "connector": connector.name}

    try:
        async with command_context(session, actor, "connection.sync", args) as ctx:
            sync_id: UUID = (await session.execute(select(func.uuidv7()))).scalar_one()
            cursor = await connector.sync(
                SyncContext(
                    command=ctx,
                    connection=connection,
                    sync_id=sync_id,
                    master_key=master_key,
                    http=http,
                    notifier=notifier,
                    session_factory=session_factory,
                    webhook=_webhook_target(settings),
                )
            )
            await record_sync_success(ctx, connection_id, cursor)
            recovered = (
                await transition_status(ctx, connection_id, "healthy", only_from=("failing",))
                is not None
            )
    except Exception as exc:
        # command_context has rolled the failed sync back; record the failure on its own.
        rejected = isinstance(exc, CredentialRejectedError)
        error = _describe(exc)[:MAX_ERROR_LENGTH]
        logger.warning("sync of connection %s failed: %s", connection_id, type(exc).__name__)
        async with command_context(session, actor, "connection.sync_failed", args) as ctx:
            failures = await record_sync_failure(ctx, connection_id, error)
            moved = await transition_status(
                ctx,
                connection_id,
                "needs_reauth" if rejected else "failing",
                only_from=SYNCABLE,
            )
        if moved is None:
            return
        if rejected:
            await _notify_reauth(notifier, connection.display_name, error)
        elif failures == NOTIFY_AFTER_FAILURES:
            await _notify(
                notifier,
                f"{connection.display_name} has failed to sync {failures} times in a row."
                f" Last error: {error}",
            )
        return

    if recovered:
        await _notify(notifier, f"{connection.display_name} has recovered and is syncing again.")


def _describe(exc: Exception) -> str:
    """What last_error (and the owner's notification) says about a failed sync.

    Raw exception text can quote anything, an upstream response included, so only messages
    written to be shown are: a rejection's, and a MonzoApiError's (its status and code).
    """
    if isinstance(exc, CredentialRejectedError):
        return str(exc)
    if isinstance(exc, MonzoApiError):
        return f"{type(exc).__name__}: {exc}"
    return type(exc).__name__


def _webhook_target(settings: Settings) -> WebhookTarget | None:
    webhook = settings.webhook()
    if webhook is None:
        return None
    base_url, secret = webhook
    return WebhookTarget(base_url, Secret(secret.get_secret_value()))


def _master_key(settings: Settings) -> bytes:
    if settings.master_key is None:
        raise RuntimeError("syncing needs the master key (LARARIUM_MASTER_KEY)")
    return load_master_key(settings.master_key)


async def _notify_reauth(notifier: Notifier, display_name: str, error: str) -> None:
    await _notify(notifier, f"{display_name} needs to be authorised again: {error}")


def _monotonic_seconds() -> int:
    return time.monotonic_ns() // 1_000_000_000


async def run_await_approval(
    session: AsyncSession,
    connection_id: UUID,
    *,
    settings: Settings,
    http: httpx.AsyncClient,
    notifier: Notifier,
    session_factory: async_sessionmaker[AsyncSession],
    clock: Callable[[], int] = _monotonic_seconds,
    sleep: Callable[[int], Awaitable[None]] = asyncio.sleep,
) -> None:
    """Wait for the owner to approve a newly authorised connection, then backfill it.

    Polls every 10 s for up to 4 minutes while the connection is `authorising`. On the first
    success it requests a full backfill, marks the connection healthy and syncs it at once,
    inside the window in which all history can be fetched. On timeout, or if that backfill
    fails, the connection needs authorising again, and the owner is told.
    """
    master_key = _master_key(settings)
    connection = await get_connection(session, connection_id)
    connector = registry.connector(connection.connector)
    actor = Actor("connector", connector.name)
    args = {"connection_id": str(connection_id), "connector": connector.name}
    check_id: UUID = (await session.execute(select(func.uuidv7()))).scalar_one()
    deadline = clock() + APPROVAL_WINDOW_SECONDS

    while True:
        connection = await get_connection(session, connection_id)
        if connection.status != "authorising":
            logger.info(
                "approval of connection %s ended: status is %s", connection_id, connection.status
            )
            await session.rollback()
            return
        try:
            # check_access writes nothing through this context; a token it refreshes is
            # committed in a session of its own.
            approved = await connector.check_access(
                SyncContext(
                    command=CommandContext(session, actor, check_id),
                    connection=connection,
                    sync_id=check_id,
                    master_key=master_key,
                    http=http,
                    notifier=notifier,
                    session_factory=session_factory,
                )
            )
        except CredentialRejectedError as exc:
            await session.rollback()
            error = str(exc)[:MAX_ERROR_LENGTH]
            if await _end_approval(session, connection_id, actor, args, error):
                await _notify_reauth(notifier, connection.display_name, error)
            return
        except Exception as exc:
            # Probably transient (a timeout, an outage): keep polling until the window closes.
            logger.warning(
                "approval check of connection %s failed: %s", connection_id, type(exc).__name__
            )
            approved = False
        # End the read transaction: it isn't held open across the wait.
        await session.rollback()
        if approved:
            break
        if clock() >= deadline:
            if await _end_approval(session, connection_id, actor, args, APPROVAL_EXPIRED):
                await _notify(notifier, f"{connection.display_name}: {APPROVAL_EXPIRED}")
            return
        await sleep(APPROVAL_POLL_SECONDS)

    async with command_context(session, actor, "connection.approved", args) as ctx:
        moved = await transition_status(ctx, connection_id, "healthy", only_from=("authorising",))
        if moved is not None:
            # Read by the sync below, which removes it once the backfill is stored.
            await update_config(ctx, connection_id, {BACKFILL_REQUESTED: True})
    if moved is None:
        return
    await run_sync(
        session,
        connection_id,
        settings=settings,
        http=http,
        notifier=notifier,
        session_factory=session_factory,
    )
    # A backfill that failed can't be retried outside the window, and syncing on with partial
    # history (and no opening balance) would make the numbers wrong without saying so. So the
    # connection needs authorising again, which opens a new window (ruling R18).
    after = await get_connection(session, connection_id)
    if after.config.get(BACKFILL_REQUESTED):
        async with command_context(session, actor, "connection.backfill_failed", args) as ctx:
            await update_config(ctx, connection_id, remove=[BACKFILL_REQUESTED])
            # A rejection has already moved it to needs_reauth, and told the owner.
            moved = await transition_status(
                ctx, connection_id, "needs_reauth", only_from=SYNCABLE, error=BACKFILL_FAILED
            )
        if moved is not None:
            await _notify(notifier, f"{after.display_name}: {BACKFILL_FAILED}")


async def _end_approval(
    session: AsyncSession,
    connection_id: UUID,
    actor: Actor,
    args: dict[str, str],
    error: str,
) -> bool:
    """Move an authorising connection to needs_reauth; False if its status changed meanwhile."""
    async with command_context(session, actor, "connection.approval_failed", args) as ctx:
        moved = await transition_status(
            ctx, connection_id, "needs_reauth", only_from=("authorising",), error=error
        )
    return moved is not None


async def defer_due_syncs(session: AsyncSession) -> int:
    """Queue a sync for every connection that can sync; return how many were newly queued."""
    ids = (
        (
            await session.execute(
                select(Connection.id)
                .where(Connection.status.in_(SYNCABLE))
                .order_by(Connection.display_name, Connection.id)
            )
        )
        .scalars()
        .all()
    )
    return sum([await defer_sync(connection_id) for connection_id in ids])


@asynccontextmanager
async def _job_resources(
    settings: Settings,
) -> AsyncGenerator[tuple[httpx.AsyncClient, async_sessionmaker[AsyncSession]]]:
    engine = create_engine(settings.database_url)
    try:
        async with httpx.AsyncClient(timeout=30) as http:
            yield http, make_sessionmaker(engine)
    finally:
        await engine.dispose()


@procrastinate_app.task(name="sync_connection")
async def sync_connection(connection_id: str) -> None:
    # Queued with lock and queueing_lock "sync:<id>" (see defer_sync): at most one sync per
    # connection is queued, and one runs.
    settings = get_settings()
    async with _job_resources(settings) as (http, sessions), sessions() as session:
        await run_sync(
            session,
            UUID(connection_id),
            settings=settings,
            http=http,
            notifier=notifier_from_settings(settings, http),
            session_factory=sessions,
        )


@procrastinate_app.task(name="await_approval")
async def await_approval(connection_id: str) -> None:
    # Queued with lock "sync:<id>" (see defer_await_approval): no sync overlaps it.
    settings = get_settings()
    async with _job_resources(settings) as (http, sessions), sessions() as session:
        await run_await_approval(
            session,
            UUID(connection_id),
            settings=settings,
            http=http,
            notifier=notifier_from_settings(settings, http),
            session_factory=sessions,
        )


@procrastinate_app.periodic(cron="*/30 * * * *")
@procrastinate_app.task(name="schedule_syncs")
async def schedule_syncs(timestamp: int) -> None:
    engine = create_engine(get_settings().database_url)
    try:
        async with make_sessionmaker(engine)() as session:
            queued = await defer_due_syncs(session)
        logger.info("queued %d connection syncs", queued)
    finally:
        await engine.dispose()


@procrastinate_app.periodic(cron="* * * * *")
@procrastinate_app.task(name="process_webhook_inbox")
async def process_webhook_inbox(timestamp: int) -> int:
    """Turn stored webhook deliveries into syncs; the queueing lock drops duplicates."""
    engine = create_engine(get_settings().database_url)
    try:
        async with make_sessionmaker(engine)() as session:
            processed = await process_inbox(session, defer=defer_sync)
    finally:
        await engine.dispose()
    if processed:
        logger.info("processed %d webhook deliveries", processed)
    return processed


async def retry_stalled(manager: JobManager) -> int:
    """Put back to `todo` the jobs left `doing` by a worker that stopped sending heartbeats
    (killed mid-job, say); return how many. Until then such a job holds its lock, and so
    blocks every later sync of its connection."""
    stalled = list(await manager.get_stalled_jobs())
    for job in stalled:
        await manager.retry_job(job)
    return len(stalled)


@procrastinate_app.periodic(cron="*/5 * * * *")
@procrastinate_app.task(name="retry_stalled_jobs", queueing_lock="retry_stalled_jobs")
async def retry_stalled_jobs(timestamp: int) -> int:
    retried = await retry_stalled(procrastinate_app.job_manager)
    if retried:
        logger.warning("retried %d jobs left running by a stopped worker", retried)
    return retried
