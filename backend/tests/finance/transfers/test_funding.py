"""A deposit whose method the source doesn't state (account type catalog design §8.2): on an
account whose type lists `bank_transfer` among its funding methods, a record naming nobody
(no own account, no card merchant, no bank account) pairs with an identified bank transfer
naming its account, as one marked a bank transfer would (pairs.registry_tier's ONE_SIDED).
The type is read at pairing time (contra.eligible_records' funded_by_transfer); the record's
stored bank_transfer flag is left as its source gave it. weak_tier and card_tier don't
change, and card evidence comes first: a deposit a card payment could have paid gets no edge
through its account's funding alone."""

from collections.abc import Awaitable, Callable
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from lararium.core.connections import create_connection
from lararium.modules.finance import models
from lararium.modules.finance.account_types import keys_funded_by
from lararium.modules.finance.accounts import create_account, set_type_key
from lararium.modules.finance.identifiers import (
    assign_identifier,
    get_or_create_identifier,
    register_card_merchant,
)
from lararium.modules.finance.instruments import InstrumentRecord
from lararium.modules.finance.ledger import CounterpartyFields
from lararium.modules.finance.system_accounts import system_account_ids
from lararium.modules.finance.transfers import (
    TransferRecord,
    list_transfers,
    load_transfer_settings,
    preview_match,
)
from lararium.modules.finance.transfers.contra import eligible_records
from lararium.modules.finance.transfers.pairs import (
    ONE_SIDED,
    WEAK,
    Pairable,
    _may_be_transfer,
    card_tier,
    pair_relation,
    registry_tier,
    weak_tier,
)
from lararium.modules.finance.transfers.settings import DEFAULTS

from .conftest import (
    IMPORTED,
    WHEN,
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

NAMED = "131313:24242424"  # the bank account a transfer names, registered to its account
STRANGER = "500000:50000001"  # someone else's bank account
NEAR = DEFAULTS.near_window
INVEST, STOCKS_ISA, CASH_ISA = "trading212.invest", "trading212.stocks_isa", "trading212.cash_isa"


def test_the_trading212_types_are_funded_by_bank_transfer() -> None:
    """What the database tests below rest on (account_types.py says it, nothing here)."""
    assert {INVEST, STOCKS_ISA, CASH_ISA} <= keys_funded_by("bank_transfer")


# The tiers on their own.

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
        "fiat": True,
    }
    return Pairable(account_id=account, quantity=Decimal(quantity), **(defaults | kw))


# A bank transfer naming X (its counterparty bank account registered to X).
SENT = _pairable(M, "-100", holder=X, bank_transfer=True, names_account=True)


def test_may_be_transfer() -> None:
    assert _may_be_transfer(_pairable(X, "100", bank_transfer=True))
    assert _may_be_transfer(_pairable(X, "100", bank_transfer=True, names_account=True))
    assert _may_be_transfer(_pairable(X, "100", funded_by_transfer=True))
    for other in (
        _pairable(X, "100"),  # not funded
        _pairable(X, "100", funded_by_transfer=True, names_account=True),  # a bank account
        _pairable(X, "100", funded_by_transfer=True, holder=M),  # an own account
        _pairable(X, "100", funded_by_transfer=True, holder=X),  # its own account
        _pairable(X, "100", funded_by_transfer=True, holders=frozenset({X})),  # a merchant
    ):
        assert not _may_be_transfer(other), other


def test_registry_tier_takes_an_unmarked_deposit_on_a_funded_account() -> None:
    deposit = _pairable(X, "100", funded_by_transfer=True)
    assert registry_tier(SENT, deposit) == registry_tier(deposit, SENT) == ONE_SIDED
    # The other way round too: a withdrawal to an identified receipt naming its account.
    withdrawal = _pairable(X, "-100", funded_by_transfer=True)
    receipt = _pairable(M, "100", holder=X, bank_transfer=True, names_account=True)
    assert registry_tier(withdrawal, receipt) == ONE_SIDED
    refused = [
        _pairable(X, "100"),  # its account isn't funded by bank transfer
        _pairable(X, "100", funded_by_transfer=True, names_account=True),  # names a bank account
        _pairable(X, "100", funded_by_transfer=True, holders=frozenset({X})),  # a card merchant
        _pairable(X, "100", funded_by_transfer=True, holder=X),  # names its own account
        _pairable(Y, "100", funded_by_transfer=True),  # on another account than SENT names
    ]
    for other in refused:
        assert registry_tier(SENT, other) is None, other
        assert registry_tier(other, SENT) is None, other
    # Neither identified: a bank transfer naming nobody, or one naming a bank account only.
    for unidentified in (
        _pairable(M, "-100", bank_transfer=True),
        _pairable(M, "-100", bank_transfer=True, names_account=True),
        _pairable(M, "-100", funded_by_transfer=True),
    ):
        assert registry_tier(unidentified, deposit) is None
        assert registry_tier(deposit, unidentified) is None


