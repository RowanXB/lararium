"""The interface statement imports (M2) use, with a simulated import row: a manual-origin
transaction on the manual account, naming no counterparty, marked a bank transfer, recorded
as a row of a committed import (record_import)."""

from datetime import timedelta
from decimal import Decimal

import pytest
from sqlalchemy import insert, text, update
from sqlalchemy.exc import IntegrityError

from lararium.modules.finance import models
from lararium.modules.finance.identifiers import assign_identifier, get_or_create_identifier
from lararium.modules.finance.ledger import (
    CounterpartyFields,
    PostingInput,
    TransactionInput,
    post_transaction,
    void_transaction,
)
from lararium.modules.finance.transfers import (
    list_transfers,
    preview_match,
    reject,
    source_ref,
    transfer_contra,
    unassign_identifier_and_rematch,
    unlink_transaction,
    unreject,
)

from .conftest import (
    A_IDENTIFIER,
    B_IDENTIFIER,
    C_IDENTIFIER,
    IMPORTED,
    WHEN,
    assert_consistent,
    balances_on,
    contra_account,
    placeholders,
)

ROW_AT = WHEN + timedelta(hours=5)


@pytest.fixture
async def inferred(session, run, connected_bank, manual_bank, connector_tx, heal):
    """A's payment of 100 to the manual account B, with B's placeholder."""
    a = await connected_bank("A")
    b = await manual_bank("B")
    identifier = await run(get_or_create_identifier, "uk_bank_account", B_IDENTIFIER)
    record = await connector_tx(a, "-100", identifier_id=identifier)
    await heal()
    [transfer] = await list_transfers(session)
    assert transfer.status == "inferred"
    assert (await balances_on(session))[b.id] == Decimal(1100)
    return a, b, identifier, record, transfer


@pytest.fixture
def import_row(session, run, gbp, record_import):
    """Post a row as an M2 import would: its contra from transfer_contra."""

    async def _post(account, quantity: str = "100"):
        contra = await transfer_contra(
            session,
            source=None,
            own_account_id=account.id,
            counterparty_identifier_id=None,
            other_postings=0,
        )
        amount = Decimal(quantity)
        row = await run(
            post_transaction,
            TransactionInput(
                occurred_at=ROW_AT,
                description="Faster payment from A",
                origin="manual",
                postings=[
                    PostingInput(account.id, gbp.id, amount),
                    PostingInput(contra, gbp.id, -amount),
                ],
                counterparty=CounterpartyFields(bank_transfer=True),
            ),
        )
        await record_import(account.id, [row.id])
        assert await source_ref(session, row.id) == ("import", f"test:{row.id}")
        return row

    return _post


@pytest.fixture
async def imported(session, inferred, import_row, heal):
    """The import row on B, matched with A's payment in place of the placeholder."""
    a, b, identifier, record, _ = inferred
    row = await import_row(b)
    await heal()
    return a, b, identifier, record, row


async def _assert_placeholder_back(session, ids, a, b, record) -> None:
    [transfer] = await list_transfers(session)
    assert (transfer.status, transfer.out_transaction_id, transfer.in_transaction_id) == (
        "inferred",
        record.id,
        None,
    )
    [placeholder] = await placeholders(session)
    assert transfer.placeholder_transaction_id == placeholder.id
    assert await contra_account(session, record.id, a.account.id) == ids.in_transit
    balances = await balances_on(session)
    assert (balances[b.id], balances[ids.in_transit]) == (Decimal(1100), 0)
    await assert_consistent(session)


async def test_import_row_replaces_placeholder(session, ids, imported, heal) -> None:
    _, b, _, record, row = imported

    [transfer] = await list_transfers(session)
    assert (transfer.status, transfer.out_transaction_id, transfer.in_transaction_id) == (
        "matched",
        record.id,
        row.id,
    )
    assert transfer.placeholder_transaction_id is None
    assert await placeholders(session) == []
    assert await contra_account(session, row.id, b.id) == ids.in_transit
    balances = await balances_on(session)
    assert balances[b.id] == Decimal(1100)  # one +100, not two
    assert balances[ids.in_transit] == 0
    await assert_consistent(session)
    assert await heal() == 0


