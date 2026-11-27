"""Migration 0046: the account_family kind, which never has an account, and the typed Notes
evidence (account family identifier design §5.9): transactions.outside_family and
import_rows.family_move."""

import importlib.util
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import Connection, create_engine, exc, select, text

from lararium.modules.finance import models
from lararium.modules.finance.identifiers import family_identifier, register_card_merchant
from lararium.modules.finance.ledger import (
    CounterpartyFields,
    PostingInput,
    TransactionInput,
    post_transaction,
)
from lararium.modules.finance.system_accounts import get_or_create_system_account

MIGRATION = (
    Path(__file__).parents[2]
    / "migrations"
    / "versions"
    / "0046_finance_account_family_identifier.py"
)


def _migration() -> Any:
    spec = importlib.util.spec_from_file_location(MIGRATION.stem, MIGRATION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _insert(connection: Connection, kind: str, value: str, account_id: str | None = None) -> None:
    connection.execute(
        text(
            "insert into finance.account_identifiers (kind, value, account_id, created_by)"
            " values (:kind, :value, :account_id, 'test')"
        ),
        {"kind": kind, "value": value, "account_id": account_id},
    )


def _rejected(connection: Connection, statement: str, **params: Any) -> None:
    savepoint = connection.begin_nested()
    with pytest.raises(exc.IntegrityError):
        connection.execute(text(statement), params)
    savepoint.rollback()


def _columns(connection: Connection) -> dict[tuple[str, str], tuple[str, str, str | None]]:
    """(table, column): (type, nullable, default) of the two Notes columns, where they exist."""
    rows = connection.execute(
        text(
            "select table_name, column_name, data_type, is_nullable, column_default"
            " from information_schema.columns where table_schema = 'finance'"
            " and (table_name, column_name) in"
            " (('transactions', 'outside_family'), ('import_rows', 'family_move'))"
        )
    )
    return {(row[0], row[1]): (row[2], row[3], row[4]) for row in rows}


def test_0046_and_its_downgrade(owner_url: str, migrated: None) -> None:
    engine = create_engine(owner_url)
    try:
        with engine.connect() as connection:
            assert _columns(connection) == {
                ("transactions", "outside_family"): ("boolean", "NO", "false"),
                ("import_rows", "family_move"): ("boolean", "YES", None),
            }
            account_id = connection.execute(
                text(
                    "insert into finance.accounts (name, ledger_type, subtype, balance_mode,"
                    " source_kind, owner_entity_id)"
                    " values ('Family', 'asset', 'bank', 'hybrid', 'manual',"
                    "  (select id from core.entities where is_self)) returning id"
                )
            ).scalar_one()
            _insert(connection, "account_family", "trading212")
            _rejected(
                connection,
                "insert into finance.account_identifiers (kind, value, account_id, created_by)"
                " values ('account_family', 'other', :account_id, 'test')",
                account_id=account_id,
            )
            identifier_id = connection.execute(
                text("select id from finance.account_identifiers where kind = 'account_family'")
            ).scalar_one()
            _rejected(
                connection,
                "insert into finance.account_identifier_holders (identifier_id, kind, account_id)"
                " values (:identifier_id, 'account_family', :account_id)",
                identifier_id=identifier_id,
                account_id=account_id,
            )

            with Operations.context(MigrationContext.configure(connection)):
                _migration().downgrade()
            left = connection.execute(
                text(
                    "select count(*) from finance.account_identifiers where kind = 'account_family'"
                )
            ).scalar_one()
            assert left == 0
            assert _columns(connection) == {}
            _rejected(
                connection,
                "insert into finance.account_identifiers (kind, value, created_by)"
                " values ('account_family', 'trading212', 'test')",
            )
            connection.rollback()
    finally:
        engine.dispose()


async def test_downgrading_0046_first_clears_the_transactions_naming_a_family(
    session, run, make_account, gbp
) -> None:
    """Its downgrade clears the counterparty identifier of a transaction naming a family, then
    deletes the row; a card merchant's stays."""
    account = await make_account(balance_mode="hybrid")
    unassigned = await run(get_or_create_system_account, "unassigned")
    family_id = await run(family_identifier, "trading212")
    merchant = await run(register_card_merchant, "merch_MIGRATION46", [account.id])
    posted = []
    for identifier_id in (family_id, merchant.id):
        posted.append(
            await run(
                post_transaction,
                TransactionInput(
                    occurred_at=datetime(2026, 11, 1, tzinfo=UTC),
                    description="Move",
                    postings=[
                        PostingInput(account.id, gbp.id, Decimal(10)),
                        PostingInput(unassigned, gbp.id, Decimal(-10)),
                    ],
                    counterparty=CounterpartyFields(identifier_id=identifier_id),
                ),
            )
        )

    for statement in _migration().DOWNGRADE[:2]:
        await session.execute(text(statement))

    t, i = models.Transaction, models.AccountIdentifier
    assert {
        row.id: row.counterparty_identifier_id
        for row in await session.execute(
            select(t.id, t.counterparty_identifier_id).where(t.id.in_([p.id for p in posted]))
        )
    } == {posted[0].id: None, posted[1].id: merchant.id}
    assert list(await session.scalars(select(i.id).where(i.id.in_([family_id, merchant.id])))) == [
        merchant.id
    ]
