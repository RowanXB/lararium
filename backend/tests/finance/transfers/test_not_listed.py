"""No placeholder beside a row that may be the counterpart (account type catalog design §8.3
item 3).

M4 §5.8's import-coverage gate takes a counterpart found by amount and date on a manual
account only from the first day that account's imports list in full. When the gate is all
that keeps an identified record from such a candidate, a placeholder there would count the
money twice if the candidate is the real row: the record's transfer opens instead with no
placeholder and the review reason `not_listed_in_full`. When a later upload lists the days,
the pair forms by itself."""

import importlib.util
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from lararium.modules.finance import models
from lararium.modules.finance.accounts import AccountRecord
from lararium.modules.finance.identifiers import get_or_create_identifier
from lararium.modules.finance.ledger import void_transaction
from lararium.modules.finance.transfers import TransferRecord, list_transfers

from .conftest import (
    B_IDENTIFIER,
    DAY,
    WHEN,
    ConnectorTx,
    DuplicateRow,
    Heal,
    ImportedBank,
    MakeConnected,
    RowDetails,
    Run,
    StatementRow,
    SystemIds,
    assert_consistent,
    balances_on,
    placeholders,
)


async def _list_no_full_day(session: AsyncSession, account: AccountRecord) -> None:
    """The account's uploads list no day in full, as a History CSV of one row does."""
    b = models.ImportBatch
    await session.execute(
        update(b).where(b.account_id == account.id).values(complete_from=None, complete_to=None)
    )


async def _transfer_of(session: AsyncSession, record_id: UUID) -> TransferRecord:
    [transfer] = [
        t
        for t in await list_transfers(session)
        if record_id in (t.out_transaction_id, t.in_transaction_id)
    ]
    return transfer


async def _setup(
    session: AsyncSession,
    run: Run,
    connected_bank: MakeConnected,
    connector_tx: ConnectorTx,
    imported_bank: ImportedBank,
    statement_row: StatementRow,
    *,
    amount: str = "100",
    day: date = DAY,
) -> tuple[AccountRecord, UUID, UUID]:
    """A's bank transfer of 100 at WHEN names B, a manual account whose one upload holds a
    bank-transfer row naming nobody (`amount` on `day`) and lists no day in full. Returns B,
    A's record and B's row."""
    a = await connected_bank("A")
    b = await imported_bank("B", identifier=B_IDENTIFIER)
    identifier = await run(get_or_create_identifier, "uk_bank_account", B_IDENTIFIER)
    sent = await connector_tx(a, "-100", occurred_at=WHEN, identifier_id=identifier)
    row = await statement_row(b, amount, day=day)
    await _list_no_full_day(session, b)
    return b, sent.id, row


async def test_a_row_the_gate_refuses_opens_a_review_with_no_placeholder(
    session: AsyncSession,
    run: Run,
    ids: SystemIds,
    connected_bank: MakeConnected,
    connector_tx: ConnectorTx,
    imported_bank: ImportedBank,
    statement_row: StatementRow,
    duplicate_row: DuplicateRow,
    heal: Heal,
) -> None:
    """B's row of 100 is in A's window, but no upload lists its days in full: the transfer
    waits for review, B counts the row once, and A's 100 stays in In transit. A later upload
    listing those days in full pairs them."""
    b, sent, row = await _setup(
        session, run, connected_bank, connector_tx, imported_bank, statement_row
    )
    await heal()

    transfer = await _transfer_of(session, sent)
    assert (transfer.status, transfer.review_reason) == ("awaiting", "not_listed_in_full")
    assert (transfer.placeholder_transaction_id, transfer.settlement_transaction_id) == (
        None,
        None,
    )
    assert transfer.in_transaction_id is None
    assert await placeholders(session) == []
    balances = await balances_on(session)
    assert balances[b.id] == Decimal(100)  # the row alone
    assert balances[ids.in_transit] == Decimal(100)
    await assert_consistent(session)
    assert await heal() == 0

    # A later upload lists the days in full (the row again, as a duplicate).
    await duplicate_row(row, RowDetails())
    await heal()

    transfer = await _transfer_of(session, sent)
    assert (transfer.status, transfer.review_reason) == ("matched", None)
    assert transfer.in_transaction_id == row
    balances = await balances_on(session)
    assert balances[b.id] == Decimal(100)
    assert balances.get(ids.in_transit, Decimal(0)) == 0
    await assert_consistent(session)
    assert await heal() == 0


async def test_when_the_row_goes_the_placeholder_comes_back(
    session: AsyncSession,
    run: Run,
    connected_bank: MakeConnected,
    connector_tx: ConnectorTx,
    imported_bank: ImportedBank,
    statement_row: StatementRow,
    heal: Heal,
) -> None:
    """Voiding the row that held the review leaves no candidate: the next heal posts the
    placeholder, with no review."""
    _, sent, row = await _setup(
        session, run, connected_bank, connector_tx, imported_bank, statement_row
    )
    await heal()
    assert (await _transfer_of(session, sent)).review_reason == "not_listed_in_full"

    await run(void_transaction, row)
    await heal()

    transfer = await _transfer_of(session, sent)
    assert (transfer.status, transfer.review_reason) == ("inferred", None)
    assert transfer.placeholder_transaction_id is not None
    await assert_consistent(session)
    assert await heal() == 0


