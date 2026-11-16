"""Imports through M4's transfer matching (its design §5.8): an imported row that is the
other side of a known transfer replaces the transfer's placeholder instead of counting
twice, a rollback brings the placeholder back, and the statement's own bank account is
registered to the account it was imported into."""

from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import UUID

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from lararium.core.connections import create_connection
from lararium.modules.finance import models
from lararium.modules.finance.accounts import AccountRecord, create_account
from lararium.modules.finance.identifiers import (
    assign_identifier,
    get_or_create_identifier,
    list_identifiers,
)
from lararium.modules.finance.importing.profiles import ImportProfileRecord
from lararium.modules.finance.instruments import InstrumentRecord
from lararium.modules.finance.ledger import (
    CounterpartyFields,
    PostingInput,
    TransactionInput,
    upsert_connector_transaction,
)
from lararium.modules.finance.transfers import (
    find_transfer_violations,
    heal_transfers,
    list_transfers,
    reject,
    source_ref,
    transfer_contra,
)

from .staging_helpers import Commit, Rollback, Run, Stage, _balance_after, _csv_profile

SORT_CODE, ACCOUNT_NUMBER = "401234", "12345678"
HEADER = "Date,Type,Description,Amount,Balance,Sort Code,Account Number\n"
COLUMNS = {
    "date": "Date",
    "type": "Type",
    "description": ["Description"],
    "amount": "Amount",
    "balance": "Balance",
    "account_sort_code": "Sort Code",
    "account_number": "Account Number",
}
END = date(2026, 12, 31)
NOVEMBER = "01/11/2026,CR,Pay,500.00,500.00\n30/11/2026,VIS,Shop,-1.00,499.00\n"
# The 100 from A arrives on 11 Dec, a day December's statement lists in full.
DECEMBER = (
    "01/12/2026,VIS,Shop,-2.00,497.00\n11/12/2026,{type},From A,100.00,597.00\n"
    "31/12/2026,VIS,Shop,-1.00,596.00\n"
)


def _lines(rows: str) -> str:
    return HEADER + "".join(
        f"{line},{SORT_CODE},{ACCOUNT_NUMBER}\n" for line in rows.strip().split("\n")
    )


@pytest.fixture
async def profile(run: Run) -> ImportProfileRecord:
    return await _csv_profile(run, columns=COLUMNS, card_types=["VIS"])


@pytest.fixture
async def connected(run: Run) -> tuple[UUID, AccountRecord]:
    """A, an account with a connection of its own (Monzo, say)."""
    connection = await run(create_connection, connector="monzo", display_name="A", config={})
    account = await run(
        create_account,
        name="A",
        ledger_type="asset",
        subtype="bank",
        balance_mode="transactions",
        source_connection_id=connection.id,
    )
    return connection.id, account


async def _pay(
    session: AsyncSession,
    run: Run,
    gbp: InstrumentRecord,
    connected: tuple[UUID, AccountRecord],
    on: date = date(2026, 12, 10),
) -> None:
    """A pays 100 (on 10 Dec) to the bank account M's statement names, then matching runs."""
    connection_id, a = connected
    identifier = await run(
        get_or_create_identifier, "uk_bank_account", f"{SORT_CODE}:{ACCOUNT_NUMBER}"
    )
    contra = await transfer_contra(
        session,
        source=("connection", f"{connection_id}:tx_1"),
        own_account_id=a.id,
        counterparty_identifier_id=identifier,
        other_postings=0,
    )
    await run(
        upsert_connector_transaction,
        TransactionInput(
            occurred_at=datetime(on.year, on.month, on.day, 12, tzinfo=UTC),
            description="To M",
            origin="connector",
            connection_id=connection_id,
            external_id="tx_1",
            postings=[
                PostingInput(a.id, gbp.id, Decimal("-100")),
                PostingInput(contra, gbp.id, Decimal("100")),
            ],
            counterparty=CounterpartyFields(identifier_id=identifier, bank_transfer=True),
        ),
    )
    await run(heal_transfers)