async def _assert_previewed(session, previewed, row) -> None:
    """Posting (and healing) did what preview_match said: the row is in a matched transfer
    with exactly the previewed record, or in none when it previewed none."""
    matched = [
        t
        for t in await list_transfers(session, statuses=["matched"])
        if row.id in (t.out_transaction_id, t.in_transaction_id)
    ]
    if previewed is None:
        assert matched == []
    else:
        [transfer] = matched
        assert {transfer.out_transaction_id, transfer.in_transaction_id} == {previewed, row.id}


async def test_preview_match_finds_the_partner(session, gbp, inferred) -> None:
    _, b, _, sent, _ = inferred
    count = text("select count(*) from finance.transactions")
    before = await session.scalar(count)

    async def preview(quantity: str = "100", bank_transfer: bool = True, complete=IMPORTED):
        return await preview_match(
            session,
            account_id=b.id,
            instrument_id=gbp.id,
            quantity=Decimal(quantity),
            occurred_at=ROW_AT,
            counterparty=CounterpartyFields(bank_transfer=bank_transfer),
            complete=complete,
        )

    assert await preview() == sent.id
    assert await preview("90") is None  # another amount
    assert await preview("-100") is None  # the wrong direction
    assert await preview(bank_transfer=False) is None  # a card refund, say
    # Its import doesn't list A's window in full: B's real row may be missing (design §5.8).
    assert await preview(complete=None) is None
    assert await preview(complete=(WHEN.date(), IMPORTED[1])) is None
    assert await session.scalar(count) == before


async def test_preview_match_none_when_ambiguous(
    session, run, gbp, connected_bank, manual_bank, connector_tx, heal
) -> None:
    a = await connected_bank("A")
    b = await manual_bank("B")
    identifier = await run(get_or_create_identifier, "uk_bank_account", B_IDENTIFIER)
    for n in (1, 2):
        await connector_tx(
            a,
            "-100",
            external_id=f"tx_{n}",
            occurred_at=WHEN + timedelta(hours=n),
            identifier_id=identifier,
        )
    await heal()
    assert [t.status for t in await list_transfers(session)] == ["inferred"] * 2

    assert (
        await preview_match(
            session,
            account_id=b.id,
            instrument_id=gbp.id,
            quantity=Decimal(100),
            occurred_at=ROW_AT,
            counterparty=CounterpartyFields(bank_transfer=True),
            complete=IMPORTED,
        )
        is None
    )


async def test_unlink_then_void_restores_placeholder(session, run, ids, imported, heal) -> None:
    a, b, _, record, row = imported

    # The M2 rollback order: unlink each row, then void it.
    await run(unlink_transaction, row.id)
    # The row is no longer a leg: back on Unassigned, until it is voided.
    assert await contra_account(session, row.id, b.id) == ids.unassigned
    await assert_consistent(session)
    r = models.ImportRow
    await session.execute(update(r).where(r.transaction_id == row.id).values(status="rolled_back"))
    await run(void_transaction, row.id)

    await _assert_placeholder_back(session, ids, a, b, record)
    assert await heal() == 0


async def test_unlink_from_a_rejected_transfer_keeps_the_rejection(
    session, run, ids, imported, heal
) -> None:
    a, b, _, record, row = imported
    [transfer] = await list_transfers(session)
    await run(reject, transfer.id)

    await _unlink_and_void(session, run, row)

    [still] = await list_transfers(session)
    assert (still.id, still.status, still.out_transaction_id, still.in_transaction_id) == (
        transfer.id,
        "rejected",
        record.id,
        None,
    )
    assert await contra_account(session, record.id, a.account.id) == ids.unassigned
    assert (await balances_on(session))[b.id] == Decimal(1000)
    # The row's decision stays with the rejection it is part of.
    assert await _rejections(session) == {
        await source_ref(session, record.id): transfer.id,
        ("import", f"test:{row.id}"): transfer.id,
    }
    await assert_consistent(session)
    assert await heal() == 0


async def test_voiding_import_row_restores_placeholder(session, run, ids, imported, heal) -> None:
    a, b, _, record, row = imported

    await run(void_transaction, row.id)
    await heal()

    await _assert_placeholder_back(session, ids, a, b, record)
    assert await heal() == 0


async def test_delete_linked_without_unlink_fails(session, imported) -> None:
    *_, row = imported
    with pytest.raises(IntegrityError):
        async with session.begin_nested():
            await session.execute(
                text("delete from finance.transactions where id = :t"), {"t": row.id}
            )
    [transfer] = await list_transfers(session)
    assert (transfer.status, transfer.in_transaction_id) == ("matched", row.id)
    await assert_consistent(session)


