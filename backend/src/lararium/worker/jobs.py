"""Worker jobs: syncing connections, waiting for a new one's approval, and the periodic
tasks that schedule them."""

import asyncio
import logging
from collections.abc import AsyncGenerator, Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from uuid import UUID

import httpx
from procrastinate import exceptions as procrastinate_exceptions
from procrastinate.jobs import Status
from procrastinate.manager import JobManager
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from lararium.config import Settings, get_settings, load_master_key
from lararium.core.actors import Actor
from lararium.core.commands import CommandContext, command_context
from lararium.core.connections import (
    AUTHORISED_AT_KEY,
    ConnectionStatus,
    get_connection,
    record_sync_failure,
    record_sync_success,
    transition_status,
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
from lararium.worker.app import defer_await_approval, defer_sync, procrastinate_app

logger = logging.getLogger(__name__)

# Consecutive failed syncs before the owner is told.
NOTIFY_AFTER_FAILURES = 3
# Only these can sync: the others have no usable credentials, or the owner turned them off.
SYNCABLE = ("healthy", "failing")
MAX_ERROR_LENGTH = 500
# After the OAuth callback (Decision 7): poll every 10 s for up to 4 minutes, which leaves the
# full backfill inside the 5 minutes after approval in which Monzo serves all history.
APPROVAL_POLL_SECONDS = 10
APPROVAL_WINDOW = timedelta(minutes=4)
APPROVAL_EXPIRED = "Approve the Monzo request in the app within 4 minutes, then authorise again"


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
    backfill_for: str | None = None,
) -> None:
    """Sync one connection and record the outcome, notifying the owner when it matters.

    A failing sync is recorded on the connection, not raised: the schedule tries again. A
    status changed while the sync ran (the owner disabled the connection, say) is kept.

    `backfill_for`: the authorisation (its AUTHORISED_AT_KEY) whose approval this sync follows
    with the full backfill, the only sync that fetches all history (run_await_approval). The
    connection is `authorising` until the backfill is stored, and then healthy. If it fails it
    can't be retried outside the window, and syncing on with partial history (and no opening
    balance) would make the numbers wrong without saying so, so the connection needs
    authorising again (ruling R18). Either outcome applies only while that authorisation is
    still the current one: one made meanwhile has its own approval job.
    """
    backfill = backfill_for is not None
    connection = await get_connection(session, connection_id)
    allowed: tuple[ConnectionStatus, ...] = ("authorising",) if backfill else SYNCABLE
    if connection.status not in allowed:
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
                    full_backfill=backfill,
                )
            )
            await record_sync_success(ctx, connection_id, cursor)
            from_ = ("authorising",) if backfill else ("failing",)
            moved = await transition_status(
                ctx, connection_id, "healthy", only_from=from_, authorised_at=backfill_for
            )
            recovered = moved == "failing"
    except Exception as exc:
        # command_context has rolled the failed sync back; record the failure on its own.
        rejected = isinstance(exc, CredentialRejectedError)
        error = _describe(exc)[:MAX_ERROR_LENGTH]
        logger.warning("sync of connection %s failed: %s", connection_id, type(exc).__name__)
        backfill_failed = backfill and not rejected
        async with command_context(session, actor, "connection.sync_failed", args) as ctx:
            failures = await record_sync_failure(ctx, connection_id, error)
            moved = await transition_status(
                ctx,
                connection_id,
                "needs_reauth" if rejected or backfill else "failing",
                only_from=allowed,
                error=BACKFILL_FAILED if backfill_failed else None,
                authorised_at=backfill_for,
            )
        if moved is None:
            return
        if rejected:
            await _notify_reauth(notifier, connection.display_name, error)
        elif backfill_failed:
            await _notify(notifier, f"{connection.display_name}: {BACKFILL_FAILED}")
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