async def test_a_later_upload_holding_the_window_settles_it(
    session: AsyncSession,
    run: Run,
    connected_bank: MakeConnected,
    connector_tx: ConnectorTx,
    imported_bank: ImportedBank,
    statement_row: StatementRow,
    heal: Heal,
) -> None:
    """A later upload lists in full from the day after the row's to well after A's window:
    the row's day still isn't listed, but the window now ends on days B's statements list,
    so the transfer is settled before history, as one opened then would be."""
    b, sent, _ = await _setup(
        session, run, connected_bank, connector_tx, imported_bank, statement_row
    )
    await heal()
    assert (await _transfer_of(session, sent)).review_reason == "not_listed_in_full"

    later = DAY + timedelta(days=1)
    await statement_row(b, "-5", day=later, complete=(later, DAY + timedelta(days=60)))
    await heal()

    transfer = await _transfer_of(session, sent)
    assert (transfer.status, transfer.review_reason) == ("before_history", None)
    assert transfer.settlement_transaction_id is not None
    await assert_consistent(session)
    assert await heal() == 0


def _migration_0040() -> Any:
    path = (
        Path(__file__).parents[3]
        / "migrations"
        / "versions"
        / "0040_finance_transfer_review_not_listed.py"
    )
    spec = importlib.util.spec_from_file_location(path.stem, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def test_downgrading_0040_deletes_the_reviews_it_allows(
    session: AsyncSession,
    run: Run,
    ids: SystemIds,
    connected_bank: MakeConnected,
    connector_tx: ConnectorTx,
    imported_bank: ImportedBank,
    statement_row: StatementRow,
    heal: Heal,
) -> None:
    """0040's downgrade first deletes the transfers that are not_listed_in_full, so the
    narrower CHECK can be restored: their records stay in In transit, a leg of no
    transfer, which the code before it heals (it opens them again, inferred)."""
    _, sent, _ = await _setup(
        session, run, connected_bank, connector_tx, imported_bank, statement_row
    )
    await heal()
    assert (await _transfer_of(session, sent)).review_reason == "not_listed_in_full"

    delete, *_ = _migration_0040().DOWNGRADE
    await session.execute(text(delete))

    assert await list_transfers(session) == []
    assert (await balances_on(session))[ids.in_transit] == Decimal(100)


def test_0040_widens_the_check_and_its_downgrade_narrows_it(owner_url: str, migrated: None) -> None:
    """The CHECK takes not_listed_in_full after 0040, and not after its downgrade."""
    query = text(
        "select pg_get_constraintdef(oid) from pg_constraint"
        " where conname = 'transfers_review_reason_check'"
    )
    engine = create_engine(owner_url)
    try:
        with engine.connect() as connection:
            assert "not_listed_in_full" in connection.execute(query).scalar_one()
            with Operations.context(MigrationContext.configure(connection)):
                _migration_0040().downgrade()
            assert "not_listed_in_full" not in connection.execute(query).scalar_one()
            with Operations.context(MigrationContext.configure(connection)):
                _migration_0040().upgrade()
            assert "not_listed_in_full" in connection.execute(query).scalar_one()
            connection.rollback()
    finally:
        engine.dispose()


async def test_with_no_candidate_in_the_window_a_placeholder_is_posted(
    session: AsyncSession,
    run: Run,
    connected_bank: MakeConnected,
    connector_tx: ConnectorTx,
    imported_bank: ImportedBank,
    statement_row: StatementRow,
    heal: Heal,
) -> None:
    """B's row of 100 is three weeks later, outside A's window: as before, a placeholder."""
    _, sent, _ = await _setup(
        session,
        run,
        connected_bank,
        connector_tx,
        imported_bank,
        statement_row,
        day=DAY + timedelta(days=21),
    )
    await heal()

    transfer = await _transfer_of(session, sent)
    assert (transfer.status, transfer.review_reason) == ("inferred", None)
    assert transfer.placeholder_transaction_id is not None
    await assert_consistent(session)
    assert await heal() == 0


async def test_a_candidate_of_another_amount_changes_nothing(
    session: AsyncSession,
    run: Run,
    connected_bank: MakeConnected,
    connector_tx: ConnectorTx,
    imported_bank: ImportedBank,
    statement_row: StatementRow,
    heal: Heal,
) -> None:
    """B's row in the window is of 90: no evidence (Ruling 8), so a placeholder as before."""
    _, sent, _ = await _setup(
        session, run, connected_bank, connector_tx, imported_bank, statement_row, amount="90"
    )
    await heal()

    transfer = await _transfer_of(session, sent)
    assert (transfer.status, transfer.review_reason) == ("inferred", None)
    assert transfer.placeholder_transaction_id is not None
    await assert_consistent(session)
    assert await heal() == 0


async def test_a_transfer_the_account_s_statements_hold_is_settled_as_before(
    session: AsyncSession,
    run: Run,
    connected_bank: MakeConnected,
    connector_tx: ConnectorTx,
    imported_bank: ImportedBank,
    statement_row: StatementRow,
    heal: Heal,
) -> None:
    """B's upload lists in full from the day after A's window starts to well after it ends:
    the gate refuses B's row, but the window ends on days B's statements list, so the
    transfer is settled (before_history), with no placeholder and no review, as before."""
    a = await connected_bank("A")
    b = await imported_bank("B", identifier=B_IDENTIFIER)
    identifier = await run(get_or_create_identifier, "uk_bank_account", B_IDENTIFIER)
    sent = await connector_tx(a, "-100", occurred_at=WHEN, identifier_id=identifier)
    await statement_row(b, "100", day=DAY, complete=(DAY, DAY + timedelta(days=60)))
    await heal()

    transfer = await _transfer_of(session, sent.id)
    assert (transfer.status, transfer.review_reason) == ("before_history", None)
    assert transfer.placeholder_transaction_id is None
    await assert_consistent(session)
    assert await heal() == 0
