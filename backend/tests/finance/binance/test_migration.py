"""Migration 0030 goes down to 0029 and up again, taking what it added and leaving the rest;
migration 0031 (`never_transfer`) likewise down to 0030, and 0032 (the reconciliation
differences) down to 0031."""

from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text

BACKEND = Path(__file__).parents[3]
_TABLES = "select count(*) from pg_class where relname like 'binance\\_%' and relkind = 'r'"


def test_migration_round_trip(owner_url: str, migrated: None) -> None:
    config = Config(str(BACKEND / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", owner_url)
    engine = create_engine(owner_url)
    try:
        with engine.begin() as connection:
            before = connection.execute(text(_TABLES)).scalar()
            connection.execute(
                text(
                    "insert into finance.account_identifiers (kind, value, created_by)"
                    " values ('chain_address', '0x11111111111111111111', 'test')"
                )
            )
        assert before and before > 30
        try:
            command.downgrade(config, "0029")
            with engine.begin() as connection:
                assert connection.execute(text(_TABLES)).scalar() == 0
                gone = connection.execute(
                    text(
                        "select count(*) from finance.account_identifiers"
                        " where kind = 'chain_address'"
                    )
                )
                assert gone.scalar() == 0
        finally:
            command.upgrade(config, "head")
        with engine.begin() as connection:
            assert connection.execute(text(_TABLES)).scalar() == before
    finally:
        engine.dispose()


_NEVER_TRANSFER = (
    "select count(*) from information_schema.columns where table_schema = 'finance'"
    " and table_name = 'transactions' and column_name = 'never_transfer'"
)


def test_never_transfer_migration_round_trip(owner_url: str, migrated: None) -> None:
    config = Config(str(BACKEND / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", owner_url)
    engine = create_engine(owner_url)
    try:
        try:
            command.downgrade(config, "0030")
            with engine.begin() as connection:
                assert connection.execute(text(_NEVER_TRANSFER)).scalar() == 0
        finally:
            command.upgrade(config, "head")
        with engine.begin() as connection:
            assert connection.execute(text(_NEVER_TRANSFER)).scalar() == 1
    finally:
        engine.dispose()


_DIFFERENCES = "select to_regclass('finance.binance_reconciliation_differences') is not null"


def test_reconcile_migration_round_trip(owner_url: str, migrated: None) -> None:
    config = Config(str(BACKEND / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", owner_url)
    engine = create_engine(owner_url)
    try:
        try:
            command.downgrade(config, "0031")
            with engine.begin() as connection:
                assert connection.execute(text(_DIFFERENCES)).scalar() is False
                assert connection.execute(text(_NEVER_TRANSFER)).scalar() == 1
        finally:
            command.upgrade(config, "head")
        with engine.begin() as connection:
            assert connection.execute(text(_DIFFERENCES)).scalar() is True
    finally:
        engine.dispose()
