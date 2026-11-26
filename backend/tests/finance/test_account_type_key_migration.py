"""Migration 0038: finance.accounts.type_key, filled for Trading 212 accounts."""

import importlib.util
from pathlib import Path
from typing import Any

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, text

from lararium.core.connections import create_connection
from lararium.modules.finance.accounts import create_account, get_account

MIGRATION = (
    Path(__file__).parents[2] / "migrations" / "versions" / "0038_finance_account_type_key.py"
)


def _migration() -> Any:
    spec = importlib.util.spec_from_file_location(MIGRATION.stem, MIGRATION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def _connected(run, connector: str, config: dict[str, Any]):
    connection = await run(
        lambda ctx: create_connection(
            ctx, connector=connector, display_name=connector, config=config
        )
    )
    return await run(
        create_account,
        name=f"{connector} {config}",
        ledger_type="asset",
        subtype="broker",
        balance_mode="transactions",
        source_connection_id=connection.id,
    )


async def test_upgrade_fills_trading212_accounts_only(session, run, make_account) -> None:
    invest = await _connected(run, "trading212", {"account_kind": "invest"})
    isa = await _connected(run, "trading212", {"account_kind": "isa"})
    monzo = await _connected(run, "monzo", {"account_kind": "invest"})
    manual = await make_account()
    await session.execute(text("update finance.accounts set type_key = NULL"))

    await session.execute(text(_migration().BACKFILL))

    assert (await get_account(session, invest.id)).type_key == "trading212.invest"
    assert (await get_account(session, isa.id)).type_key == "trading212.stocks_isa"
    assert (await get_account(session, monzo.id)).type_key is None
    assert (await get_account(session, manual.id)).type_key is None


def test_downgrade_drops_the_column(owner_url: str, migrated: None) -> None:
    engine = create_engine(owner_url)
    try:
        with engine.connect() as connection:
            query = text(
                "select count(*) from information_schema.columns"
                " where table_schema = 'finance' and table_name = 'accounts'"
                " and column_name = 'type_key'"
            )
            assert connection.execute(query).scalar_one() == 1
            with Operations.context(MigrationContext.configure(connection)):
                _migration().downgrade()
            assert connection.execute(query).scalar_one() == 0
            connection.rollback()
    finally:
        engine.dispose()
