"""Migration 0047: the income kind column and the owner categories table."""

import importlib.util
from pathlib import Path
from typing import Any

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import Connection, create_engine, text

MIGRATION = (
    Path(__file__).parents[2] / "migrations" / "versions" / "0047_finance_income_categories.py"
)


def _migration() -> Any:
    spec = importlib.util.spec_from_file_location(MIGRATION.stem, MIGRATION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _present(connection: Connection) -> tuple[int, int]:
    """(income_kind columns, owner categories tables)."""
    column = connection.execute(
        text(
            "select count(*) from information_schema.columns where table_schema = 'finance'"
            " and table_name = 'transactions' and column_name = 'income_kind'"
        )
    ).scalar_one()
    table = connection.execute(
        text(
            "select count(*) from information_schema.tables where table_schema = 'finance'"
            " and table_name = 'transaction_owner_categories'"
        )
    ).scalar_one()
    return column, table


def test_0047_and_its_downgrade(owner_url: str, migrated: None) -> None:
    engine = create_engine(owner_url)
    try:
        with engine.connect() as connection:
            assert _present(connection) == (1, 1)
            with Operations.context(MigrationContext.configure(connection)):
                _migration().downgrade()
            assert _present(connection) == (0, 0)
            with Operations.context(MigrationContext.configure(connection)):
                _migration().upgrade()
            assert _present(connection) == (1, 1)
            connection.rollback()
    finally:
        engine.dispose()
