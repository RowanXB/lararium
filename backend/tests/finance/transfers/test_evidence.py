"""More evidence that a transaction names another own account (transfer evidence design
§5.1): an imported row that prints only a counterparty's account number names the one
registered account with that number, and a sub-account's bank transfer names its main
account. And the weakest (§5.3): a row that prints only a sort code pairs with a unique
counterpart naming no bank account. Rows are simulated by the statement_row and
duplicate_row fixtures."""

from datetime import UTC, datetime, time, timedelta
from decimal import Decimal

import pytest

from lararium.core.connections import create_connection
from lararium.modules.finance.accounts import create_account
from lararium.modules.finance.identifiers import assign_identifier, get_or_create_identifier
from lararium.modules.finance.ledger import CounterpartyFields
from lararium.modules.finance.transfers import (
    list_transfers,
    pair,
    preview_match,
    reject,
    unassign_identifier_and_rematch,
    unpair,
    unreject,
    weak_transfers,
)

from .conftest import (
    A_IDENTIFIER,
    C_IDENTIFIER,
    DAY,
    IMPORTED,
    WHEN,
    Connected,
    RowDetails,
    assert_consistent,
    contra_account,
    placeholders,
)

X_ID = A_IDENTIFIER  # "445566:11112222"
Y_ID = C_IDENTIFIER  # "778899:33334444"
X_NUMBER = "11112222"
STRANGER = "500000:50000001"
# H, an HSBC-like account, and what a BOC-like row paying it prints: its sort code alone
# (stored digits only, as the column's CHECK requires).
H_ID = "404040:55556666"
H_SORT_CODE = RowDetails(sort_code="404040")
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
    x = await imported_bank("X", identifier=X_ID)
    await imported_bank("Z", identifier=f"990011:{X_NUMBER}")
    y = await imported_bank("Y", identifier=Y_ID)
    await statement_row(x, "-100", complete=FULL)
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


# A duplicate keyed as its original was matched by its key; one with a key of its own, by
# its day and amount alone (a CSV and a PDF of the same account describe rows differently).
@pytest.mark.parametrize("key", [None, "later file's own key"])
async def test_the_account_number_on_a_duplicate_row_counts(
    key, session, imported_bank, statement_row, duplicate_row, heal
) -> None:
    x = await imported_bank("X", identifier=X_ID)
    y = await imported_bank("Y", identifier=Y_ID)
    sent = await statement_row(x, "-100", complete=FULL)
    received = await statement_row(y, "100", complete=FULL)
    await duplicate_row(received, RowDetails(account_number=X_NUMBER), key=key)
    await heal()

    t = await _only_transfer(session)
    assert (t.from_account_id, t.to_account_id, t.status) == (x.id, y.id, "matched")
    assert {t.out_transaction_id, t.in_transaction_id} == {sent, received}


async def test_a_duplicate_matched_by_day_and_amount_among_alike_rows_gives_no_evidence(
    session, imported_bank, statement_row, duplicate_row, heal
) -> None:
    # X pays a shop and its savings account S £100 each on one day. A later file of X's, its
    # rows matched by day and amount in its own order (Decision 16), can mark the shop row's
    # duplicate the row that prints S's account number: the shop payment mustn't pair.
    x = await imported_bank("X", identifier=X_ID)
    s = await imported_bank("S", identifier=Y_ID)
    shop = await statement_row(x, "-100", bank_transfer=False, complete=FULL)
    to_savings = await statement_row(x, "-100", complete=FULL)
    await statement_row(s, "100", complete=FULL)
    await duplicate_row(shop, RowDetails(account_number="33334444"), key="later row 1")
    await duplicate_row(to_savings, RowDetails(), key="later row 2")
    await heal()

    assert await list_transfers(session) == []


async def test_a_duplicate_matched_by_its_key_among_alike_rows_counts(
    session, imported_bank, statement_row, duplicate_row, heal
) -> None:
    # As above, but the later file's row has the savings row's own key: it is that row.
    x = await imported_bank("X", identifier=X_ID)
    s = await imported_bank("S", identifier=Y_ID)
    await statement_row(x, "-100", bank_transfer=False, complete=FULL)
    to_savings = await statement_row(x, "-100", complete=FULL)
    received = await statement_row(s, "100", complete=FULL)
    await duplicate_row(to_savings, RowDetails(account_number="33334444"))
    await heal()

    t = await _only_transfer(session)
    assert (t.out_transaction_id, t.in_transaction_id, t.status) == (
        to_savings,
        received,
        "matched",
    )


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


