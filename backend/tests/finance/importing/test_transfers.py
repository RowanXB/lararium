"""Imports through M4's transfer matching (its design §5.8): an imported row that is the
other side of a known transfer replaces the transfer's placeholder instead of counting
twice, a rollback brings the placeholder back, and the statement's own bank account is
registered to the account it was imported into."""

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from uuid import UUID

import pytest
from hypothesis import given
from hypothesis import strategies as st
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from lararium.core.connections import create_connection
from lararium.modules.finance import models
from lararium.modules.finance.accounts import AccountRecord, create_account
from lararium.modules.finance.identifiers import get_or_create_identifier, list_identifiers
from lararium.modules.finance.importing.profiles import ImportProfileRecord
from lararium.modules.finance.importing.staging import list_batch_rows
from lararium.modules.finance.instruments import InstrumentRecord
from lararium.modules.finance.ledger import (
    CounterpartyFields,
    PostingInput,
    TransactionInput,
    find_unbalanced_transactions,
    upsert_connector_transaction,
)
from lararium.modules.finance.system_accounts import get_or_create_system_account
from lararium.modules.finance.transfers import (
    find_transfer_violations,
    heal_transfers,
    list_transfers,
    reject,
    source_ref,
    transfer_contra,
)

from .staging_helpers import (
    Commit,
    Rollback,
    Run,
    Stage,
    _adjustments,
    _balance_after,
    _csv_profile,
    isolated_example,
)

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
    quantity: str = "-100",
) -> None:
    """A pays 100 (on 10 Dec) to the bank account M's statement names, or receives it from
    there (a positive quantity), then matching runs."""
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
                PostingInput(a.id, gbp.id, Decimal(quantity)),
                PostingInput(contra, gbp.id, -Decimal(quantity)),
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
    account: AccountRecord,
    connected: tuple[UUID, AccountRecord],
) -> None:
    await commit((await stage(profile, _lines(NOVEMBER))).id)
    await _pay(session, run, gbp, connected)
    await commit((await stage(profile, _lines(DECEMBER.format(type="VIS")))).id)
    # A card refund of 100 is not the transfer. December lists the days M's side may be
    # booked on in full, so M's balance is the statement's and the transfer is settled.
    assert await _status(session) == "before_history"
    assert await _balance_after(session, account.id, END) == Decimal("596")


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


