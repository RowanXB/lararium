"""No placeholder beside a row that may be the counterpart (account type catalog design §8.3
item 3).

M4 §5.8's import-coverage gate takes a counterpart found by amount and date on a manual
account only from the first day that account's imports list in full. When the gate is all
that keeps an identified record from such a candidate, a placeholder there would count the
money twice if the candidate is the real row: the record's transfer opens instead with no
placeholder and the review reason `not_listed_in_full`. When a later upload lists the days,
the pair forms by itself."""

import importlib.util
from datetime import UTC, date, datetime, timedelta
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
from lararium.modules.finance.transfers import (
    TransferRecord,
    candidates,
    is_overdue,
    list_transfers,
    load_transfer_settings,
    pair,
)

from .conftest import (
    A_IDENTIFIER,
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


async def test_the_review_offers_the_row_and_pairing_it_settles_the_review(
    session: AsyncSession,
    run: Run,
    ids: SystemIds,
    connected_bank: MakeConnected,
    connector_tx: ConnectorTx,
    imported_bank: ImportedBank,
    statement_row: StatementRow,
    heal: Heal,
) -> None:
    """The row the gate refused is the review's candidate of the same amount, so the
    Transfers page offers it with Pair; pairing it (the transfer's pair) matches the two
    and clears the review."""
    b, sent, row = await _setup(
        session, run, connected_bank, connector_tx, imported_bank, statement_row
    )
    await heal()
    transfer = await _transfer_of(session, sent)
    assert transfer.review_reason == "not_listed_in_full"

    found = await candidates(session, transfer.id)
    assert [(c.transaction_id, c.account_id, c.reason) for c in found] == [(row, b.id, "exact")]

    paired = await run(pair, transfer.id, row)

    assert (paired.status, paired.review_reason) == ("matched", None)
    assert (paired.out_transaction_id, paired.in_transaction_id) == (sent, row)
    assert await placeholders(session) == []
    balances = await balances_on(session)
    assert balances[b.id] == Decimal(100)
    assert balances.get(ids.in_transit, Decimal(0)) == 0
    await assert_consistent(session)
    assert await heal() == 0


async def test_a_review_not_listed_in_full_is_never_overdue(
    session: AsyncSession,
    run: Run,
    connected_bank: MakeConnected,
    connector_tx: ConnectorTx,
    imported_bank: ImportedBank,
    statement_row: StatementRow,
    heal: Heal,
) -> None:
    """It awaits an upload of a manual account, not a connection's report: however long it
    waits, it is not overdue (its review already puts it in Needs attention)."""
    _, sent, _ = await _setup(
        session, run, connected_bank, connector_tx, imported_bank, statement_row
    )
    await heal()
    transfer = await _transfer_of(session, sent)
    assert (transfer.status, transfer.review_reason) == ("awaiting", "not_listed_in_full")

    settings = await load_transfer_settings(session)
    later = datetime.now(UTC) + settings.overdue * 10
    assert not is_overdue(transfer, later, settings)


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


async def test_a_gated_row_beside_one_the_gate_takes_leaves_both_for_review(
    session: AsyncSession,
    run: Run,
    ids: SystemIds,
    connected_bank: MakeConnected,
    connector_tx: ConnectorTx,
    imported_bank: ImportedBank,
    statement_row: StatementRow,
    heal: Heal,
) -> None:
    """A's payment of 100 at WHEN names B. B's upload lists in full from WHEN's day for two
    days only (so it doesn't hold A's window), with two rows of 100 naming nobody: one
    giving only its date, which the gate refuses (A's window starts the day before), and one
    timed to the second 30 s after WHEN, which a timed receipt lets through (§8.3 item 4).
    The refused row is as plausible a counterpart: the payment is paired with neither, its
    review lists both, and no placeholder stands beside them."""
    a = await connected_bank("A")
    b = await imported_bank("B", identifier=B_IDENTIFIER)
    identifier = await run(get_or_create_identifier, "uk_bank_account", B_IDENTIFIER)
    sent = (await connector_tx(a, "-100", occurred_at=WHEN, identifier_id=identifier)).id
    listed = (WHEN.date(), WHEN.date() + timedelta(days=2))
    dated = await statement_row(b, "100", day=WHEN.date(), complete=listed)
    timed = await statement_row(
        b, "100", at=WHEN + timedelta(seconds=30), time_unit="second", complete=listed
    )
    await heal()

    transfer = await _transfer_of(session, sent)
    assert (transfer.status, transfer.review_reason) == ("awaiting", "several_candidates")
    assert transfer.in_transaction_id is None
    assert (transfer.placeholder_transaction_id, transfer.settlement_transaction_id) == (
        None,
        None,
    )
    assert await placeholders(session) == []
    found = await candidates(session, transfer.id)
    assert sorted((c.transaction_id, c.reason) for c in found) == sorted(
        [(dated, "exact"), (timed, "exact")]
    )
    balances = await balances_on(session)
    assert balances[b.id] == Decimal(200)  # the two rows, once each
    assert balances[ids.in_transit] == Decimal(100)
    await assert_consistent(session)
    assert await heal() == 0


async def test_a_gated_row_beside_one_of_another_amount_posts_no_placeholder(
    session: AsyncSession,
    run: Run,
    ids: SystemIds,
    registered: Any,
    connector_tx: ConnectorTx,
    imported_bank: ImportedBank,
    statement_row: StatementRow,
    heal: Heal,
) -> None:
    """A's payment of 100 names B; B's row of 99.30 names A (another amount, mutually
    identified) and its row of 100 names nobody on days no upload lists in full. The
    transfer is for review as amount_differs, the gated row is among its candidates, and no
    placeholder stands beside it."""
    a, a_id = await registered("A", A_IDENTIFIER)
    b = await imported_bank("B", identifier=B_IDENTIFIER)
    identifier = await run(get_or_create_identifier, "uk_bank_account", B_IDENTIFIER)
    sent = (await connector_tx(a, "-100", occurred_at=WHEN, identifier_id=identifier)).id
    net = await statement_row(b, "99.30", day=WHEN.date(), identifier_id=a_id)
    gated = await statement_row(b, "100", day=WHEN.date())
    await _list_no_full_day(session, b)
    await heal()

    transfer = await _transfer_of(session, sent)
    assert (transfer.status, transfer.review_reason) == ("awaiting", "amount_differs")
    assert (transfer.placeholder_transaction_id, transfer.settlement_transaction_id) == (
        None,
        None,
    )
    assert await placeholders(session) == []
    found = await candidates(session, transfer.id)
    assert [(c.transaction_id, c.reason) for c in found] == [
        (gated, "exact"),
        (net, "amount_differs"),
    ]
    assert (await balances_on(session))[b.id] == Decimal("199.30")
    await assert_consistent(session)
    assert await heal() == 0


async def test_heal_takes_the_placeholder_away_when_a_gated_row_arrives(
    session: AsyncSession,
    run: Run,
    registered: Any,
    connector_tx: ConnectorTx,
    imported_bank: ImportedBank,
    statement_row: StatementRow,
    heal: Heal,
) -> None:
    """As above, with B's row of 100 arriving after the transfer opened for review as
    amount_differs, with a placeholder: its review reason stays, but heal finds the
    placeholder beside a row that may be the counterpart and takes it away. Voiding the row
    brings the placeholder back."""
    a, a_id = await registered("A", A_IDENTIFIER)
    b = await imported_bank("B", identifier=B_IDENTIFIER)
    identifier = await run(get_or_create_identifier, "uk_bank_account", B_IDENTIFIER)
    sent = (await connector_tx(a, "-100", occurred_at=WHEN, identifier_id=identifier)).id
    await statement_row(b, "99.30", day=WHEN.date(), identifier_id=a_id)
    await _list_no_full_day(session, b)
    await heal()
    transfer = await _transfer_of(session, sent)
    assert (transfer.status, transfer.review_reason) == ("inferred", "amount_differs")

    gated = await statement_row(b, "100", day=WHEN.date())
    await _list_no_full_day(session, b)
    await heal()

    transfer = await _transfer_of(session, sent)
    assert (transfer.status, transfer.review_reason) == ("awaiting", "amount_differs")
    assert transfer.placeholder_transaction_id is None
    await assert_consistent(session)
    assert await heal() == 0

    await run(void_transaction, gated)
    await heal()

    transfer = await _transfer_of(session, sent)
    assert (transfer.status, transfer.review_reason) == ("inferred", "amount_differs")
    assert transfer.placeholder_transaction_id is not None
    await assert_consistent(session)
    assert await heal() == 0


async def test_a_row_the_gate_refused_another_sender_holds_back_its_one_candidate(
    session: AsyncSession,
    run: Run,
    ids: SystemIds,
    connected_bank: MakeConnected,
    connector_tx: ConnectorTx,
    imported_bank: ImportedBank,
    statement_row: StatementRow,
    heal: Heal,
) -> None:
    """B's uploads list in full from DAY for two days (so they hold no window), with two
    rows of 100 naming nobody, on DAY and four days later. S's payment of 100 on DAY names
    B: its window starts the day before, so the gate refuses it both rows. A's of 100 three
    days later names B too, and its gate takes the later row, its only candidate. That row
    may be S's: A is paired with neither, and its transfer waits for review with no
    placeholder beside the row, which it lists. When an upload lists from the day before
    DAY, the gate takes both rows for S: nothing is paired around either (S has two
    candidates, the later row two)."""
    a, s = await connected_bank("A"), await connected_bank("S")
    b = await imported_bank("B", identifier=B_IDENTIFIER)
    identifier = await run(get_or_create_identifier, "uk_bank_account", B_IDENTIFIER)
    noon = datetime.combine(DAY, datetime.min.time(), UTC) + timedelta(hours=12)
    listed = (DAY, DAY + timedelta(days=2))
    await statement_row(b, "100", day=DAY, complete=listed)
    later = await statement_row(b, "100", day=DAY + timedelta(days=4), complete=listed)
    sent_s = (await connector_tx(s, "-100", occurred_at=noon, identifier_id=identifier)).id
    sent_a = (
        await connector_tx(
            a, "-100", occurred_at=noon + timedelta(days=3), identifier_id=identifier
        )
    ).id
    await heal()

    transfer_s = await _transfer_of(session, sent_s)
    assert (transfer_s.status, transfer_s.review_reason) == ("awaiting", "not_listed_in_full")
    transfer_a = await _transfer_of(session, sent_a)
    assert (transfer_a.status, transfer_a.review_reason) == ("awaiting", "several_candidates")
    assert transfer_a.in_transaction_id is None
    assert transfer_a.placeholder_transaction_id is None
    assert await placeholders(session) == []
    found = await candidates(session, transfer_a.id)
    assert [(c.transaction_id, c.reason) for c in found] == [(later, "exact")]
    balances = await balances_on(session, DAY + timedelta(days=10))
    assert balances[b.id] == Decimal(200)  # the two rows, once each
    assert balances[ids.in_transit] == Decimal(200)
    await assert_consistent(session)
    assert await heal() == 0

    # A later upload lists in full from the day before DAY.
    before = DAY - timedelta(days=1)
    await statement_row(b, "-5", day=before, complete=(before, DAY + timedelta(days=2)))
    await heal()

    for sent in (sent_s, sent_a):
        transfer = await _transfer_of(session, sent)
        assert transfer.in_transaction_id is None
        assert transfer.review_reason == "several_candidates"
    await assert_consistent(session)
    assert await heal() == 0
