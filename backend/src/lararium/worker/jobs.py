"""Worker jobs: syncing connections, waiting for a new one's approval, checking transfers
between the owner's accounts, and the periodic tasks that schedule them."""

import asyncio
import logging
from collections.abc import AsyncGenerator, Awaitable, Callable, Mapping
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
    SYNC_REQUESTED_KEY,
    ConnectionStatus,
    get_connection,
    record_sync_failure,
    record_sync_success,
    settle_sync_request,
    transition_status,
)
from lararium.core.connectors import BACKFILL_FAILED, SyncContext, WebhookTarget
from lararium.core.documents import LocalDocumentStore
from lararium.core.errors import CredentialRejectedError, DisplayableError
from lararium.core.json_types import JSONValue
from lararium.core.models import Connection
from lararium.core.notify import Notifier, notifier_from_settings
from lararium.core.secrets import Secret
from lararium.db import create_engine, make_sessionmaker
from lararium.modules import registry
from lararium.modules.finance.ecb import run_fetch_ecb_rates
from lararium.modules.finance.monzo.client import MonzoApiError
from lararium.modules.finance.monzo.webhooks import process_inbox
from lararium.modules.finance.transfers import (
    due_notifications,
    heal_transfers,
    lock_transfers,
    mark_notified,
)
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
# Monzo serves all history for this long after the owner approves.
FULL_HISTORY_WINDOW = timedelta(minutes=5)
HTTP_TIMEOUT_SECONDS = 30
APPROVAL_EXPIRED = "Approve the Monzo request in the app within 4 minutes, then authorise again"
TRANSFERS_ACTOR = Actor("system", "transfers")


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
    seen_request = connection.config.get(SYNC_REQUESTED_KEY)
    requested = seen_request is not None

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
                    documents=LocalDocumentStore(settings.document_dir),
                    requested=requested,
                    coingecko_api_key=_coingecko_key(settings),
                    esplora_url=settings.esplora_url,
                )
            )
            await record_sync_success(ctx, connection_id, cursor)
            if requested:
                await settle_sync_request(ctx, connection_id, seen_request)
            from_ = ("authorising",) if backfill else ("failing",)
            moved = await transition_status(
                ctx, connection_id, "healthy", only_from=from_, authorised_at=backfill_for
            )
            recovered = moved == "failing"
    except Exception as exc:
        # command_context has rolled the failed sync back; record the failure on its own.
        rejected = isinstance(exc, CredentialRejectedError)
        error = _describe(exc)[:MAX_ERROR_LENGTH]
        # The description only (a message written to be shown, a Monzo error's status and
        # code, else the type), never other exceptions' text, which may quote a request.
        logger.warning("sync of connection %s failed: %s", connection_id, error)
        backfill_failed = backfill and not rejected
        async with command_context(session, actor, "connection.sync_failed", args) as ctx:
            # The status first, and the failure only where it still applies: a sync whose
            # connection was disabled, or authorised again, meanwhile records nothing, not
            # even its error, which would show on the new authorisation while it waits.
            moved = await transition_status(
                ctx,
                connection_id,
                "needs_reauth" if rejected or backfill else "failing",
                only_from=allowed,
                authorised_at=backfill_for,
            )
            failures = 0
            if moved is not None:
                failures = await record_sync_failure(
                    ctx, connection_id, BACKFILL_FAILED if backfill_failed else error
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

    # Matching runs after the sync has committed, in a transaction of its own: what the sync
    # posted already sits in In transit or Unassigned as it should (Decision 9).
    await _heal(session, args)
    if recovered:
        await _notify(notifier, f"{connection.display_name} has recovered and is syncing again.")


async def _heal(session: AsyncSession, args: Mapping[str, JSONValue]) -> None:
    """heal_transfers in a command of its own; a failure is logged, never raised: the next
    heal (after a sync, or check_transfers) repairs it, and the ledger is right meanwhile.
    `args`: what the audit row says it followed (a sync's connection, say)."""
    try:
        async with command_context(session, TRANSFERS_ACTOR, "finance.heal_transfers", args) as ctx:
            await heal_transfers(ctx)
    except Exception as exc:
        # The type only: the text may quote the owner's data (a bank account, say).
        logger.error("healing transfers failed: %s", type(exc).__name__)


# More transfers due in one check than this are reported in one summary message.
MAX_TRANSFER_MESSAGES = 3


async def run_check_transfers(
    session: AsyncSession,
    *,
    notifier: Notifier,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> None:
    """Heal, then tell the owner once about each transfer that is overdue or needs review:
    a message each, or one summary when there are more than MAX_TRANSFER_MESSAGES (the first
    check after deploy may find a whole history due).

    The messages are read and marked under the matching lock, so a heal running meanwhile
    can't reopen a transfer between them (a reopened one keeps its notified_at). They are
    sent only once that has committed and the lock is free: a slow notifier must not hold
    up every heal. A message lost to a failed send, or to a crash before it is sent, is not
    sent again."""
    await _heal(session, {})
    async with command_context(session, TRANSFERS_ACTOR, "finance.notify_transfers", {}) as ctx:
        await lock_transfers(ctx)
        due = await due_notifications(session, now())
        await mark_notified(ctx, [transfer_id for transfer_id, _ in due])
    messages = [text for _, text in due]
    if len(messages) > MAX_TRANSFER_MESSAGES:
        messages = [f"{len(messages)} transfers need your attention: see Transfers"]
    for text in messages:
        await _notify(notifier, text)


def _describe(exc: Exception) -> str:
    """What last_error (and the owner's notification) says about a failed sync.

    Raw exception text can quote anything, an upstream response included, so only messages
    written to be shown are: a rejection's, a DisplayableError's, and a MonzoApiError's (its
    status and code).
    """
    if isinstance(exc, CredentialRejectedError | DisplayableError):
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


def _coingecko_key(settings: Settings) -> Secret | None:
    key = settings.coingecko_api_key
    return None if key is None else Secret(key.get_secret_value())


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
    # The authorisation this job has asked Monzo about before its deadline, if any.
    polled_in_time: str | None = None

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
        deadline = (
            datetime.fromisoformat(authorised_at) + APPROVAL_WINDOW if authorised_at else None
        )
        # A job that asked before the deadline asks once more after it, however long its last
        # check and wait took: an approval given since still counts, as Monzo's full-history
        # window runs from the approval. One that didn't (run again after its worker died,
        # say) ends without asking, so it doesn't backfill late.
        expired = deadline is None or now() >= deadline
        if expired and polled_in_time != authorised_at:
            await session.rollback()
            if await _end_approval(
                session, connection_id, actor, args, APPROVAL_EXPIRED, authorised_at
            ):
                await _notify(notifier, f"{connection.display_name}: {APPROVAL_EXPIRED}")
            return
        if not expired:
            polled_in_time = authorised_at
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
                    esplora_url=settings.esplora_url,
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
        if expired:
            if await _end_approval(
                session, connection_id, actor, args, APPROVAL_EXPIRED, authorised_at
            ):
                await _notify(notifier, f"{connection.display_name}: {APPROVAL_EXPIRED}")
            return
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
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_SECONDS) as http:
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


@procrastinate_app.periodic(cron="*/30 * * * *")
@procrastinate_app.task(name="check_transfers", queueing_lock="check_transfers")
async def check_transfers(timestamp: int) -> None:
    settings = get_settings()
    async with _job_resources(settings) as (http, sessions), sessions() as session:
        await run_check_transfers(session, notifier=notifier_from_settings(settings, http))


# The ECB publishes its reference rates around 16:00 CET.
@procrastinate_app.periodic(cron="30 16 * * *")
@procrastinate_app.task(name="fetch_ecb_rates", queueing_lock="fetch_ecb_rates")
async def fetch_ecb_rates(timestamp: int) -> int:
    settings = get_settings()
    async with _job_resources(settings) as (http, sessions):
        written = await run_fetch_ecb_rates(sessions, http, datetime.now(UTC).date())
    logger.info("stored %d ECB rates", written)
    return written


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


# Every minute: a stalled job holds its connection's sync lock until then, and an approval
# job waiting behind it has only 4 minutes from its authorisation.
@procrastinate_app.periodic(cron="* * * * *")
@procrastinate_app.task(name="retry_stalled_jobs", queueing_lock="retry_stalled_jobs")
async def retry_stalled_jobs(timestamp: int) -> int:
    retried = await retry_stalled(procrastinate_app.job_manager)
    if retried:
        logger.warning("retried %d jobs left running by a stopped worker", retried)
    return retried
