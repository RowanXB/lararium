"""The Procrastinate app: its jobs live in the `procrastinate` schema (migration 0009)."""

import os

from procrastinate import App, PsycopgConnector


def psycopg_dsn(database_url: str) -> str:
    """The SQLAlchemy URL as a plain libpq DSN."""
    return database_url.replace("postgresql+psycopg://", "postgresql://", 1)


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