async def test_removing_identifier_reverts_to_unassigned(session, run, ids, inferred, heal) -> None:
    a, b, identifier, record, _ = inferred

    await run(unassign_identifier_and_rematch, identifier)

    assert await list_transfers(session) == []
    assert await placeholders(session) == []
    assert await contra_account(session, record.id, a.account.id) == ids.unassigned
    assert (await balances_on(session))[b.id] == Decimal(1000)
    await assert_consistent(session)
    assert await heal() == 0


async def test_weekly_transfers_each_match_their_own_week(
    session, run, ids, gbp, connected_bank, manual_bank, connector_tx, record_import, heal
) -> None:
    # A weekly standing order from A to B: Monzo stamps each payment at 09:00, and B's
    # statement rows carry only their date (00:00), so a receive is 9 hours before its send
    # and exactly a week minus 9 hours after the previous one.
    a = await connected_bank("A")
    b = await manual_bank("B", snapshot_at=WHEN - timedelta(days=30))
    identifier = await run(get_or_create_identifier, "uk_bank_account", B_IDENTIFIER)
    midnight = WHEN.replace(hour=0)
    weeks = range(4)
    sends = [
        await connector_tx(
            a,
            "-50",
            external_id=f"so_{k}",
            occurred_at=midnight + timedelta(days=7 * k, hours=9),
            identifier_id=identifier,
        )
        for k in weeks
    ]
    rows = []
    for k in weeks:
        contra = await transfer_contra(
            session,
            source=None,
            own_account_id=b.id,
            counterparty_identifier_id=None,
            other_postings=0,
        )
        rows.append(
            await run(
                post_transaction,
                TransactionInput(
                    occurred_at=midnight + timedelta(days=7 * k),
                    description="Standing order from A",
                    origin="manual",
                    postings=[
                        PostingInput(b.id, gbp.id, Decimal(50)),
                        PostingInput(contra, gbp.id, Decimal(-50)),
                    ],
                    counterparty=CounterpartyFields(bank_transfer=True),
                ),
            )
        )
    await record_import(b.id, [r.id for r in rows])

    await heal()

    transfers = await list_transfers(session)
    assert sorted(
        (t.status, t.review_reason, t.out_transaction_id, t.in_transaction_id) for t in transfers
    ) == sorted(("matched", None, s.id, r.id) for s, r in zip(sends, rows, strict=True))
    assert await placeholders(session) == []
    await assert_consistent(session)
    assert await heal() == 0


async def test_preview_match_reads_the_relation_matching_does(
    session, run, gbp, inferred, registered, connector_tx, import_row, record_import, heal
) -> None:
    _, b, b_identifier, sent, _ = inferred
    # C's payment to B is already matched with B's row naming C (mutually identified), so
    # its strongest pair is that one, and a new row naming nobody pairs only with A's.
    c, c_identifier = await registered("C", C_IDENTIFIER)
    await connector_tx(
        c,
        "-100",
        external_id="c_out",
        occurred_at=WHEN + timedelta(hours=1),
        identifier_id=b_identifier,
    )
    contra = await transfer_contra(
        session,
        source=None,
        own_account_id=b.id,
        counterparty_identifier_id=c_identifier,
        other_postings=0,
    )
    from_c = await run(
        post_transaction,
        TransactionInput(
            occurred_at=WHEN + timedelta(hours=2),
            description="Faster payment from C",
            origin="manual",
            postings=[
                PostingInput(b.id, gbp.id, Decimal(100)),
                PostingInput(contra, gbp.id, Decimal(-100)),
            ],
            counterparty=CounterpartyFields(identifier_id=c_identifier, bank_transfer=True),
        ),
    )
    await record_import(b.id, [from_c.id])
    await heal()
    assert sorted(t.status for t in await list_transfers(session)) == ["inferred", "matched"]

    previewed = await preview_match(
        session,
        account_id=b.id,
        instrument_id=gbp.id,
        quantity=Decimal(100),
        occurred_at=ROW_AT,
        counterparty=CounterpartyFields(bank_transfer=True),
        complete=IMPORTED,
    )

    assert previewed == sent.id
    # Posting the row does link it with A's record, as previewed.
    row = await import_row(b)
    await heal()
    await _assert_previewed(session, previewed, row)