async def test_a_sub_accounts_deposit_pairs_with_its_main_accounts_payment(
    session, imported_bank, statement_row, heal
) -> None:
    m = await imported_bank("M")
    s = await imported_bank("S", main=m)
    sent = await statement_row(m, "-100", complete=FULL)
    received = await statement_row(s, "100")
    await heal()

    t = await _only_transfer(session)
    assert (t.from_account_id, t.to_account_id, t.status) == (m.id, s.id, "matched")
    assert {t.out_transaction_id, t.in_transaction_id} == {sent, received}


async def test_a_sub_accounts_row_after_the_main_accounts_last_import_gets_a_placeholder(
    session, imported_bank, statement_row, heal
) -> None:
    m = await imported_bank("M")
    s = await imported_bank("S", main=m)
    await statement_row(m, "-5", complete=(DAY - timedelta(days=60), DAY))
    day = DAY + timedelta(days=3)
    await statement_row(s, "100", day=day, complete=(DAY, day))
    await heal()

    t = await _only_transfer(session)
    assert (t.from_account_id, t.to_account_id, t.status) == (m.id, s.id, "inferred")
    assert len(await placeholders(session)) == 1


async def test_a_sub_accounts_row_inside_the_main_accounts_full_days_settles(
    session, imported_bank, statement_row, heal
) -> None:
    m = await imported_bank("M")
    s = await imported_bank("S", main=m)
    await statement_row(m, "-5", complete=FULL)
    await statement_row(s, "100")
    await heal()

    t = await _only_transfer(session)
    assert (t.from_account_id, t.to_account_id, t.status) == (m.id, s.id, "before_history")
    assert await placeholders(session) == []


async def test_a_sub_account_row_naming_an_account_ignores_the_main(
    session, imported_bank, statement_row, heal
) -> None:
    m = await imported_bank("M")
    s = await imported_bank("S", main=m)
    await statement_row(m, "-100", complete=FULL)
    await statement_row(s, "100", details=RowDetails(account_number="50000001"))
    await heal()

    assert await list_transfers(session) == []


@pytest.mark.parametrize(
    "printed",
    [
        RowDetails(account_number="50000001"),
        RowDetails(account_ending="0001"),
        RowDetails(sort_code="500000"),
        None,  # the posted transaction names a stranger's bank account in full
    ],
    ids=["account number", "account ending", "sort code", "full account"],
)
async def test_a_sub_accounts_deposit_takes_no_main_account_payment_naming_a_bank_account(
    session, run, imported_bank, statement_row, heal, printed
) -> None:
    """A move between a sub-account and its main account prints no bank account on either
    side, so a payment to someone else's account, the same day and of the same amount, is
    no candidate, and the move pairs alone (transfer evidence design §5.2)."""
    m = await imported_bank("M")
    s = await imported_bank("S", main=m)
    stranger = None
    if printed is None:
        stranger = await run(get_or_create_identifier, "uk_bank_account", STRANGER)
    sent = await statement_row(m, "-100", complete=FULL)
    await statement_row(m, "-100", details=printed, identifier_id=stranger)
    received = await statement_row(s, "100")
    await heal()

    t = await _only_transfer(session)
    assert (t.from_account_id, t.to_account_id, t.status) == (m.id, s.id, "matched")
    assert {t.out_transaction_id, t.in_transaction_id} == {sent, received}
    assert t.review_reason is None
    await assert_consistent(session)


async def test_a_sub_accounts_deposit_never_pairs_with_a_main_account_row_naming_a_bank_account(
    session, imported_bank, statement_row, heal
) -> None:
    """Even as the only row of that amount: it is somebody else's payment, and the move's
    own row lies before what the main account's imports list (settled, no placeholder)."""
    m = await imported_bank("M")
    s = await imported_bank("S", main=m)
    await statement_row(m, "-100", details=RowDetails(account_number="50000001"), complete=FULL)
    received = await statement_row(s, "100")
    await heal()

    t = await _only_transfer(session)
    assert (t.from_account_id, t.to_account_id, t.status) == (m.id, s.id, "before_history")
    assert (t.out_transaction_id, t.in_transaction_id) == (None, received)
    assert await placeholders(session) == []


async def test_a_main_account_row_naming_the_sub_accounts_own_bank_account_pairs(
    session, run, imported_bank, statement_row, heal
) -> None:
    """A sub-account with bank details of its own, registered: the main account's row
    names it, and the two pair, each naming the other."""
    m = await imported_bank("M")
    s = await imported_bank("S", main=m, identifier=Y_ID)
    named = await run(get_or_create_identifier, "uk_bank_account", Y_ID)
    sent = await statement_row(m, "-100", identifier_id=named, complete=FULL)
    received = await statement_row(s, "100")
    await heal()

    t = await _only_transfer(session)
    assert (t.from_account_id, t.to_account_id, t.status) == (m.id, s.id, "matched")
    assert {t.out_transaction_id, t.in_transaction_id} == {sent, received}