def test_weak_tier_is_unchanged() -> None:
    """A sort code alone still needs both sides marked bank transfers."""
    for funded in (False, True):
        sent = _pairable(M, "-100", weak_holder=X, funded_by_transfer=funded)
        deposit = _pairable(X, "100", funded_by_transfer=funded)
        assert weak_tier(sent, deposit) is None
        assert weak_tier(deposit, sent) is None
        marked = replace(sent, bank_transfer=True), replace(deposit, bank_transfer=True)
        assert weak_tier(*marked) == weak_tier(*reversed(marked)) == WEAK


def test_card_tier_is_unchanged() -> None:
    """test_card_deposits.test_card_tier's cases give the same with either side funded."""
    at = WHEN

    def p(account: UUID, quantity: str, **kw: Any) -> Pairable:
        return _pairable(account, quantity, **({"source": ("connection", f"{uuid4()}")} | kw))

    card = p(M, "-50", holders=frozenset({X, Y}))
    cases = [
        p(X, "50", occurred_at=at + NEAR),
        p(X, "50", occurred_at=at + NEAR + timedelta(seconds=1)),
        p(X, "49.99"),
        p(X, "-50"),
        p(X, "50", instrument_id=UUID(int=2)),
        p(M, "50"),
        p(X, "50", holder=M),
        p(X, "50", holders=frozenset({M})),
        p(X, "50", names_account=True, bank_transfer=True),
        p(X, "50", source=("import", "k")),
        p(X, "50", source=("transaction", "t")),
    ]
    by_hand = _pairable(M, "-50", holders=frozenset({X}), source=("import", "k"))
    for payer in (card, by_hand):
        for other in cases:
            expected = card_tier(payer, other, NEAR), card_tier(other, payer, NEAR)
            for a, b in ((True, False), (False, True), (True, True)):
                funded = replace(payer, funded_by_transfer=a), replace(other, funded_by_transfer=b)
                assert (card_tier(*funded, NEAR), card_tier(*reversed(funded), NEAR)) == expected
    assert card_tier(card, cases[0], NEAR) == ONE_SIDED  # the cases include a pair


# In the ledger.

type MakeTyped = Callable[..., Awaitable[Connected]]


@pytest.fixture
def trading212(run: Run) -> MakeTyped:
    """A Trading 212 account on a connection of its own, of the type `type_key`."""

    async def _make(name: str, type_key: str | None) -> Connected:
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
            type_key=type_key,
        )
        return Connected(connection.id, account)

    return _make


async def _named(run: Run, account_id: UUID) -> UUID:
    """The bank account a transfer names, registered to the account."""
    record = await run(assign_identifier, account_id, "uk_bank_account", NAMED)
    return record.id


async def _eligible_row(session: AsyncSession, transaction_id: UUID) -> Any:
    e = eligible_records(await system_account_ids(session))
    return (await session.execute(select(e).where(e.c.transaction_id == transaction_id))).one()


async def _tiers(session: AsyncSession) -> Any:
    relation = await pair_relation(
        session, await system_account_ids(session), await load_transfer_settings(session)
    )
    return relation.tiers


def _including(transfers: list[TransferRecord], transaction_id: UUID) -> list[TransferRecord]:
    return [t for t in transfers if transaction_id in (t.out_transaction_id, t.in_transaction_id)]


@pytest.mark.parametrize("typed", [True, False])
async def test_an_identified_bank_transfer_pairs_with_an_unmarked_deposit(
    session: AsyncSession,
    run: Run,
    ids: SystemIds,
    connected_bank: MakeConnected,
    trading212: MakeTyped,
    connector_tx: ConnectorTx,
    heal: Heal,
    typed: bool,
) -> None:
    """A Trading 212 Invest DEPOSIT (not marked a bank transfer, naming nobody) and a
    connected bank's bank transfer naming a bank account registered to Invest: they
    pair, though Invest is no card merchant's holder. Untyped, they don't."""
    m = await connected_bank("M")
    invest = await trading212("Invest", INVEST if typed else None)
    named = await _named(run, invest.account.id)
    deposit = await connector_tx(invest, "100", external_id="deposit", bank_transfer=False)
    sent = await connector_tx(m, "-100", identifier_id=named)
    await heal()

    assert (await session.scalars(select(models.AccountIdentifierHolder))).all() == []
    row = await _eligible_row(session, deposit.id)
    assert (row.funded_by_transfer, row.bank_transfer) == (typed, False)
    assert (row.holder, row.names_account, row.holders) == (None, False, None)
    [own] = _including(await list_transfers(session), sent.id)
    assert (own.from_account_id, own.to_account_id) == (m.account.id, invest.account.id)
    if typed:
        assert (own.status, own.out_transaction_id, own.in_transaction_id) == (
            "matched",
            sent.id,
            deposit.id,
        )
        assert (await _tiers(session))[sent.id][deposit.id] == ONE_SIDED
        assert await contra_account(session, deposit.id, invest.account.id) == ids.in_transit
    else:
        assert own.in_transaction_id is None
        assert not _including(await list_transfers(session), deposit.id)
        assert await contra_account(session, deposit.id, invest.account.id) == ids.unassigned
        assert deposit.id not in (await _tiers(session)).get(sent.id, {})
    assert await heal() == 0
    await assert_consistent(session)


