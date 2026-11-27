"""References on a shared payee bank account (account type catalog design §8.1): a record
naming a bank account registered to no account, whose payment reference matches one of the
references it carries, names that reference's account, as if the bank account were
registered to it. eligible_records (pairing), transfer_contra (the contra a record posts
to) and preview_match read it through one definition (identifiers.named_account)."""

from dataclasses import dataclass
from datetime import UTC, datetime, time
from decimal import Decimal
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from lararium.modules.finance import models
from lararium.modules.finance.accounts import AccountRecord
from lararium.modules.finance.identifiers import (
    assign_identifier,
    get_or_create_identifier,
    named_account,
)
from lararium.modules.finance.instruments import InstrumentRecord
from lararium.modules.finance.ledger import CounterpartyFields
from lararium.modules.finance.system_accounts import system_account_ids
from lararium.modules.finance.transfers import (
    list_transfers,
    load_transfer_settings,
    preview_match,
    register_references_and_rematch,
    suggestions,
)
from lararium.modules.finance.transfers.contra import eligible_records
from lararium.modules.finance.transfers.pairs import ONE_SIDED, pair_relation

from .conftest import (
    DAY,
    IMPORTED,
    Connected,
    ConnectorTx,
    Heal,
    ImportedBank,
    MakeConnected,
    RowDetails,
    Run,
    StatementRow,
    SystemIds,
    assert_consistent,
    contra_account,
)

PAYEE = "121212:34343434"  # a platform's client money account, registered to no account
CONNECTED_ID = "565656:78787878"  # M's own bank account


@dataclass(frozen=True)
class Accounts:
    m: Connected  # a connected bank account
    a: AccountRecord  # imported accounts, registered under nothing
    b: AccountRecord


@pytest.fixture
async def accounts(
    connected_bank: MakeConnected, imported_bank: ImportedBank, ids: SystemIds
) -> Accounts:
    return Accounts(await connected_bank("M"), await imported_bank("A"), await imported_bank("B"))


async def _register(run: Run, references: list[tuple[UUID, str]]) -> UUID:
    record = await run(register_references_and_rematch, PAYEE, references)
    return record.id


@pytest.fixture
async def payee(run: Run, accounts: Accounts) -> UUID:
    """The shared payee, carrying (A, "REF-A") and (B, "REF-B")."""
    return await _register(run, [(accounts.a.id, "REF-A"), (accounts.b.id, "REF-B")])


async def _eligible_row(session: AsyncSession, transaction_id: UUID) -> Any:
    e = eligible_records(await system_account_ids(session))
    return (await session.execute(select(e).where(e.c.transaction_id == transaction_id))).one()


async def _tiers(session: AsyncSession) -> Any:
    relation = await pair_relation(
        session, await system_account_ids(session), await load_transfer_settings(session)
    )
    return relation.tiers


async def test_named_account(session: AsyncSession, run: Run, accounts: Accounts, payee: UUID):
    assert await named_account(session, payee, "REF-A") == accounts.a.id
    assert await named_account(session, payee, " ref-b ") == accounts.b.id
    for unmatched in (None, "", "REF-C", "REF A"):
        assert await named_account(session, payee, unmatched) is None
    # A bank account registered to an account names it, whatever the reference.
    own = await run(assign_identifier, accounts.a.id, "uk_bank_account", CONNECTED_ID)
    assert await named_account(session, own.id, "REF-B") == accounts.a.id
    assert await named_account(session, own.id, None) == accounts.a.id


async def test_a_matching_reference_pairs_with_its_account(
    session: AsyncSession,
    accounts: Accounts,
    payee: UUID,
    ids: SystemIds,
    connector_tx: ConnectorTx,
    statement_row: StatementRow,
    heal: Heal,
) -> None:
    m = accounts.m
    sent = await connector_tx(m, "-100", identifier_id=payee, reference=" ref-a ")
    # In transit from the moment it is posted, not only once healed.
    assert await contra_account(session, sent.id, m.account.id) == ids.in_transit
    received = await statement_row(accounts.a, "100")
    await heal()

    [transfer] = await list_transfers(session)
    assert (transfer.status, transfer.out_transaction_id, transfer.in_transaction_id) == (
        "matched",
        sent.id,
        received,
    )
    assert (transfer.from_account_id, transfer.to_account_id) == (m.account.id, accounts.a.id)
    assert (await _tiers(session))[sent.id][received] == ONE_SIDED
    assert await contra_account(session, received, accounts.a.id) == ids.in_transit
    await assert_consistent(session)


@pytest.mark.parametrize("b_row", [False, True])
async def test_another_reference_names_another_account(
    session: AsyncSession,
    accounts: Accounts,
    payee: UUID,
    ids: SystemIds,
    connector_tx: ConnectorTx,
    statement_row: StatementRow,
    heal: Heal,
    b_row: bool,
) -> None:
    sent = await connector_tx(accounts.m, "-100", identifier_id=payee, reference="REF-B")
    a_row = await statement_row(accounts.a, "100")
    received = await statement_row(accounts.b, "100") if b_row else None
    await heal()

    transfers = await list_transfers(session)
    [own] = [t for t in transfers if sent.id == t.out_transaction_id]
    assert own.to_account_id == accounts.b.id
    assert own.in_transaction_id == received
    if b_row:
        assert own.status == "matched"
    assert not any(a_row in (t.out_transaction_id, t.in_transaction_id) for t in transfers)
    assert await contra_account(session, a_row, accounts.a.id) == ids.unassigned
    await assert_consistent(session)