async def test_preview_match_takes_no_staged_row_naming_a_stranger_for_a_sub_accounts_move(
    session, run, gbp, imported_bank, statement_row, heal
) -> None:
    """preview_match reads the relation matching does: a row not yet posted on the main
    account that names someone else's account is no counterpart of the sub-account's
    move."""
    m = await imported_bank("M")
    s = await imported_bank("S", main=m)
    stranger = await run(get_or_create_identifier, "uk_bank_account", STRANGER)
    received = await statement_row(s, "100")
    await heal()

    async def preview(identifier_id):
        return await preview_match(
            session,
            account_id=m.id,
            instrument_id=gbp.id,
            quantity=Decimal(-100),
            occurred_at=datetime.combine(DAY, time(), UTC),
            counterparty=CounterpartyFields(identifier_id=identifier_id, bank_transfer=True),
            complete=FULL,
        )

    assert await preview(None) == received
    assert await preview(stranger) is None


async def test_interest_on_a_sub_account_is_no_transfer(
    session, imported_bank, statement_row, heal
) -> None:
    m = await imported_bank("M")
    s = await imported_bank("S", main=m)
    await statement_row(m, "-100", complete=FULL)
    await statement_row(s, "100", bank_transfer=False)
    await heal()

    assert await list_transfers(session) == []


async def test_a_connected_account_with_a_parent_is_unaffected(
    session, run, imported_bank, statement_row, connector_tx, heal
) -> None:
    m = await imported_bank("M")
    connection = await run(create_connection, connector="monzo", display_name="S", config={})
    s = await run(
        create_account,
        name="S",
        ledger_type="asset",
        subtype="bank",
        balance_mode="transactions",
        parent_account_id=m.id,
        source_connection_id=connection.id,
    )
    await statement_row(m, "-100", complete=FULL)
    await connector_tx(Connected(connection.id, s), "100", occurred_at=WHEN)
    await heal()

    assert await list_transfers(session) == []


async def test_a_sort_code_alone_pairs_a_unique_counterpart(
    session, imported_bank, statement_row, heal
) -> None:
    x = await imported_bank("X")
    h = await imported_bank("H", identifier=H_ID)
    sent = await statement_row(x, "-100", details=H_SORT_CODE)
    received = await statement_row(h, "100")
    await heal()

    transfers = await list_transfers(session)
    [t] = transfers
    assert (t.from_account_id, t.to_account_id, t.status) == (x.id, h.id, "matched")
    assert {t.out_transaction_id, t.in_transaction_id} == {sent, received}
    assert await weak_transfers(session, transfers) == {t.id}
    await assert_consistent(session)


async def test_a_sort_code_alone_never_pairs_with_a_row_naming_an_account(
    session, run, imported_bank, statement_row, heal
) -> None:
    x = await imported_bank("X")
    h = await imported_bank("H", identifier=H_ID)
    stranger = await run(get_or_create_identifier, "uk_bank_account", STRANGER)
    await statement_row(x, "-100", details=H_SORT_CODE)
    await statement_row(h, "100", identifier_id=stranger)
    await heal()

    assert await list_transfers(session) == []


async def test_preview_match_takes_no_staged_row_naming_a_stranger_for_a_sort_code(
    session, run, gbp, imported_bank, statement_row, heal
) -> None:
    """preview_match reads the relation matching does: a row not yet posted that names
    someone else's account names a bank account, so a sort code alone pairs it with
    nothing, as once it is posted (the test above)."""
    x = await imported_bank("X")
    h = await imported_bank("H", identifier=H_ID)
    stranger = await run(get_or_create_identifier, "uk_bank_account", STRANGER)
    sent = await statement_row(x, "-100", details=H_SORT_CODE)
    await heal()

    async def preview(identifier_id):
        return await preview_match(
            session,
            account_id=h.id,
            instrument_id=gbp.id,
            quantity=Decimal(100),
            occurred_at=datetime.combine(DAY, time(), UTC),
            counterparty=CounterpartyFields(identifier_id=identifier_id, bank_transfer=True),
            complete=IMPORTED,
        )

    assert await preview(None) == sent  # naming nobody, it would pair weakly
    assert await preview(stranger) is None


async def test_a_sort_code_alone_with_two_candidates_pairs_nothing_and_asks_nothing(
    session, imported_bank, statement_row, heal
) -> None:
    x = await imported_bank("X")
    h = await imported_bank("H", identifier=H_ID)
    await statement_row(x, "-100", details=H_SORT_CODE)
    await statement_row(h, "100")
    await statement_row(h, "100")
    await heal()

    # No pair, and no transfer to carry a review reason either.
    assert await list_transfers(session) == []