@pytest.mark.parametrize("second", ["on_y", "on_x"])
async def test_only_the_named_account_s_one_deposit(
    session: AsyncSession,
    run: Run,
    ids: SystemIds,
    connected_bank: MakeConnected,
    trading212: MakeTyped,
    connector_tx: ConnectorTx,
    heal: Heal,
    second: str,
) -> None:
    """Review Focus 3: a bank transfer naming X, with same-amount unmarked
    deposits on X and on Y in the window, pairs with X's only; with two on X, with
    neither."""
    m = await connected_bank("M")
    x = await trading212("X", INVEST)
    y = await trading212("Y", STOCKS_ISA)
    named = await _named(run, x.account.id)
    first = await connector_tx(x, "100", external_id="first", bank_transfer=False)
    other = await connector_tx(
        y if second == "on_y" else x,
        "100",
        external_id="second",
        occurred_at=WHEN + timedelta(days=1),
        bank_transfer=False,
    )
    sent = await connector_tx(m, "-100", identifier_id=named)
    await heal()

    transfers = await list_transfers(session)
    [own] = _including(transfers, sent.id)
    assert own.to_account_id == x.account.id
    if second == "on_y":
        assert (own.status, own.in_transaction_id) == ("matched", first.id)
        assert not _including(transfers, other.id)
        assert await contra_account(session, other.id, y.account.id) == ids.unassigned
    else:
        assert own.in_transaction_id is None
        assert own.review_reason == "several_candidates"
        for deposit in (first, other):
            assert not _including(transfers, deposit.id)
            assert await contra_account(session, deposit.id, x.account.id) == ids.unassigned
    assert await heal() == 0
    await assert_consistent(session)


@pytest.mark.parametrize("names", [False, True])
@pytest.mark.parametrize("source", ["connector", "import"])
async def test_a_deposit_naming_a_bank_account_is_no_funded_deposit(
    session: AsyncSession,
    run: Run,
    ids: SystemIds,
    connected_bank: MakeConnected,
    trading212: MakeTyped,
    imported_bank: ImportedBank,
    connector_tx: ConnectorTx,
    statement_row: StatementRow,
    heal: Heal,
    source: str,
    names: bool,
) -> None:
    """Review Focus 4: a record on a bank-transfer-funded account that names a bank account
    (a connector's naming a stranger's, an import row printing a stranger's account number)
    is no funded deposit: the identified bank transfer to its account doesn't pair with it.
    Naming nothing, it does."""
    m = await connected_bank("M")
    invest = await trading212("Invest", INVEST) if source == "connector" else None
    if invest is not None:
        account = invest.account
    else:
        account = await imported_bank("Cash ISA")
        await run(set_type_key, account.id, CASH_ISA)
    named = await _named(run, account.id)
    if invest is not None:
        stranger = await run(get_or_create_identifier, "uk_bank_account", STRANGER)
        deposit = (
            await connector_tx(
                invest,
                "100",
                external_id="deposit",
                identifier_id=stranger if names else None,
                bank_transfer=False,
            )
        ).id
    else:
        printed = RowDetails(account_number=STRANGER.split(":")[1]) if names else None
        deposit = await statement_row(account, "100", details=printed, bank_transfer=False)
    sent = await connector_tx(m, "-100", identifier_id=named)
    await heal()

    row = await _eligible_row(session, deposit)
    assert (row.funded_by_transfer, row.names_account, row.holder) == (True, names, None)
    [own] = _including(await list_transfers(session), sent.id)
    if names:
        assert own.in_transaction_id is None
        assert await contra_account(session, deposit, account.id) == ids.unassigned
        assert deposit not in (await _tiers(session)).get(sent.id, {})
    else:
        assert (own.status, own.in_transaction_id) == ("matched", deposit)
    await assert_consistent(session)


