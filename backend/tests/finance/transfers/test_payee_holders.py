"""A payee bank account held by several own accounts (account type catalog design §8.1): a
record naming it is identified to the set of its holders, not to one account. In the
one-sided registry tier its counterpart is a record on one of the holder accounts that may
be a transfer (marked a bank transfer, or funded by bank transfer and naming nobody), by
amount and date window as ever; uniqueness, the coverage gate and card precedence are
unchanged. Until it pairs its contra is Unassigned, as a card merchant's payer's is."""

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, time, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from lararium.core.connections import create_connection
from lararium.modules.finance.accounts import AccountRecord, create_account
from lararium.modules.finance.errors import FinanceValidationError
from lararium.modules.finance.identifiers import dismiss_identifier, get_or_create_identifier
from lararium.modules.finance.instruments import InstrumentRecord
from lararium.modules.finance.ledger import CounterpartyFields
from lararium.modules.finance.system_accounts import system_account_ids
from lararium.modules.finance.transfers import (
    list_transfers,
    load_transfer_settings,
    preview_match,
    register_holders_and_rematch,
    suggestions,
)
from lararium.modules.finance.transfers.contra import eligible_records
from lararium.modules.finance.transfers.pairs import (
    ONE_SIDED,
    Pairable,
    PairRelation,
    pair_relation,
    registry_tier,
    weak_tier,
)

from .conftest import (
    DAY,
    IMPORTED,
    WHEN,
    Connected,
    ConnectorTx,
    Heal,
    ImportedBank,
    MakeConnected,
    Run,
    StatementRow,
    SystemIds,
    assert_consistent,
    contra_account,
    placeholders,
)

PAYEE = "121212:34343434"  # a platform's client money account, held by A and B
INVEST = "trading212.invest"  # a type funded by bank transfer


# The tier on its own.

M, X, Y = UUID(int=10), UUID(int=11), UUID(int=12)


def _pairable(account: UUID, quantity: str, **kw: Any) -> Pairable:
    defaults: dict[str, Any] = {
        "id": uuid4(),
        "source": ("transaction", f"{account}:{quantity}"),
        "instrument_id": UUID(int=1),
        "occurred_at": WHEN,
        "booking_date": WHEN.date(),
        "holder": None,
        "bank_transfer": False,
    }
    return Pairable(account_id=account, quantity=Decimal(quantity), **(defaults | kw))


# A bank transfer naming the payee X and Y hold.
SENT = _pairable(M, "-100", bank_transfer=True, names_account=True, payee_holders=frozenset({X, Y}))


def test_registry_tier_pairs_a_held_payee_with_a_holder_that_may_be_a_transfer() -> None:
    for other in (
        _pairable(X, "100", bank_transfer=True),
        _pairable(Y, "100", funded_by_transfer=True),  # funded, naming nobody
        # One naming the payer's account is identified: the registry's own rule takes it.
        _pairable(X, "100", bank_transfer=True, holder=M, names_account=True),
    ):
        assert registry_tier(SENT, other) == registry_tier(other, SENT) == ONE_SIDED, other
    for other in (
        _pairable(M, "100", bank_transfer=True),  # on its own account
        _pairable(UUID(int=13), "100", bank_transfer=True),  # on no holder
        _pairable(X, "100"),  # no transfer: unmarked, not funded
        _pairable(X, "100", funded_by_transfer=True, names_account=True),  # names an account
        _pairable(X, "100", bank_transfer=True, holder=Y, names_account=True),  # names Y
    ):
        assert registry_tier(SENT, other) is None and registry_tier(other, SENT) is None, other
    assert weak_tier(SENT, _pairable(X, "100", bank_transfer=True)) is None


def test_a_held_payee_s_record_pairs_with_none_outside_its_holders() -> None:
    """A record naming a payee only its holders hold says where its money went: a receipt
    on another own account, even one naming the payer's account, is not its other side."""
    sent = _pairable(
        M, "-100", bank_transfer=True, names_account=True, payee_holders=frozenset({X})
    )
    elsewhere = _pairable(Y, "100", bank_transfer=True, holder=M, names_account=True)
    assert registry_tier(sent, elsewhere) is None and registry_tier(elsewhere, sent) is None


def test_a_card_merchant_s_holders_are_no_payee_s() -> None:
    """The two sets mean different evidence: a card payment's holders feed only card_tier."""
    card = _pairable(M, "-100", holders=frozenset({X}), source=("connection", "c:1"))
    assert registry_tier(card, _pairable(X, "100", bank_transfer=True)) is None
    assert registry_tier(card, _pairable(X, "100", funded_by_transfer=True)) is None


# In the ledger.

type MakeTyped = Callable[[str], Awaitable[Connected]]


@pytest.fixture
def trading212(run: Run) -> MakeTyped:
    """A Trading 212 Invest account on a connection of its own (funded by bank transfer)."""

    async def _make(name: str) -> Connected:
        connection = await run(
            create_connection, connector="trading212", display_name=name, config={}
        )
        account = await run(
            create_account,
            name=name,
            ledger_type="asset",
            subtype="broker",
            balance_mode="transactions",
            source_connection_id=connection.id,
            type_key=INVEST,
        )
        return Connected(connection.id, account)

    return _make