@pytest.mark.parametrize("reference", [None, "REF-C", "REF A"])
async def test_a_reference_matching_none_names_nobody(
    session: AsyncSession,
    run: Run,
    accounts: Accounts,
    payee: UUID,
    ids: SystemIds,
    connector_tx: ConnectorTx,
    statement_row: StatementRow,
    heal: Heal,
    reference: str | None,
) -> None:
    m = accounts.m
    await run(assign_identifier, m.account.id, "uk_bank_account", CONNECTED_ID)
    sent = await connector_tx(m, "-100", identifier_id=payee, reference=reference)
    assert await contra_account(session, sent.id, m.account.id) == ids.unassigned
    # A's row prints M's sort code alone, so the weak tier would pair it with a record of M
    # naming no bank account.
    received = await statement_row(
        accounts.a, "100", details=RowDetails(sort_code=CONNECTED_ID.split(":")[0])
    )
    await heal()

    assert await list_transfers(session) == []
    assert await contra_account(session, sent.id, m.account.id) == ids.unassigned
    assert await contra_account(session, received, accounts.a.id) == ids.unassigned
    row = await _eligible_row(session, sent.id)
    assert (row.holder, row.names_account) == (None, True)
    assert (await _eligible_row(session, received)).weak_holder == m.account.id
    tiers = await _tiers(session)
    assert not tiers.get(sent.id) and not any(sent.id in found for found in tiers.values())
    await assert_consistent(session)


@pytest.mark.parametrize("reference", ["REF-A", " ref-a ", "REF-C", None])
async def test_preview_match_reads_the_reference(
    session: AsyncSession,
    gbp: InstrumentRecord,
    accounts: Accounts,
    payee: UUID,
    imported_bank: ImportedBank,
    statement_row: StatementRow,
    heal: Heal,
    reference: str | None,
) -> None:
    x = await imported_bank("X")
    received = await statement_row(accounts.a, "100")
    await heal()

    previewed = await preview_match(
        session,
        account_id=x.id,
        instrument_id=gbp.id,
        quantity=Decimal("-100"),
        occurred_at=datetime.combine(DAY, time(), UTC),
        counterparty=CounterpartyFields(
            identifier_id=payee, reference=reference, bank_transfer=True
        ),
        complete=IMPORTED,
    )
    matches = reference is not None and reference.strip().upper() == "REF-A"
    assert previewed == (received if matches else None)

    # Posted, it gets the same answer.
    sent = await statement_row(x, "-100", identifier_id=payee, reference=reference)
    await heal()
    matched = [
        t
        for t in await list_transfers(session, statuses=["matched"])
        if sent in (t.out_transaction_id, t.in_transaction_id)
    ]
    if previewed is None:
        assert matched == []
    else:
        [transfer] = matched
        assert {transfer.out_transaction_id, transfer.in_transaction_id} == {previewed, sent}
    await assert_consistent(session)


async def test_a_payee_with_references_is_no_suggestion(
    session: AsyncSession, run: Run, accounts: Accounts, connector_tx: ConnectorTx
) -> None:
    identifier = await run(get_or_create_identifier, "uk_bank_account", PAYEE)
    await connector_tx(accounts.m, "-100", identifier_id=identifier, reference="REF-A")

    async def suggested() -> bool:
        return identifier in [s.identifier_id for s in await suggestions(session)]

    assert await suggested()
    await _register(run, [(accounts.a.id, "REF-A")])
    assert not await suggested()
    await _register(run, [])
    assert await suggested()


async def test_changing_a_reference_moves_the_pair(
    session: AsyncSession,
    run: Run,
    accounts: Accounts,
    ids: SystemIds,
    connector_tx: ConnectorTx,
    statement_row: StatementRow,
    heal: Heal,
) -> None:
    payee = await _register(run, [(accounts.a.id, "REF-A")])
    sent = await connector_tx(accounts.m, "-100", identifier_id=payee, reference="REF-A")
    a_row = await statement_row(accounts.a, "100")
    b_row = await statement_row(accounts.b, "100")
    await heal()
    [transfer] = await list_transfers(session)
    assert (transfer.out_transaction_id, transfer.in_transaction_id) == (sent.id, a_row)

    await _register(run, [(accounts.b.id, "REF-A")])

    [transfer] = await list_transfers(session)
    assert (transfer.status, transfer.out_transaction_id, transfer.in_transaction_id) == (
        "matched",
        sent.id,
        b_row,
    )
    assert transfer.to_account_id == accounts.b.id
    assert await contra_account(session, a_row, accounts.a.id) == ids.unassigned
    assert await heal() == 0
    await assert_consistent(session)


async def test_a_dismissed_payee_with_references_names_alike_everywhere(
    session: AsyncSession,
    accounts: Accounts,
    payee: UUID,
    ids: SystemIds,
    connector_tx: ConnectorTx,
) -> None:
    """A dismissal racing a registration can leave a dismissed bank account carrying
    references (dismiss_identifier reads them unlocked). Its references are read as any
    other's, as a card merchant's holders are, by each definition alike; and it is no
    suggestion."""
    i = models.AccountIdentifier
    await session.execute(update(i).where(i.id == payee).values(dismissed_at=datetime.now(UTC)))
    sent = await connector_tx(accounts.m, "-100", identifier_id=payee, reference="REF-A")

    assert await named_account(session, payee, "REF-A") == accounts.a.id
    assert await contra_account(session, sent.id, accounts.m.account.id) == ids.in_transit
    assert (await _eligible_row(session, sent.id)).holder == accounts.a.id
    assert payee not in [s.identifier_id for s in await suggestions(session)]
