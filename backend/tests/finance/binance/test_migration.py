"""Migration 0030 goes down to 0029 and up again, taking what it added and leaving the rest;
migration 0031 (`never_transfer`) likewise down to 0030, 0032 (the reconciliation
differences and the account report times) down to 0031, and 0033 (the daily bars and the
CoinGecko ids) down to 0032. Each table a migration creates is checked on its own."""

import re
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import Connection, create_engine, text

BACKEND = Path(__file__).parents[3]
VERSIONS = BACKEND / "migrations" / "versions"


def _created(revision: str) -> list[str]:
    """The tables (in `finance`) the migration creates, in its order."""
    [path] = VERSIONS.glob(f"{revision}_*.py")
    return re.findall(r"CREATE TABLE finance\.(\w+)", path.read_text())


def _present(connection: Connection, tables: list[str]) -> dict[str, bool]:
    return {
        table: connection.execute(
            text("select to_regclass(:name) is not null"), {"name": f"finance.{table}"}
        ).scalar()
        is True
        for table in tables
    }


def _all(tables: list[str], present: bool) -> dict[str, bool]:
    return dict.fromkeys(tables, present)


def _config(owner_url: str) -> Config:
    config = Config(str(BACKEND / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", owner_url)
    return config


def test_each_migration_creates_its_own_tables() -> None:
    assert len(_created("0030")) == 44
    assert "binance_assets_seen" in _created("0030")
    assert _created("0031") == []
    assert _created("0032") == ["binance_account_reports", "binance_reconciliation_differences"]
    assert _created("0033") == ["binance_klines", "binance_coingecko_ids"]


def test_migration_round_trip(owner_url: str, migrated: None) -> None:
    config = _config(owner_url)
    core, later = _created("0030"), [*_created("0032"), *_created("0033")]
    engine = create_engine(owner_url)
    try:
        with engine.begin() as connection:
            assert _present(connection, core + later) == _all(core + later, True)
            connection.execute(
                text(
                    "insert into finance.account_identifiers (kind, value, created_by)"
                    " values ('chain_address', '0x11111111111111111111', 'test')"
                )
            )
        try:
            command.downgrade(config, "0029")
            with engine.begin() as connection:
                assert _present(connection, core + later) == _all(core + later, False)
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
            assert _present(connection, core + later) == _all(core + later, True)
    finally:
        engine.dispose()


_NEVER_TRANSFER = (
    "select count(*) from information_schema.columns where table_schema = 'finance'"
    " and table_name = 'transactions' and column_name = 'never_transfer'"
)


def test_never_transfer_migration_round_trip(owner_url: str, migrated: None) -> None:
    config = _config(owner_url)
    core = _created("0030")
    engine = create_engine(owner_url)
    try:
        try:
            command.downgrade(config, "0030")
            with engine.begin() as connection:
                assert connection.execute(text(_NEVER_TRANSFER)).scalar() == 0
                assert _present(connection, core) == _all(core, True)
        finally:
            command.upgrade(config, "head")
        with engine.begin() as connection:
            assert connection.execute(text(_NEVER_TRANSFER)).scalar() == 1
    finally:
        engine.dispose()


def test_reconcile_migration_round_trip(owner_url: str, migrated: None) -> None:
    config = _config(owner_url)
    tables = _created("0032")
    engine = create_engine(owner_url)
    try:
        try:
            command.downgrade(config, "0031")
            with engine.begin() as connection:
                assert _present(connection, tables) == _all(tables, False)
                assert connection.execute(text(_NEVER_TRANSFER)).scalar() == 1
        finally:
            command.upgrade(config, "head")
        with engine.begin() as connection:
            assert _present(connection, tables) == _all(tables, True)
    finally:
        engine.dispose()


def test_prices_migration_round_trip(owner_url: str, migrated: None) -> None:
    config = _config(owner_url)
    tables, before = _created("0033"), _created("0032")
    engine = create_engine(owner_url)
    try:
        try:
            command.downgrade(config, "0032")
            with engine.begin() as connection:
                assert _present(connection, tables) == _all(tables, False)
                assert _present(connection, before) == _all(before, True)
        finally:
            command.upgrade(config, "head")
        with engine.begin() as connection:
            assert _present(connection, tables) == _all(tables, True)
    finally:
        engine.dispose()