async def _register(run: Run, *holders: AccountRecord) -> UUID:
    record = await run(
        register_holders_and_rematch, "uk_bank_account", PAYEE, [h.id for h in holders]
    )
    return record.id


async def _relation(session: AsyncSession) -> PairRelation:
    return await pair_relation(
        session, await system_account_ids(session), await load_transfer_settings(session)
    )


async def _eligible_row(session: AsyncSession, transaction_id: UUID) -> Any:
    e = eligible_records(await system_account_ids(session))
    return (await session.execute(select(e).where(e.c.transaction_id == transaction_id))).one()


@pytest.mark.parametrize("on", ["b", "a"])
async def test_a_transfer_to_the_payee_pairs_with_its_one_holder_deposit(
    session: AsyncSession,
    run: Run,
    ids: SystemIds,
    connected_bank: MakeConnected,
    trading212: MakeTyped,
    imported_bank: ImportedBank,
    connector_tx: ConnectorTx,
    statement_row: StatementRow,
    heal: Heal,
    on: str,
) -> None:
    """A connected bank's bank transfer naming the payee held by A (a connector's account
    funded by bank transfer) and B (a manual import account) pairs ONE_SIDED with the one
    same-amount deposit on either that may be a transfer: an unmarked one on A, a bank
    transfer on B."""
    m = await connected_bank("M")
    a = await trading212("A")
    b = await imported_bank("B")
    payee = await _register(run, a.account, b)
    sent = await connector_tx(m, "-100", identifier_id=payee)
    # Identified to no one account: Unassigned until it pairs.
    assert await contra_account(session, sent.id, m.account.id) == ids.unassigned
    row = await _eligible_row(session, sent.id)
    assert (row.holder, row.holders, row.names_account) == (None, None, True)
    assert set(row.payee_holders) == {a.account.id, b.id}
    if on == "a":
        # B's imports list in full (the coverage gate asks it of every manual holder).
        await statement_row(b, "-7")
        received = (await connector_tx(a, "100", external_id="deposit", bank_transfer=False)).id
        account_id = a.account.id
    else:
        received = await statement_row(b, "100")
        account_id = b.id
    await heal()

    [transfer] = await list_transfers(session)
    assert (transfer.status, transfer.out_transaction_id, transfer.in_transaction_id) == (
        "matched",
        sent.id,
        received,
    )
    assert (transfer.from_account_id, transfer.to_account_id) == (m.account.id, account_id)
    assert (await _relation(session)).tiers[sent.id][received] == ONE_SIDED
    assert await contra_account(session, sent.id, m.account.id) == ids.in_transit
    assert await contra_account(session, received, account_id) == ids.in_transit
    assert await heal() == 0
    await assert_consistent(session)


@pytest.mark.parametrize("covered", [False, True])
async def test_every_manual_holder_is_held_to_the_coverage_gate(
    session: AsyncSession,
    run: Run,
    ids: SystemIds,
    connected_bank: MakeConnected,
    trading212: MakeTyped,
    imported_bank: ImportedBank,
    connector_tx: ConnectorTx,
    statement_row: StatementRow,
    heal: Heal,
    covered: bool,
) -> None:
    """The payment's real other side may be on any holder: a deposit on the connected A is
    taken only once the manual B's imports list in full from before the window. Until
    then it stays the payment's candidate, one the rule can't take."""
    m = await connected_bank("M")
    a = await trading212("A")
    b = await imported_bank("B")
    payee = await _register(run, a.account, b)
    if covered:
        early = WHEN.date() - timedelta(days=30)
        await statement_row(b, "-7", complete=(early, WHEN.date() + timedelta(days=60)))
    sent = await connector_tx(m, "-100", identifier_id=payee)
    on_a = (await connector_tx(a, "100", external_id="deposit", bank_transfer=False)).id
    await heal()

    relation = await _relation(session)
    assert relation.candidates(sent.id) == [on_a]
    assert relation.takeable(sent.id, on_a) is covered
    matched = [t for t in await list_transfers(session) if t.status == "matched"]
    if covered:
        assert [(t.out_transaction_id, t.in_transaction_id) for t in matched] == [(sent.id, on_a)]
    else:
        assert matched == []
        assert relation.review_reason(sent.id) == "not_listed_in_full"
        assert await contra_account(session, on_a, a.account.id) == ids.unassigned
    assert await heal() == 0
    await assert_consistent(session)


