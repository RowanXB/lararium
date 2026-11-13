"""Worker jobs: syncing connections, and the periodic tasks that schedule them."""

import logging
from uuid import UUID

import httpx
from procrastinate import exceptions as procrastinate_exceptions
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from lararium.config import Settings, get_settings
from lararium.core.actors import Actor
from lararium.core.commands import command_context
from lararium.core.connections import (
    get_connection,
    record_sync_failure,
    record_sync_success,
    set_status,
)
from lararium.core.connectors import SyncContext
from lararium.core.errors import CredentialRejectedError
from lararium.core.models import Connection
from lararium.core.notify import Notifier, notifier_from_settings
from lararium.core.secrets import load_master_key
from lararium.db import create_engine, make_sessionmaker
from lararium.modules import registry
from lararium.worker.app import procrastinate_app

logger = logging.getLogger(__name__)

# Consecutive failed syncs before the owner is told.
NOTIFY_AFTER_FAILURES = 3
# Only these can sync: the others have no usable credentials, or the owner turned them off.
SYNCABLE = ("healthy", "failing")
MAX_ERROR_LENGTH = 500


def _lock(connection_id: UUID | str) -> str:
    return f"sync:{connection_id}"


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

    A failing sync is recorded on the connection, not raised: the schedule tries again.
    """
    connection = await get_connection(session, connection_id)
    if connection.status not in SYNCABLE:
        logger.info("not syncing connection %s: status is %s", connection_id, connection.status)
        return
    if settings.master_key is None:
        raise RuntimeError("syncing needs the master key (LARARIUM_MASTER_KEY)")
    master_key = load_master_key(settings.master_key)
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
                )
            )
            await record_sync_success(ctx, connection_id, cursor)
            if connection.status == "failing":
                await set_status(ctx, connection_id, "healthy")
    except Exception as exc:
        # command_context has rolled the failed sync back; record the failure on its own.
        rejected = isinstance(exc, CredentialRejectedError)
        error = (str(exc) if rejected else f"{type(exc).__name__}: {exc}")[:MAX_ERROR_LENGTH]
        logger.warning("sync of connection %s failed: %s", connection_id, type(exc).__name__)
        async with command_context(session, actor, "connection.sync_failed", args) as ctx:
            failures = await record_sync_failure(ctx, connection_id, error)
            await set_status(ctx, connection_id, "needs_reauth" if rejected else "failing")
        if rejected:
            await _notify(
                notifier, f"{connection.display_name} needs to be authorised again: {error}"
            )
        elif failures == NOTIFY_AFTER_FAILURES:
            await _notify(
                notifier,
                f"{connection.display_name} has failed to sync {failures} times in a row."
                f" Last error: {error}",
            )
        return

    if connection.status == "failing":
        await _notify(notifier, f"{connection.display_name} has recovered and is syncing again.")


async def defer_sync(connection_id: UUID | str) -> bool:
    """Queue a sync for the connection; False if one is already queued."""
    lock = _lock(connection_id)
    try:
        await sync_connection.configure(lock=lock, queueing_lock=lock).defer_async(
            connection_id=str(connection_id)
        )
    except procrastinate_exceptions.AlreadyEnqueued:
        return False
    return True


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


@procrastinate_app.task(name="sync_connection")
async def sync_connection(connection_id: str) -> None:
    # Queued with lock and queueing_lock "sync:<id>" (see defer_sync): at most one sync per
    # connection is queued, and one runs.
    settings = get_settings()
    engine = create_engine(settings.database_url)
    try:
        async with httpx.AsyncClient(timeout=30) as http:
            sessions = make_sessionmaker(engine)
            async with sessions() as session:
                await run_sync(
                    session,
                    UUID(connection_id),
                    settings=settings,
                    http=http,
                    notifier=notifier_from_settings(settings, http),
                    session_factory=sessions,
                )
    finally:
        await engine.dispose()


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
    """Turn queued webhook deliveries into syncs. The body arrives with the webhook task."""
    return 0