async def test_preview_match_applies_the_rows_decisions(session, gbp, inferred) -> None:
    *_, sent, transfer = inferred
    ref = ("import", "statement_1:row_7")  # the stable reference a re-imported row keeps

    async def preview(source=ref):
        return await preview_match(
            session,
            account_id=inferred[1].id,
            instrument_id=gbp.id,
            quantity=Decimal(100),
            occurred_at=ROW_AT,
            counterparty=CounterpartyFields(bank_transfer=True),
            complete=IMPORTED,
            source=source,
        )

    assert await preview() == sent.id
    d = models.TransferDecision
    sent_ref = await source_ref(session, sent.id)
    never = await session.scalar(
        insert(d)
        .values(
            kind="never_pair",
            subject_kind=sent_ref[0],
            subject_ref=sent_ref[1],
            other_kind=ref[0],
            other_ref=ref[1],
        )
        .returning(d.id)
    )
    assert await preview() is None  # the owner unpaired them
    assert await preview(source=None) == sent.id  # another row is not
    await session.execute(
        text("delete from finance.transfer_decisions where id = :d"), {"d": never}
    )
    await session.execute(
        insert(d).values(
            kind="reject", subject_kind=ref[0], subject_ref=ref[1], transfer_id=transfer.id
        )
    )
    assert await preview() is None  # the owner said the row is no transfer


@pytest.fixture
def stable_row(session, run, gbp, record_import):
    """Post a statement row as an M2 import would, under a stable reference: its dedupe key,
    which it keeps when the statement is imported again."""

    async def _post(account, row: str, quantity: str = "100", *, at=ROW_AT, identifier=None):
        contra = await transfer_contra(
            session,
            source=("import", row),
            own_account_id=account.id,
            counterparty_identifier_id=identifier,
            other_postings=0,
        )
        amount = Decimal(quantity)
        posted = await run(
            post_transaction,
            TransactionInput(
                occurred_at=at,
                description="Faster payment",
                origin="manual",
                postings=[
                    PostingInput(account.id, gbp.id, amount),
                    PostingInput(contra, gbp.id, -amount),
                ],
                counterparty=CounterpartyFields(identifier_id=identifier, bank_transfer=True),
            ),
        )
        await record_import(account.id, [posted.id], keys=[row])
        return posted

    return _post


async def _rejections(session) -> dict[tuple[str, str], object]:
    """Each reject decision's reference, with the rejected transfer it names."""
    rows = await session.execute(
        text(
            "select subject_kind, subject_ref, transfer_id from finance.transfer_decisions"
            " where kind = 'reject'"
        )
    )
    return {(r.subject_kind, r.subject_ref): r.transfer_id for r in rows}


async def _assert_rejections_hold(session) -> None:
    """Every record with a reject decision is an own record of the transfer it names."""
    loose = await session.scalar(
        text(
            "select count(*) from finance.transfer_decisions d"
            " join finance.transactions t on d.subject_kind = 'connection'"
            "   and d.subject_ref = t.connection_id::text || ':' || t.external_id"
            "   or d.subject_kind = 'transaction' and d.subject_ref = t.id::text"
            "   or d.subject_kind = 'import' and d.subject_ref in (select r.dedupe_key"
            "     from finance.import_rows r"
            "     where r.transaction_id = t.id and r.status = 'committed')"
            " join finance.transfers tr on tr.id = d.transfer_id"
            " where d.kind = 'reject' and tr.status = 'rejected'"
            "   and t.id is distinct from tr.out_transaction_id"
            "   and t.id is distinct from tr.in_transaction_id"
        )
    )
    assert loose == 0


async def _roll_back(session, run, heal, row) -> None:
    """M2's rollback: unlink the row, void it, its import row rolled back; heal."""
    await _unlink_and_void(session, run, row)
    await heal()


async def _unlink_and_void(session, run, row) -> None:
    await run(unlink_transaction, row.id)
    r = models.ImportRow
    await session.execute(update(r).where(r.transaction_id == row.id).values(status="rolled_back"))
    await run(void_transaction, row.id)


