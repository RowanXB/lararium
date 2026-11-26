"""lararium.restore_check, which the restore rehearsal runs on the restored database."""

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text

from lararium import restore_check
from lararium.modules.finance.ledger import PostingInput, TransactionInput, post_transaction

ALEMBIC_INI = Path(__file__).parents[2] / "alembic.ini"
WHEN = datetime(2026, 3, 1, 12, tzinfo=UTC)


async def test_a_balanced_ledger_has_no_problems(session, run, make_account, gbp) -> None:
    a, b = await make_account(name="A"), await make_account(name="B")
    await run(
        post_transaction,
        TransactionInput(
            occurred_at=WHEN,
            description="x",
            postings=[
                PostingInput(a.id, gbp.id, Decimal("-1")),
                PostingInput(b.id, gbp.id, Decimal("1")),
            ],
        ),
    )
    assert await restore_check.ledger_problems(session) == []


async def test_an_unbalanced_transaction_is_reported(session, run, make_account, gbp) -> None:
    a, b = await make_account(name="A"), await make_account(name="B")
    tx = await run(
        post_transaction,
        TransactionInput(
            occurred_at=WHEN,
            description="x",
            postings=[
                PostingInput(a.id, gbp.id, Decimal("-1")),
                PostingInput(b.id, gbp.id, Decimal("1")),
            ],
        ),
    )
    await session.execute(
        text("update finance.postings set quantity = 2 where transaction_id = :t and quantity = 1"),
        {"t": tx.id},
    )
    assert await restore_check.ledger_problems(session) == [f"1 unbalanced transactions: {tx.id}"]


def test_long_id_lists_are_cut_short() -> None:
    ids = [uuid4() for _ in range(restore_check.SHOWN_IDS + 3)]
    line = restore_check._ids("things", ids)  # pyright: ignore[reportPrivateUsage]
    assert line.startswith(f"{len(ids)} things: {ids[0]}")
    assert line.endswith(" and 3 more")
    assert str(ids[-1]) not in line


def test_a_revision_behind_the_head_is_reported(owner_url: str, migrated: None) -> None:
    heads = restore_check.code_heads(ALEMBIC_INI)
    engine = create_engine(owner_url)
    try:
        with engine.connect() as connection:
            assert restore_check.migration_problem(connection, heads) is None
            connection.execute(text("update alembic_version set version_num = '0001'"))
            problem = restore_check.migration_problem(connection, heads)
            assert problem == f"migration revision ['0001'] is not the code's head {sorted(heads)}"
            connection.rollback()
    finally:
        engine.dispose()


async def test_check_reads_a_migrated_database(owner_url: str, migrated: None) -> None:
    assert await restore_check.check(owner_url, ALEMBIC_INI) == []


def test_main_needs_the_database_url(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    monkeypatch.delenv("LARARIUM_MIGRATION_DATABASE_URL", raising=False)
    assert restore_check.main() == 2
    assert "LARARIUM_MIGRATION_DATABASE_URL" in capsys.readouterr().err
