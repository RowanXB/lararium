from logging.config import fileConfig

from alembic import context
from sqlalchemy import create_engine, pool

from financial_gate.config import get_settings

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name, disable_existing_loggers=False)


def database_url() -> str:
    url = config.get_main_option("sqlalchemy.url") or get_settings().migration_database_url
    if not url:
        raise RuntimeError("Set FG_MIGRATION_DATABASE_URL (or sqlalchemy.url) to run migrations")
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
