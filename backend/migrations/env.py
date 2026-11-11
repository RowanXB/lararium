import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import create_engine, pool

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name, disable_existing_loggers=False)


def database_url() -> str:
    # Read the variable directly: full Settings would also demand the app's runtime variables.
    url = config.get_main_option("sqlalchemy.url") or os.environ.get("FG_MIGRATION_DATABASE_URL")
    if not url:
        raise RuntimeError(
            "Set FG_MIGRATION_DATABASE_URL (or sqlalchemy.url in the Alembic config)"
            " to run migrations"
        )
    return url


def run_migrations_offline() -> None:
    context.configure(url=database_url(), literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    engine = create_engine(database_url(), poolclass=pool.NullPool)
    try:
        with engine.connect() as connection:
            context.configure(connection=connection)
            with context.begin_transaction():
                context.run_migrations()
    finally:
        engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