async def _status(session: AsyncSession) -> str:
    [transfer] = await list_transfers(session)
    return transfer.status


async def test_statement_registers_its_own_bank_account(
    session: AsyncSession,
    stage: Stage,
    commit: Commit,
    profile: ImportProfileRecord,
    account: AccountRecord,
) -> None:
    await commit((await stage(profile, _lines(NOVEMBER))).id)
    [identifier] = await list_identifiers(session)
    assert (identifier.value, identifier.account_id) == (
        f"{SORT_CODE}:{ACCOUNT_NUMBER}",
        account.id,
    )


async def test_imported_row_replaces_the_placeholder_and_rollback_restores_it(
    session: AsyncSession,
    run: Run,
    gbp: InstrumentRecord,
    stage: Stage,
    commit: Commit,
    rollback: Rollback,
    profile: ImportProfileRecord,
    account: AccountRecord,
    connected: tuple[UUID, AccountRecord],
) -> None:
    await commit((await stage(profile, _lines(NOVEMBER))).id)
    await _pay(session, run, gbp, connected)
    assert await _status(session) == "inferred"  # a placeholder of +100 on M meanwhile
    assert await _balance_after(session, account.id, END) == Decimal("599")

    december = await commit((await stage(profile, _lines(DECEMBER.format(type="CR")))).id)
    assert await _status(session) == "matched"
    # One +100, the statement's own row: the ledger is the statement's closing balance.
    assert await _balance_after(session, account.id, END) == Decimal("596")
    assert await find_transfer_violations(session) == []

    await rollback(december.id)
    assert await _status(session) == "inferred"
    assert await _balance_after(session, account.id, END) == Decimal("599")
    assert await find_transfer_violations(session) == []


async def test_a_card_row_is_no_transfers_other_side(
    session: AsyncSession,
    run: Run,
    gbp: InstrumentRecord,
    stage: Stage,
    commit: Commit,
    profile: ImportProfileRecord,
    connected: tuple[UUID, AccountRecord],
) -> None:
    await commit((await stage(profile, _lines(NOVEMBER))).id)
    await _pay(session, run, gbp, connected)
    await commit((await stage(profile, _lines(DECEMBER.format(type="VIS")))).id)
    assert await _status(session) == "inferred"  # a card refund of 100 is not the transfer


async def test_the_owners_reject_follows_the_row_imported_again(
    session: AsyncSession,
    run: Run,
    gbp: InstrumentRecord,
    stage: Stage,
    commit: Commit,
    rollback: Rollback,
    profile: ImportProfileRecord,
    connected: tuple[UUID, AccountRecord],
) -> None:
    """Decisions are keyed by a row's stable reference, its dedupe key, which the same row
    keeps when imported again after a rollback."""
    await commit((await stage(profile, _lines(NOVEMBER))).id)
    await _pay(session, run, gbp, connected)
    december = await commit((await stage(profile, _lines(DECEMBER.format(type="CR")))).id)
    [transfer] = await list_transfers(session)
    assert transfer.in_transaction_id is not None
    reference = await source_ref(session, transfer.in_transaction_id)
    assert reference[0] == "import"
    await run(reject, transfer.id)

    await rollback(december.id)
    await commit((await stage(profile, _lines(DECEMBER.format(type="CR")))).id)
    r = models.ImportRow
    again = await session.scalar(
        select(r.transaction_id).where(r.status == "committed", r.description == "From A")
    )
    assert again is not None and again != transfer.in_transaction_id
    assert await source_ref(session, again) == reference
    assert [t.status for t in await list_transfers(session)] == ["rejected"]


