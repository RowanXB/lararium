"""Migration 0045: the references table goes, and a uk_bank_account may have holder rows."""

import importlib.util
from pathlib import Path
from typing import Any

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import Connection, create_engine, select, text

from lararium.modules.finance import models
from lararium.modules.finance.identifiers import register_card_merchant, register_holders

MIGRATION = (
    Path(__file__).parents[2] / "migrations" / "versions" / "0045_finance_shared_payee_holders.py"
)


def _migration() -> Any:
    spec = importlib.util.spec_from_file_location(MIGRATION.stem, MIGRATION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _schema(connection: Connection) -> tuple[int, str | None, str]:
    """(references tables, the holders' kind default, the holders' kind CHECK)."""
    tables = connection.execute(
        text(
            "select count(*) from information_schema.tables where table_schema = 'finance'"
            " and table_name = 'account_identifier_references'"
        )
    ).scalar_one()
    default = connection.execute(
        text(
            "select column_default from information_schema.columns"
            " where table_schema = 'finance' and table_name = 'account_identifier_holders'"
            " and column_name = 'kind'"
        )
    ).scalar_one()
    check = connection.execute(
        text(
            "select pg_get_constraintdef(oid) from pg_constraint"
            " where conname = 'account_identifier_holders_kind_check'"
        )
    ).scalar_one()
    return tables, default, check


def test_0045_and_its_downgrade(owner_url: str, migrated: None) -> None:
    engine = create_engine(owner_url)
    try:
        with engine.connect() as connection:
            tables, default, check = _schema(connection)
            assert (tables, default) == (0, None)
            assert "uk_bank_account" in check and "card_merchant" in check
            with Operations.context(MigrationContext.configure(connection)):
                _migration().downgrade()
            tables, default, check = _schema(connection)
            assert (tables, default) == (1, "'card_merchant'::text")
            assert "uk_bank_account" not in check
            with Operations.context(MigrationContext.configure(connection)):
                _migration().upgrade()
            tables, default, check = _schema(connection)
            assert (tables, default) == (0, None)
            assert "uk_bank_account" in check
            connection.rollback()
    finally:
        engine.dispose()


async def test_downgrading_0045_first_deletes_the_bank_accounts_holders(
    session, run, make_account
) -> None:
    """Its downgrade deletes the bank accounts' holder rows first, so the narrower CHECK can
    be restored; a card merchant's stay."""
    account = await make_account(balance_mode="hybrid")
    await run(register_holders, "uk_bank_account", "11-22-33 87654321", [account.id])
    merchant = await run(register_card_merchant, "merch_MIGRATION41", [account.id])

    delete, *_ = _migration().DOWNGRADE
    await session.execute(text(delete))

    h = models.AccountIdentifierHolder
    assert list(await session.execute(select(h.identifier_id, h.kind))) == [
        (merchant.id, "card_merchant")
    ]