async def test_reimporting_a_rejected_row_keeps_the_rejection(
    session, run, ids, inferred, stable_row, heal
) -> None:
    # A row naming nobody, matched with A's payment; the owner rejects the pair; the
    # statement is rolled back and imported again.
    _, b, _, sent, _ = inferred
    row = await stable_row(b, "row1")
    await heal()
    [transfer] = await list_transfers(session)
    assert (transfer.status, transfer.in_transaction_id) == ("matched", row.id)
    await run(reject, transfer.id)
    after_reject = await balances_on(session)

    await _roll_back(session, run, heal, row)
    [held] = await list_transfers(session)
    assert (held.id, held.status, held.out_transaction_id, held.in_transaction_id) == (
        transfer.id,
        "rejected",
        sent.id,
        None,
    )
    again = await stable_row(b, "row1")
    await heal()

    [held] = await list_transfers(session)
    assert (held.id, held.status, held.out_transaction_id, held.in_transaction_id) == (
        transfer.id,
        "rejected",
        sent.id,
        again.id,
    )
    assert await contra_account(session, again.id, b.id) == ids.unassigned
    assert await balances_on(session) == after_reject
    assert after_reject.get(ids.in_transit, 0) == 0
    await _assert_rejections_hold(session)
    await assert_consistent(session)
    assert await heal() == 0

    # Undo withdraws both decisions, and the two match again.
    await run(unreject, transfer.id)
    assert await _rejections(session) == {}
    [matched] = await list_transfers(session)
    assert (matched.status, matched.out_transaction_id, matched.in_transaction_id) == (
        "matched",
        sent.id,
        again.id,
    )
    await assert_consistent(session)
    assert await heal() == 0


async def test_reimported_weekly_rows_go_back_to_their_own_rejection(
    session, run, ids, connected_bank, manual_bank, connector_tx, stable_row, heal
) -> None:
    # Two weekly payments of 50 from A to B, each matched with B's row and rejected.
    a = await connected_bank("A")
    b = await manual_bank("B", snapshot_at=WHEN - timedelta(days=30))
    identifier = await run(get_or_create_identifier, "uk_bank_account", B_IDENTIFIER)
    weeks = (0, 1)
    sends = [
        await connector_tx(
            a,
            "-50",
            external_id=f"so_{k}",
            occurred_at=WHEN + timedelta(days=7 * k),
            identifier_id=identifier,
        )
        for k in weeks
    ]
    rows = [
        await stable_row(b, f"row{k}", "50", at=WHEN + timedelta(days=7 * k, hours=1))
        for k in weeks
    ]
    await heal()
    rejected = {}
    for k in weeks:
        [transfer] = [
            t for t in await list_transfers(session) if t.out_transaction_id == sends[k].id
        ]
        assert transfer.in_transaction_id == rows[k].id
        await run(reject, transfer.id)
        rejected[k] = transfer.id

    for row in rows:
        await _roll_back(session, run, heal, row)
    again = [
        await stable_row(b, f"row{k}", "50", at=WHEN + timedelta(days=7 * k, hours=1))
        for k in weeks
    ]
    await heal()

    transfers = await list_transfers(session)
    assert sorted(
        (t.id, t.status, t.out_transaction_id, t.in_transaction_id) for t in transfers
    ) == (sorted((rejected[k], "rejected", sends[k].id, again[k].id) for k in weeks))
    await _assert_rejections_hold(session)
    await assert_consistent(session)
    assert await heal() == 0


async def test_a_rejection_whose_rows_were_all_rolled_back_keeps_its_undo(
    session, run, ids, connected_bank, manual_bank, connector_tx, stable_row, heal
) -> None:
    # B's row names the connected A, whose side hasn't arrived: an awaiting transfer with
    # only the row, which the owner rejects.
    a = await connected_bank("A")
    await connector_tx(a, "-5", external_id="card_1", bank_transfer=False)
    a_identifier = (await run(assign_identifier, a.account.id, "uk_bank_account", A_IDENTIFIER)).id
    b = await manual_bank("B")
    row = await stable_row(b, "row1", identifier=a_identifier)
    await heal()
    [transfer] = await list_transfers(session)
    assert (transfer.status, transfer.out_transaction_id, transfer.in_transaction_id) == (
        "awaiting",
        None,
        row.id,
    )
    await run(reject, transfer.id)
    worth = await balances_on(session)
    row_ref = await source_ref(session, row.id)

    # Rolled back, the rejection stays, with no record left, and still has its Undo.
    await _roll_back(session, run, heal, row)
    [held] = await list_transfers(session)
    assert (held.id, held.status, held.out_transaction_id, held.in_transaction_id) == (
        transfer.id,
        "rejected",
        None,
        None,
    )
    assert await _rejections(session) == {row_ref: transfer.id}
    await assert_consistent(session)
    assert await heal() == 0

    # Imported again, the row goes back into it, on Unassigned.
    again = await stable_row(b, "row1", identifier=a_identifier)
    assert await contra_account(session, again.id, b.id) == ids.unassigned
    await heal()
    [held] = await list_transfers(session)
    assert (held.id, held.status, held.in_transaction_id) == (transfer.id, "rejected", again.id)
    assert await balances_on(session) == worth
    await _assert_rejections_hold(session)
    await assert_consistent(session)
    assert await heal() == 0

    # Undo withdraws the decision: the row opens its transfer again.
    await run(unreject, transfer.id)
    assert await _rejections(session) == {}
    [reopened] = await list_transfers(session)
    assert (reopened.status, reopened.in_transaction_id) == ("awaiting", again.id)
    assert await contra_account(session, again.id, b.id) == ids.in_transit
    await assert_consistent(session)
    assert await heal() == 0