@pytest.mark.parametrize("typed", [True, False])
async def test_preview_match_reads_the_type(
    session: AsyncSession,
    run: Run,
    gbp: InstrumentRecord,
    connected_bank: MakeConnected,
    imported_bank: ImportedBank,
    connector_tx: ConnectorTx,
    statement_row: StatementRow,
    heal: Heal,
    typed: bool,
) -> None:
    """A row not yet posted, unmarked and naming nobody, on a funded account: its preview
    finds the bank transfer naming its account, as posting it then does."""
    m = await connected_bank("M")
    cash_isa = await imported_bank("Cash ISA")
    if typed:
        await run(set_type_key, cash_isa.id, CASH_ISA)
    named = await _named(run, cash_isa.id)
    sent = await connector_tx(m, "-100", identifier_id=named)
    await heal()

    previewed = await preview_match(
        session,
        account_id=cash_isa.id,
        instrument_id=gbp.id,
        quantity=Decimal("100"),
        occurred_at=WHEN,
        counterparty=CounterpartyFields(bank_transfer=False),
        complete=IMPORTED,
    )
    assert previewed == (sent.id if typed else None)

    received = await statement_row(cash_isa, "100", day=WHEN.date(), bank_transfer=False)
    await heal()
    [own] = _including(await list_transfers(session), sent.id)
    assert own.in_transaction_id == (received if typed else None)
    await assert_consistent(session)


async def test_a_type_set_or_cleared_later_is_read_at_the_next_heal(
    session: AsyncSession,
    run: Run,
    ids: SystemIds,
    connected_bank: MakeConnected,
    trading212: MakeTyped,
    connector_tx: ConnectorTx,
    heal: Heal,
) -> None:
    """Decision 4: the type is read at pairing time, so the next heal brings the ledger in
    line with a type set after the records were posted, and with one cleared again."""
    m = await connected_bank("M")
    invest = await trading212("Invest", None)
    named = await _named(run, invest.account.id)
    deposit = await connector_tx(invest, "100", external_id="deposit", bank_transfer=False)
    sent = await connector_tx(m, "-100", identifier_id=named)
    await heal()

    async def deposit_is_paired() -> bool:
        [own] = _including(await list_transfers(session), sent.id)
        contra = await contra_account(session, deposit.id, invest.account.id)
        if own.in_transaction_id == deposit.id:
            assert (own.status, contra) == ("matched", ids.in_transit)
            return True
        assert (own.in_transaction_id, contra) == (None, ids.unassigned)
        return False

    assert not await deposit_is_paired()
    await run(set_type_key, invest.account.id, INVEST)
    await heal()
    assert await deposit_is_paired()
    await assert_consistent(session)
    await run(set_type_key, invest.account.id, None)
    await heal()
    assert not await deposit_is_paired()
    assert await heal() == 0
    await assert_consistent(session)


MERCHANT = "merch_TESTFUND"


@pytest.mark.parametrize("bank_deposit", [True, False])
async def test_card_evidence_comes_first(
    session: AsyncSession,
    run: Run,
    ids: SystemIds,
    connected_bank: MakeConnected,
    trading212: MakeTyped,
    connector_tx: ConnectorTx,
    heal: Heal,
    bank_deposit: bool,
) -> None:
    """A card top-up D1 that the card rule pairs with Monzo's card payment C stays C's when
    an identified bank transfer B of the same amount to its account is in the window: B pairs
    with its own deposit D2 (no card payment near it), or, without D2, with nothing."""
    m = await connected_bank("M")
    invest = await trading212("Invest", INVEST)
    merchant = await run(register_card_merchant, MERCHANT, [invest.account.id])
    named = await _named(run, invest.account.id)
    card = await connector_tx(
        m, "-50", external_id="card", identifier_id=merchant.id, bank_transfer=False
    )
    top_up = await connector_tx(
        invest, "50", external_id="top_up", occurred_at=WHEN + NEAR / 2, bank_transfer=False
    )
    sent = await connector_tx(
        m,
        "-50",
        external_id="bank",
        occurred_at=WHEN + timedelta(hours=3),
        identifier_id=named,
    )
    received = (
        await connector_tx(
            invest,
            "50",
            external_id="deposit",
            occurred_at=WHEN + timedelta(hours=4),
            bank_transfer=False,
        )
        if bank_deposit
        else None
    )
    await heal()

    transfers = await list_transfers(session)
    [by_card] = _including(transfers, card.id)
    assert (by_card.status, by_card.out_transaction_id, by_card.in_transaction_id) == (
        "matched",
        card.id,
        top_up.id,
    )
    [own] = _including(transfers, sent.id)
    assert own.to_account_id == invest.account.id
    if received is not None:
        assert (own.status, own.in_transaction_id) == ("matched", received.id)
        assert own.review_reason is None
    else:
        assert own.in_transaction_id is None
        assert own.review_reason is None
    assert top_up.id not in (await _tiers(session)).get(sent.id, {})
    assert await heal() == 0
    await assert_consistent(session)