async def test_deposits_on_two_holders_are_several_candidates(
    session: AsyncSession,
    run: Run,
    ids: SystemIds,
    connected_bank: MakeConnected,
    trading212: MakeTyped,
    imported_bank: ImportedBank,
    connector_tx: ConnectorTx,
    statement_row: StatementRow,
    heal: Heal,
) -> None:
    """With a fitting deposit on A and one on B, the rule can't choose: several_candidates,
    nothing taken, and no placeholder on the manual account B beside its row."""
    m = await connected_bank("M")
    a = await trading212("A")
    b = await imported_bank("B")
    payee = await _register(run, a.account, b)
    sent = await connector_tx(m, "-100", identifier_id=payee)
    on_a = (await connector_tx(a, "100", external_id="deposit", bank_transfer=False)).id
    on_b = await statement_row(b, "100")
    await heal()

    relation = await _relation(session)
    assert set(relation.candidates(sent.id)) == {on_a, on_b}
    assert relation.review_reason(sent.id) == "several_candidates"
    assert relation.partner(sent.id) is None
    assert await list_transfers(session) == []
    assert await placeholders(session) == []
    assert await contra_account(session, sent.id, m.account.id) == ids.unassigned
    assert await contra_account(session, on_a, a.account.id) == ids.unassigned
    assert await contra_account(session, on_b, b.id) == ids.unassigned
    assert await heal() == 0
    await assert_consistent(session)


async def test_a_deposit_on_an_account_outside_the_set_is_no_pair(
    session: AsyncSession,
    run: Run,
    ids: SystemIds,
    connected_bank: MakeConnected,
    imported_bank: ImportedBank,
    connector_tx: ConnectorTx,
    statement_row: StatementRow,
    heal: Heal,
) -> None:
    m = await connected_bank("M")
    a, b, c = [await imported_bank(name) for name in ("A", "B", "C")]
    payee = await _register(run, a, b)
    sent = await connector_tx(m, "-100", identifier_id=payee)
    received = await statement_row(c, "100")
    await heal()

    assert await list_transfers(session) == []
    assert not (await _relation(session)).tiers.get(sent.id)
    assert await contra_account(session, sent.id, m.account.id) == ids.unassigned
    assert await contra_account(session, received, c.id) == ids.unassigned
    await assert_consistent(session)


async def test_registering_the_holders_matches_again(
    session: AsyncSession,
    run: Run,
    ids: SystemIds,
    connected_bank: MakeConnected,
    imported_bank: ImportedBank,
    connector_tx: ConnectorTx,
    statement_row: StatementRow,
    heal: Heal,
) -> None:
    """Records posted before the holders are registered pair once they are, and come apart
    once the holders are removed; meanwhile the payee is no suggestion, and can't be
    dismissed."""
    m = await connected_bank("M")
    a, b = await imported_bank("A"), await imported_bank("B")
    payee = await run(get_or_create_identifier, "uk_bank_account", PAYEE)
    sent = await connector_tx(m, "-100", identifier_id=payee)
    received = await statement_row(b, "100")
    await statement_row(a, "-7")  # A's imports list in full too
    await heal()

    async def suggested() -> bool:
        return payee in [s.identifier_id for s in await suggestions(session)]

    assert await list_transfers(session) == []
    assert await suggested()

    await _register(run, a, b)

    [transfer] = await list_transfers(session)
    assert (transfer.status, transfer.out_transaction_id, transfer.in_transaction_id) == (
        "matched",
        sent.id,
        received,
    )
    assert not await suggested()
    with pytest.raises(FinanceValidationError, match="holders"):
        await run(dismiss_identifier, payee)
    assert await heal() == 0
    await assert_consistent(session)

    await _register(run)

    assert await list_transfers(session) == []
    assert await contra_account(session, sent.id, m.account.id) == ids.unassigned
    assert await contra_account(session, received, b.id) == ids.unassigned
    assert await suggested()
    await assert_consistent(session)


@pytest.mark.parametrize("held", [True, False])
async def test_preview_match_reads_the_holders(
    session: AsyncSession,
    run: Run,
    gbp: InstrumentRecord,
    imported_bank: ImportedBank,
    statement_row: StatementRow,
    heal: Heal,
    held: bool,
) -> None:
    """An imported row not yet posted, naming the payee: its preview finds B's deposit when
    B holds the payee, as posting it then does."""
    a, b, x = [await imported_bank(name) for name in ("A", "B", "X")]
    payee = (
        await _register(run, a, b)
        if held
        else await run(get_or_create_identifier, "uk_bank_account", PAYEE)
    )
    received = await statement_row(b, "100")
    await statement_row(a, "-7")  # A's imports list in full too
    await heal()

    previewed = await preview_match(
        session,
        account_id=x.id,
        instrument_id=gbp.id,
        quantity=Decimal("-100"),
        occurred_at=datetime.combine(DAY, time(), UTC),
        counterparty=CounterpartyFields(identifier_id=payee, bank_transfer=True),
        complete=IMPORTED,
    )
    assert previewed == (received if held else None)

    sent = await statement_row(x, "-100", identifier_id=payee)
    await heal()
    matched = await list_transfers(session, statuses=["matched"])
    assert [(t.out_transaction_id, t.in_transaction_id) for t in matched] == (
        [(sent, received)] if held else []
    )
    await assert_consistent(session)