async def test_undo_of_a_rolled_back_rejection_withdraws_its_decisions(
    session, run, ids, connected_bank, manual_bank, connector_tx, stable_row, heal
) -> None:
    # As above, but the owner undoes the rejection while its row is rolled back: the row
    # imported after that is no longer rejected.
    a = await connected_bank("A")
    await connector_tx(a, "-5", external_id="card_1", bank_transfer=False)
    a_identifier = (await run(assign_identifier, a.account.id, "uk_bank_account", A_IDENTIFIER)).id
    b = await manual_bank("B")
    row = await stable_row(b, "row1", identifier=a_identifier)
    await heal()
    [transfer] = await list_transfers(session)
    await run(reject, transfer.id)
    await _roll_back(session, run, heal, row)

    await run(unreject, transfer.id)

    assert await list_transfers(session) == []
    assert await _rejections(session) == {}
    again = await stable_row(b, "row1", identifier=a_identifier)
    await heal()
    [reopened] = await list_transfers(session)
    assert (reopened.status, reopened.in_transaction_id) == ("awaiting", again.id)
    await assert_consistent(session)
    assert await heal() == 0


async def _preview(session, gbp, account, quantity: str, identifier=None):
    return await preview_match(
        session,
        account_id=account.id,
        instrument_id=gbp.id,
        quantity=Decimal(quantity),
        occurred_at=ROW_AT,
        counterparty=CounterpartyFields(identifier_id=identifier, bank_transfer=True),
        complete=IMPORTED,
    )


async def test_preview_partner_without_a_transfer(
    session, run, gbp, registered, manual_bank, connector_tx, stable_row, heal
) -> None:
    # A's payment names nobody, so it has no transfer; a row on D naming A pairs with it.
    a, a_identifier = await registered("A", A_IDENTIFIER)
    d = await manual_bank("D", identifier=None)
    sent = await connector_tx(a, "-100")
    await heal()
    assert await list_transfers(session) == []

    previewed = await _preview(session, gbp, d, "100", a_identifier)

    assert previewed == sent.id
    row = await stable_row(d, "row1", identifier=a_identifier)
    await heal()
    await _assert_previewed(session, previewed, row)
    await assert_consistent(session)


async def test_preview_takeover(
    session, run, gbp, registered, manual_bank, connector_tx, stable_row, heal
) -> None:
    # A's payment to C is matched with C's row naming nobody; a row on C naming A is the
    # stronger pair, so posting it takes A's record over.
    a, a_identifier = await registered("A", A_IDENTIFIER)
    c = await manual_bank("C", identifier=C_IDENTIFIER)
    c_identifier = await run(get_or_create_identifier, "uk_bank_account", C_IDENTIFIER)
    sent = await connector_tx(a, "-100", identifier_id=c_identifier)
    weaker = await stable_row(c, "x")
    await heal()
    [transfer] = await list_transfers(session)
    assert (transfer.status, transfer.in_transaction_id) == ("matched", weaker.id)

    previewed = await _preview(session, gbp, c, "100", a_identifier)

    assert previewed == sent.id
    row = await stable_row(c, "row1", identifier=a_identifier)
    await heal()
    await _assert_previewed(session, previewed, row)
    assert all(
        weaker.id not in (t.out_transaction_id, t.in_transaction_id)
        for t in await list_transfers(session)
    )
    await assert_consistent(session)
