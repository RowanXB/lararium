"""The Procrastinate app: its jobs live in the `procrastinate` schema (migration 0009)."""

import os
from uuid import UUID

from procrastinate import App, PsycopgConnector
from procrastinate import exceptions as procrastinate_exceptions

from lararium.db import psycopg_dsn

# Procrastinate's SQL functions refer to its tables unqualified, hence the search_path.
# Nothing connects until the app is opened, so an unset URL only fails at worker start-up,
# where Settings reports it properly (see lararium.worker.__main__).
procrastinate_app = App(
    connector=PsycopgConnector(
        conninfo=psycopg_dsn(os.environ.get("LARARIUM_DATABASE_URL", "")),
        kwargs={"options": "-c search_path=procrastinate"},
    ),
    import_paths=["lararium.worker.jobs"],
)


def sync_lock(connection_id: UUID | str) -> str:
    """The lock every job that syncs a connection runs under, so they never overlap."""
    return f"sync:{connection_id}"


# Deferred by task name: the API can't import lararium.worker.jobs, which imports the modules
# whose routes defer these jobs.


async def defer_sync(connection_id: UUID | str) -> bool:
    """Queue a sync for the connection; False if one is already queued."""
    lock = sync_lock(connection_id)
    try:
        await procrastinate_app.configure_task(
            "sync_connection", allow_unknown=False, lock=lock, queueing_lock=lock
        ).defer_async(connection_id=str(connection_id))
    except procrastinate_exceptions.AlreadyEnqueued:
        return False
    return True


async def defer_await_approval(connection_id: UUID | str) -> bool:
    """Queue the wait for the owner's approval; False if one is already queued.

    It runs under the sync lock, so no sync overlaps the approval and its backfill.
    """
    try:
        await procrastinate_app.configure_task(
            "await_approval",
            allow_unknown=False,
            lock=sync_lock(connection_id),
            queueing_lock=f"approval:{connection_id}",
        ).defer_async(connection_id=str(connection_id))
    except procrastinate_exceptions.AlreadyEnqueued:
        return False
    return True
