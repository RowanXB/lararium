"""More evidence that a transaction names another own account (transfer evidence design
§5.1): an imported row that prints only a counterparty's account number names the one
registered account with that number. Rows are simulated by the statement_row and
duplicate_row fixtures."""

from datetime import timedelta

import pytest

from lararium.modules.finance.identifiers import assign_identifier, get_or_create_identifier
from lararium.modules.finance.transfers import list_transfers, unassign_identifier_and_rematch

from .conftest import A_IDENTIFIER, C_IDENTIFIER, DAY, RowDetails, contra_account, placeholders

X_ID = A_IDENTIFIER  # "445566:11112222"
Y_ID = C_IDENTIFIER  # "778899:33334444"
X_NUMBER = "11112222"
STRANGER = "500000:50000001"
# The sending account's imports list well before the receiving row's window starts, so a
# row naming nobody can be taken as its counterpart (design §5.8).
FULL = (DAY - timedelta(days=60), DAY + timedelta(days=60))


async def _only_transfer(session):
    [t] = await list_transfers(session)
    return t


async def test_an_account_number_alone_names_the_registered_account(
    session, imported_bank, statement_row, heal
) -> None:
    x = await imported_bank("X", identifier=X_ID)
    y = await imported_bank("Y", identifier=Y_ID)
    sent = await statement_row(x, "-100", complete=FULL)
    received = await statement_row(y, "100", details=RowDetails(account_number=X_NUMBER))
    await heal()

    t = await _only_transfer(session)
    assert (t.from_account_id, t.to_account_id, t.status) == (x.id, y.id, "matched")
    assert {t.out_transaction_id, t.in_transaction_id} == {sent, received}


async def test_an_account_number_two_own_accounts_share_names_nobody(
    session, imported_bank, statement_row, heal
) -> None:
    await imported_bank("X", identifier=X_ID)
    await imported_bank("Z", identifier=f"990011:{X_NUMBER}")
    y = await imported_bank("Y", identifier=Y_ID)
    await statement_row(y, "100", details=RowDetails(account_number=X_NUMBER))
    await heal()

    assert await list_transfers(session) == []


async def test_a_named_stranger_is_not_overridden_by_an_account_number(
    session, run, imported_bank, statement_row, duplicate_row, heal
) -> None:
    x = await imported_bank("X", identifier=X_ID)
    y = await imported_bank("Y", identifier=Y_ID)
    stranger = await run(get_or_create_identifier, "uk_bank_account", STRANGER)
    await statement_row(x, "-100", complete=FULL)
    received = await statement_row(y, "100", identifier_id=stranger)
    await duplicate_row(received, RowDetails(account_number=X_NUMBER))
    await heal()

    assert await list_transfers(session) == []


async def test_a_row_naming_its_own_accounts_number_is_no_transfer(
    session, imported_bank, statement_row, heal
) -> None:
    x = await imported_bank("X", identifier=X_ID)
    y = await imported_bank("Y", identifier=Y_ID)
    await statement_row(x, "-100", complete=FULL)
    await statement_row(y, "100", details=RowDetails(account_number="33334444"))
    await heal()

    assert await list_transfers(session) == []


async def test_the_account_number_on_a_duplicate_row_counts(
    session, imported_bank, statement_row, duplicate_row, heal
) -> None:
    x = await imported_bank("X", identifier=X_ID)
    y = await imported_bank("Y", identifier=Y_ID)
    sent = await statement_row(x, "-100", complete=FULL)
    received = await statement_row(y, "100", complete=FULL)
    await duplicate_row(received, RowDetails(account_number=X_NUMBER))
    await heal()

    t = await _only_transfer(session)
    assert (t.from_account_id, t.to_account_id, t.status) == (x.id, y.id, "matched")
    assert {t.out_transaction_id, t.in_transaction_id} == {sent, received}


@pytest.mark.parametrize("printed_on", ["committed", "duplicate"])
async def test_details_on_either_row_give_the_same_transfer(
    printed_on, session, imported_bank, statement_row, duplicate_row, heal
) -> None:
    x = await imported_bank("X", identifier=X_ID)
    y = await imported_bank("Y", identifier=Y_ID)
    printed = RowDetails(account_number=X_NUMBER)
    await statement_row(x, "-100", complete=FULL)
    received = await statement_row(y, "100", details=printed if printed_on == "committed" else None)
    if printed_on == "duplicate":
        await duplicate_row(received, printed)
    await heal()

    assert [
        (t.from_account_id, t.to_account_id, t.status) for t in await list_transfers(session)
    ] == [(x.id, y.id, "matched")]


async def test_a_duplicate_in_a_rolled_back_batch_gives_no_evidence(
    session, imported_bank, statement_row, duplicate_row, heal
) -> None:
    x = await imported_bank("X", identifier=X_ID)
    y = await imported_bank("Y", identifier=Y_ID)
    await statement_row(x, "-100", complete=FULL)
    received = await statement_row(y, "100", complete=FULL)
    await duplicate_row(received, RowDetails(account_number=X_NUMBER), batch_status="rolled_back")
    await heal()

    assert await list_transfers(session) == []


async def test_two_rows_with_different_account_numbers_name_nobody(
    session, imported_bank, statement_row, duplicate_row, heal
) -> None:
    x = await imported_bank("X", identifier=X_ID)
    y = await imported_bank("Y", identifier=Y_ID)
    await statement_row(x, "-100", complete=FULL)
    received = await statement_row(y, "100", details=RowDetails(account_number=X_NUMBER))
    await duplicate_row(received, RowDetails(account_number="99998888"))
    await heal()

    assert await list_transfers(session) == []


async def test_registering_the_account_later_pairs_history_and_removing_it_reverts(
    session, run, ids, imported_bank, statement_row, heal
) -> None:
    x = await imported_bank("X")
    y = await imported_bank("Y", identifier=Y_ID)
    sent = await statement_row(x, "-100", complete=FULL)
    received = await statement_row(y, "100", details=RowDetails(account_number=X_NUMBER))
    await heal()
    assert await list_transfers(session) == []

    identifier = await run(assign_identifier, x.id, "uk_bank_account", X_ID)
    await heal()
    t = await _only_transfer(session)
    assert (t.from_account_id, t.to_account_id, t.status) == (x.id, y.id, "matched")

    await run(unassign_identifier_and_rematch, identifier.id)
    assert await list_transfers(session) == []
    assert await contra_account(session, sent, x.id) == ids.unassigned
    assert await contra_account(session, received, y.id) == ids.unassigned


async def test_a_row_naming_an_account_by_number_pairs_even_if_not_a_bank_transfer(
    session, imported_bank, statement_row, heal
) -> None:
    x = await imported_bank("X", identifier=X_ID)
    y = await imported_bank("Y", identifier=Y_ID)
    await statement_row(x, "-100", complete=FULL)
    await statement_row(y, "100", bank_transfer=False, details=RowDetails(account_number=X_NUMBER))
    await heal()

    t = await _only_transfer(session)
    assert (t.from_account_id, t.to_account_id, t.status) == (x.id, y.id, "matched")
    assert await placeholders(session) == []