async def test_a_sort_code_alone_opens_no_transfer_and_no_placeholder(
    session, ids, imported_bank, statement_row, heal
) -> None:
    x = await imported_bank("X")
    await imported_bank("H", identifier=H_ID)
    sent = await statement_row(x, "-100", details=H_SORT_CODE)
    await heal()

    assert await list_transfers(session) == []
    assert await placeholders(session) == []
    assert await contra_account(session, sent, x.id) == ids.unassigned


async def test_a_sort_code_two_own_accounts_share_names_nobody(
    session, imported_bank, statement_row, heal
) -> None:
    x = await imported_bank("X")
    h = await imported_bank("H", identifier=H_ID)
    await imported_bank("K", identifier="404040:77778888")
    await statement_row(x, "-100", details=H_SORT_CODE)
    await statement_row(h, "100")
    await heal()

    assert await list_transfers(session) == []


async def test_a_sort_code_alone_takes_a_counterpart_only_from_its_accounts_full_days(
    session, imported_bank, statement_row, heal
) -> None:
    # M4's gate, as for a one-sided pair: H's imports list in full only from the day itself,
    # after the sending row's window starts, so the real receipt may be inside H's opening.
    x = await imported_bank("X")
    h = await imported_bank("H", identifier=H_ID)
    await statement_row(x, "-100", details=H_SORT_CODE)
    await statement_row(h, "100", complete=(DAY, DAY + timedelta(days=60)))
    await heal()

    assert await list_transfers(session) == []


async def test_a_named_counterpart_outranks_a_sort_code(
    session, imported_bank, statement_row, heal
) -> None:
    x = await imported_bank("X", identifier=X_ID)
    h = await imported_bank("H", identifier=H_ID)
    y = await imported_bank("Y")
    sent = await statement_row(x, "-100", details=H_SORT_CODE, complete=FULL)
    received = await statement_row(y, "100", details=RowDetails(account_number=X_NUMBER))
    await statement_row(h, "100")
    await heal()

    transfers = await list_transfers(session)
    [t] = transfers
    assert (t.from_account_id, t.to_account_id, t.status) == (x.id, y.id, "matched")
    assert {t.out_transaction_id, t.in_transaction_id} == {sent, received}
    assert await weak_transfers(session, transfers) == set()


async def test_unpairing_a_sort_code_pair_leaves_both_records_apart_on_unassigned(
    session, run, ids, imported_bank, statement_row, heal
) -> None:
    """Weak evidence makes no transfer of its own, so after Unpair neither record is in one,
    and never_pair keeps the two apart through later heals (pair_records can pair either)."""
    x = await imported_bank("X")
    h = await imported_bank("H", identifier=H_ID)
    sent = await statement_row(x, "-100", details=H_SORT_CODE)
    received = await statement_row(h, "100")
    await heal()
    t = await _only_transfer(session)

    await run(unpair, t.id)
    await heal()

    assert await list_transfers(session) == []
    assert await contra_account(session, sent, x.id) == ids.unassigned
    assert await contra_account(session, received, h.id) == ids.unassigned
    await assert_consistent(session)


async def test_rejecting_a_sort_code_pair_and_undoing_it_restores_it(
    session, run, ids, imported_bank, statement_row, heal
) -> None:
    x = await imported_bank("X")
    h = await imported_bank("H", identifier=H_ID)
    sent = await statement_row(x, "-100", details=H_SORT_CODE)
    received = await statement_row(h, "100")
    await heal()
    t = await _only_transfer(session)

    await run(reject, t.id)
    assert [r.status for r in await list_transfers(session)] == ["rejected"]
    assert await contra_account(session, sent, x.id) == ids.unassigned
    assert await contra_account(session, received, h.id) == ids.unassigned

    await run(unreject, t.id)
    await heal()
    again = await _only_transfer(session)
    assert (again.status, again.out_transaction_id, again.in_transaction_id) == (
        "matched",
        sent,
        received,
    )
    await assert_consistent(session)


async def test_a_pair_the_owner_made_is_not_weak_once_its_evidence_goes(
    session, run, imported_bank, statement_row, heal
) -> None:
    x = await imported_bank("X")
    identifier = await run(assign_identifier, x.id, "uk_bank_account", X_ID)
    y = await imported_bank("Y")
    chosen = await statement_row(x, "-100", complete=FULL)
    await statement_row(x, "-100", complete=FULL)
    received = await statement_row(y, "100", details=RowDetails(account_number=X_NUMBER))
    await heal()
    [waiting] = await list_transfers(session)
    assert waiting.review_reason == "several_candidates"
    await run(pair, waiting.id, chosen)

    # Neither record names the other's account any more, but the owner's pair holds them.
    await run(unassign_identifier_and_rematch, identifier.id)
    transfers = await list_transfers(session)
    [t] = transfers
    assert (t.status, t.out_transaction_id, t.in_transaction_id) == ("matched", chosen, received)
    assert await weak_transfers(session, transfers) == set()