async def run_await_approval(
    session: AsyncSession,
    connection_id: UUID,
    *,
    settings: Settings,
    http: httpx.AsyncClient,
    notifier: Notifier,
    session_factory: async_sessionmaker[AsyncSession],
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
    sleep: Callable[[int], Awaitable[None]] = asyncio.sleep,
) -> None:
    """Wait for the owner to approve a newly authorised connection, then backfill it.

    Polls every 10 s while the connection is `authorising`, until 4 minutes after it was
    authorised. On the first success it runs the full backfill at once, inside the window in
    which all history can be fetched (run_sync with backfill_for). On timeout the connection
    needs authorising again, and the owner is told.

    Everything here is about one authorisation, the current one when it is read: the window
    runs from when it was made (not from when this job started), and no outcome is applied
    once a newer one replaced it. So authorising again mid-wait gets fresh minutes, and a job
    re-run after its worker died (mid-backfill, say) doesn't backfill late.
    """
    master_key = _master_key(settings)
    connection = await get_connection(session, connection_id)
    connector = registry.connector(connection.connector)
    actor = Actor("connector", connector.name)
    args = {"connection_id": str(connection_id), "connector": connector.name}
    check_id: UUID = (await session.execute(select(func.uuidv7()))).scalar_one()

    while True:
        connection = await get_connection(session, connection_id)
        if connection.status != "authorising":
            logger.info(
                "approval of connection %s ended: status is %s", connection_id, connection.status
            )
            await session.rollback()
            return
        authorised_at = connection.config.get(AUTHORISED_AT_KEY)
        if not isinstance(authorised_at, str):
            authorised_at = ""  # no window to wait in: it has expired
        expired = (
            not authorised_at or now() >= datetime.fromisoformat(authorised_at) + APPROVAL_WINDOW
        )
        if expired:
            await session.rollback()
            if await _end_approval(
                session, connection_id, actor, args, APPROVAL_EXPIRED, authorised_at
            ):
                await _notify(notifier, f"{connection.display_name}: {APPROVAL_EXPIRED}")
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
            if await _end_approval(session, connection_id, actor, args, error, authorised_at):
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
        await sleep(APPROVAL_POLL_SECONDS)

    await run_sync(
        session,
        connection_id,
        settings=settings,
        http=http,
        notifier=notifier,
        session_factory=session_factory,
        backfill_for=authorised_at,
    )


async def _end_approval(
    session: AsyncSession,
    connection_id: UUID,
    actor: Actor,
    args: dict[str, str],
    error: str,
    authorised_at: str,
) -> bool:
    """Move an authorising connection to needs_reauth; False if its status changed, or it was
    authorised again, meanwhile."""
    async with command_context(session, actor, "connection.approval_failed", args) as ctx:
        moved = await transition_status(
            ctx,
            connection_id,
            "needs_reauth",
            only_from=("authorising",),
            error=error,
            # A missing authorisation (no window to wait in) has nothing to compare.
            authorised_at=authorised_at or None,
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


async def defer_due_approvals(session: AsyncSession) -> int:
    """Queue the approval job for every connection still `authorising`; return how many were
    newly queued.

    Leaving `authorising` otherwise depends on the one job the callback queued running to the
    end, which a failed statement or a lost defer can prevent. Run again, the job carries on
    within the authorisation's window, or ends an expired one and tells the owner; a duplicate
    of a queued job is dropped.
    """
    ids = (
        (
            await session.execute(
                select(Connection.id)
                .where(Connection.status == "authorising")
                .order_by(Connection.display_name, Connection.id)
            )
        )
        .scalars()
        .all()
    )
    return sum([await defer_await_approval(connection_id) for connection_id in ids])


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
            approvals = await defer_due_approvals(session)
        logger.info("queued %d connection syncs and %d approval checks", queued, approvals)
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
    blocks every later sync of its connection.

    Only one job per queueing lock can be `todo`, and the stalled job doesn't hold it while
    `doing`, so an equivalent job (a sync queued by a webhook meanwhile, say) may already be
    waiting. Then the stalled one is closed as failed instead: that frees the lock, and the
    waiting job does the same work.
    """
    stalled = list(await manager.get_stalled_jobs())
    for job in stalled:
        try:
            await manager.retry_job(job)
        except procrastinate_exceptions.UniqueViolation:
            await manager.finish_job(job, status=Status.FAILED, delete_job=False)
    return len(stalled)


@procrastinate_app.periodic(cron="*/5 * * * *")
@procrastinate_app.task(name="retry_stalled_jobs", queueing_lock="retry_stalled_jobs")
async def retry_stalled_jobs(timestamp: int) -> int:
    retried = await retry_stalled(procrastinate_app.job_manager)
    if retried:
        logger.warning("retried %d jobs left running by a stopped worker", retried)
    return retried