async def test_a_row_past_the_days_imported_waits_for_the_next_statement(
    session: AsyncSession,
    run: Run,
    gbp: InstrumentRecord,
    stage: Stage,
    commit: Commit,
    profile: ImportProfileRecord,
    connected: tuple[UUID, AccountRecord],
) -> None:
    """Design §5.8: A's payment on 30 Dec may arrive on M up to 4 Jan, but an export's last
    day (31 Dec) may be cut short and the days after aren't imported, so the 100 on 31 Dec
    may be an unrelated receipt standing in for M's real row: not taken until January's
    statement is imported, which shows the days after."""
    await commit((await stage(profile, _lines(NOVEMBER))).id)
    await _pay(session, run, gbp, connected, on=date(2026, 12, 30))
    december = "01/12/2026,VIS,Shop,-2.00,497.00\n31/12/2026,CR,From A,100.00,597.00\n"
    await commit((await stage(profile, _lines(december))).id)
    assert await _status(session) == "inferred"

    january = "01/01/2027,VIS,Shop,-1.00,596.00\n10/01/2027,VIS,Shop,-1.00,595.00\n"
    await commit((await stage(profile, _lines(january))).id)
    assert await _status(session) == "matched"


async def test_a_transfer_at_the_boundary_of_back_to_back_exports_pairs(
    session: AsyncSession,
    run: Run,
    gbp: InstrumentRecord,
    stage: Stage,
    commit: Commit,
    profile: ImportProfileRecord,
    account: AccountRecord,
    connected: tuple[UUID, AccountRecord],
) -> None:
    """An export lists in full only the days strictly inside it (it may be cut short on its
    first and last), so 30 Nov and 1 Dec are listed in full by neither of two exports
    taken without overlap. They still lie between imports, with no rows missing: A's 100
    on 30 Nov pairs with November's row once December's export is imported."""
    november = "01/11/2026,CR,Pay,500.00,500.00\n30/11/2026,CR,From A,100.00,600.00\n"
    await commit((await stage(profile, _lines(november))).id)
    await _pay(session, run, gbp, connected, on=date(2026, 11, 30))
    assert await _status(session) == "inferred"  # the days after 30 Nov aren't imported

    december = "01/12/2026,VIS,Shop,-2.00,598.00\n31/12/2026,VIS,Shop,-1.00,597.00\n"
    await commit((await stage(profile, _lines(december))).id)
    assert await _status(session) == "matched"
    assert await _balance_after(session, account.id, END) == Decimal("597")
    assert await find_transfer_violations(session) == []


async def test_rolling_back_takes_the_registration_it_made_off_again(
    session: AsyncSession,
    run: Run,
    stage: Stage,
    commit: Commit,
    rollback: Rollback,
    profile: ImportProfileRecord,
    account: AccountRecord,
) -> None:
    """A statement imported into the wrong account, then rolled back: the registration it
    made goes with it, so importing it into the right one registers it there."""
    other = await run(
        create_account,
        name="Right",
        ledger_type="asset",
        subtype="bank",
        balance_mode="transactions",
    )
    wrong = await commit((await stage(profile, _lines(NOVEMBER))).id)
    await rollback(wrong.id)
    [identifier] = await list_identifiers(session)
    assert identifier.account_id is None

    await commit((await stage(profile, _lines(NOVEMBER), account_id=other.id)).id)
    [identifier] = await list_identifiers(session)
    assert identifier.account_id == other.id
    assert account.id != other.id


async def test_rolling_back_keeps_a_registration_another_import_or_the_owner_made(
    session: AsyncSession,
    run: Run,
    stage: Stage,
    commit: Commit,
    rollback: Rollback,
    profile: ImportProfileRecord,
    account: AccountRecord,
) -> None:
    first = await commit((await stage(profile, _lines(NOVEMBER))).id)
    second = await commit((await stage(profile, _lines(DECEMBER.format(type="CR")))).id)
    await rollback(first.id)  # December's statement still names the account
    [identifier] = await list_identifiers(session)
    assert identifier.account_id == account.id
    await rollback(second.id)
    [identifier] = await list_identifiers(session)
    assert identifier.account_id is None

    # Registered by the owner first, it stays through an import's commit and rollback.
    await run(assign_identifier, account.id, "uk_bank_account", f"{SORT_CODE}:{ACCOUNT_NUMBER}")
    third = await commit((await stage(profile, _lines(NOVEMBER))).id)
    await rollback(third.id)
    [identifier] = await list_identifiers(session)
    assert identifier.account_id == account.id