async def test_rolling_back_keeps_a_registration_another_import_shows(
    session: AsyncSession,
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


async def test_a_transfer_before_the_first_statement_is_inside_its_opening(
    session: AsyncSession,
    run: Run,
    gbp: InstrumentRecord,
    stage: Stage,
    commit: Commit,
    profile: ImportProfileRecord,
    account: AccountRecord,
    connected: tuple[UUID, AccountRecord],
) -> None:
    """M's history starts on the first day its imports cover: A's 100 in October, before
    M's first statement (November), is inside the opening November's statement gives, so
    it is settled against Opening balances (before_history), not given a placeholder on
    M. Importing October, with M's own row of it, moves the start back, and they pair."""
    for key in ("unassigned", "in_transit"):  # as A's connector made them, syncing
        await run(get_or_create_system_account, key)
    await _pay(session, run, gbp, connected, on=date(2026, 10, 15))
    await commit((await stage(profile, _lines(NOVEMBER))).id)
    assert await _status(session) == "before_history"
    assert await _balance_after(session, account.id, date(2026, 11, 30)) == Decimal("499")

    october = (
        "01/10/2026,VIS,Shop,-1.00,99.00\n15/10/2026,CR,From A,100.00,199.00\n"
        "31/10/2026,VIS,Shop,-199.00,0.00\n"
    )
    await commit((await stage(profile, _lines(october))).id)
    assert await _status(session) == "matched"
    assert await _balance_after(session, account.id, date(2026, 11, 30)) == Decimal("499")
    assert await find_transfer_violations(session) == []


async def test_a_late_receipt_leaves_the_statement_balance_alone(
    session: AsyncSession,
    run: Run,
    gbp: InstrumentRecord,
    stage: Stage,
    commit: Commit,
    profile: ImportProfileRecord,
    account: AccountRecord,
    connected: tuple[UUID, AccountRecord],
) -> None:
    """A's 100 on 10 Dec arrives on M on 20 Dec, after its window: no pair. December lists
    every day M's side may be booked on in full, so M's balance is the statement's: the
    transfer is settled, with no placeholder on M. January's export agrees with
    December's, so it takes no opening adjustment."""
    await commit((await stage(profile, _lines(NOVEMBER))).id)
    await _pay(session, run, gbp, connected)
    december = (
        "01/12/2026,VIS,Shop,-2.00,497.00\n20/12/2026,CR,From A,100.00,597.00\n"
        "31/12/2026,VIS,Shop,-1.00,596.00\n"
    )
    await commit((await stage(profile, _lines(december))).id)
    assert await _status(session) == "before_history"
    assert await _balance_after(session, account.id, END) == Decimal("596")

    january = "01/01/2027,VIS,Shop,-1.00,595.00\n10/01/2027,VIS,Shop,-1.00,594.00\n"
    committed = await commit((await stage(profile, _lines(january))).id)
    assert _adjustments(await list_batch_rows(session, committed.id)) == []
    assert await find_transfer_violations(session) == []


async def test_a_skipped_statement_month_is_held_by_the_gap_adjustment(
    session: AsyncSession,
    run: Run,
    gbp: InstrumentRecord,
    stage: Stage,
    commit: Commit,
    profile: ImportProfileRecord,
    account: AccountRecord,
    connected: tuple[UUID, AccountRecord],
) -> None:
    """October and December imported, November skipped: A's 100 to M on 15 Nov is inside
    the gap December's opening adjustment holds, so it is settled, not counted again as a
    placeholder; importing November pairs it with M's own row."""
    october = "01/10/2026,CR,Pay,200.00,200.00\n31/10/2026,VIS,Shop,-1.00,199.00\n"
    await commit((await stage(profile, _lines(october))).id)
    await _pay(session, run, gbp, connected, on=date(2026, 11, 15))
    assert await _status(session) == "inferred"
    december = "01/12/2026,VIS,Shop,-1.00,298.00\n31/12/2026,VIS,Shop,-1.00,297.00\n"
    await commit((await stage(profile, _lines(december))).id)
    assert await _status(session) == "before_history"
    assert await _balance_after(session, account.id, END) == Decimal("297")

    november = "01/11/2026,VIS,Shop,-1.00,198.00\n15/11/2026,CR,From A,100.00,298.00\n"
    november += "30/11/2026,CR,Pay,1.00,299.00\n"
    await commit((await stage(profile, _lines(november))).id)
    assert await _status(session) == "matched"
    assert await find_transfer_violations(session) == []


async def test_a_transfer_across_the_first_statements_start_is_held_by_its_opening(
    session: AsyncSession,
    run: Run,
    gbp: InstrumentRecord,
    stage: Stage,
    commit: Commit,
    profile: ImportProfileRecord,
    account: AccountRecord,
    connected: tuple[UUID, AccountRecord],
) -> None:
    """A receives M's 100 on 3 Nov, which M may have booked as early as 29 Oct, before its
    first statement (November): M's side is in the statement or in its opening, so the
    transfer is settled and M's balance is the statement's."""
    for key in ("unassigned", "in_transit"):  # as A's connector made them, syncing
        await run(get_or_create_system_account, key)
    await _pay(session, run, gbp, connected, on=date(2026, 11, 3), quantity="100")
    await commit((await stage(profile, _lines(NOVEMBER))).id)
    assert await _status(session) == "before_history"
    assert await _balance_after(session, account.id, date(2026, 11, 30)) == Decimal("499")


@given(
    cents=st.integers(1, 100_000),
    booking_day=st.integers(9, 21),
    delay=st.integers(0, 3),
    incoming=st.booleans(),
    arrival=st.permutations(("october", "december", "connector")),
)
async def test_backfilling_a_transfer_in_a_statement_gap(
    session: AsyncSession,
    run: Run,
    gbp: InstrumentRecord,
    stage: Stage,
    commit: Commit,
    rollback: Rollback,
    profile: ImportProfileRecord,
    account: AccountRecord,
    connected: tuple[UUID, AccountRecord],
    cents: int,
    booking_day: int,
    delay: int,
    incoming: bool,
    arrival: list[str],
) -> None:
    amount = Decimal(cents).scaleb(-2)
    booked = date(2026, 11, booking_day)
    lag = timedelta(days=delay)
    connector_day = booked - lag if incoming else booked + lag
    if not incoming:
        amount = -amount
    transfer_type = "CR" if incoming else "BP"
    # November contains both the transfer and an unrelated card movement of equal value.
    # Its absence leaves both movements inside the adjustment to December's opening.
    november_close = Decimal(100) + 2 * amount
    statements = {
        "october": "01/10/2026,CR,Pay,101,101\n31/10/2026,VIS,Shop,-1,100\n",
        "november": (
            f"01/11/2026,VIS,Shop,-1,99\n"
            f"{booked:%d/%m/%Y},VIS,Card,{amount},{99 + amount}\n"
            f"{booked:%d/%m/%Y},{transfer_type},Bank transfer,{amount},{99 + 2 * amount}\n"
            f"30/11/2026,CR,Pay,1,{november_close}\n"
        ),
        "december": (
            f"01/12/2026,VIS,Shop,-2,{november_close - 2}\n"
            f"31/12/2026,VIS,Shop,-1,{november_close - 3}\n"
        ),
    }
    async with isolated_example(session):
        for key in ("unassigned", "in_transit"):
            await run(get_or_create_system_account, key)
        for item in arrival:
            if item == "connector":
                await _pay(session, run, gbp, connected, on=connector_day, quantity=str(-amount))
            else:
                await commit((await stage(profile, _lines(statements[item]))).id)
            assert await find_transfer_violations(session) == []

        assert await _status(session) == "before_history"
        assert await _balance_after(session, account.id, END) == november_close - 3
        for _ in range(2):
            batch = await commit((await stage(profile, _lines(statements["november"]))).id)
            [transfer] = await list_transfers(session)
            assert transfer.status == "matched"
            [bank_row] = [
                row
                for row in await list_batch_rows(session, batch.id)
                if row.description == "Bank transfer"
            ]
            matched_leg = transfer.in_transaction_id if incoming else transfer.out_transaction_id
            assert bank_row.transaction_id is not None and matched_leg == bank_row.transaction_id
            assert await _balance_after(session, account.id, booked) == 99 + 2 * amount
            assert await _balance_after(session, account.id, END) == november_close - 3
            assert await find_transfer_violations(session) == []
            assert await find_unbalanced_transactions(session) == []

            await rollback(batch.id)
            assert await _status(session) == "before_history"
            assert await _balance_after(session, account.id, END) == november_close - 3
            assert await find_transfer_violations(session) == []
