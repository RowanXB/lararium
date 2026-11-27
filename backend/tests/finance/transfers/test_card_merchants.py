"""Card merchants in the registry (eToro design §5.12, amended in Task 15): a Monzo merchant
id registered as paying into several own accounts (Trading 212's Invest and ISA share one).
A card payment naming one stays on Unassigned until it is paired: no existing tier pairs it,
and the owner can pair it by hand."""

from datetime import timedelta
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from sqlalchemy import insert, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from lararium.core.actors import Actor
from lararium.core.commands import command_context
from lararium.modules.finance import models
from lararium.modules.finance.accounts import close_account, create_account
from lararium.modules.finance.errors import FinanceValidationError
from lararium.modules.finance.identifiers import (
    assign_identifier,
    dismiss_identifier,
    get_or_create_identifier,
    list_identifiers,
    register_card_merchant,
)
from lararium.modules.finance.system_accounts import system_account_ids
from lararium.modules.finance.transfers import (
    list_transfers,
    load_transfer_settings,
    pair_records,
    register_card_merchant_and_rematch,
)
from lararium.modules.finance.transfers.contra import eligible_records
from lararium.modules.finance.transfers.pairs import (
    ONE_SIDED,
    Pairable,
    card_tier,
    cross_currency_tier,
    pair_relation,
    registry_tier,
    weak_tier,
)
from lararium.modules.finance.transfers.settings import DEFAULTS

from .conftest import (
    DAY,
    WHEN,
    Connected,
    ConnectorTx,
    Heal,
    MakeConnected,
    Run,
    SystemIds,
    assert_consistent,
    contra_account,
    in_transit_balance,
)

MERCHANT = "merch_TEST212"
MONZO_IDENTIFIER = "998877:87654321"
NEAR = DEFAULTS.near_window


@pytest.fixture
async def platform(connected_bank: MakeConnected) -> tuple[Connected, Connected, Connected]:
    """Monzo, and Trading 212's Invest and ISA, each on a connection of its own."""
    return (
        await connected_bank("Monzo"),
        await connected_bank("Invest"),
        await connected_bank("ISA"),
    )


async def holders_of(session: AsyncSession, identifier_id: UUID) -> set[UUID]:
    h = models.AccountIdentifierHolder
    return set(await session.scalars(select(h.account_id).where(h.identifier_id == identifier_id)))


async def test_register_two_holders(session: AsyncSession, run: Run, platform) -> None:
    _, invest, isa = platform

    record = await run(register_card_merchant, MERCHANT, [invest.account.id, isa.account.id])

    assert (record.kind, record.value, record.account_id) == ("card_merchant", MERCHANT, None)
    assert set(record.holders) == {invest.account.id, isa.account.id}
    assert await holders_of(session, record.id) == {invest.account.id, isa.account.id}
    # Listed on each holder's identifiers.
    for account in (invest, isa):
        [listed] = await list_identifiers(session, account_id=account.account.id)
        assert listed.id == record.id
    # Audited: the change is recorded with its holders.
    audited = await session.scalar(
        text(
            "select count(*) from core.events"
            " where type = 'finance.account_identifier.holders_changed' and subject_id = :i"
        ),
        {"i": record.id},
    )
    assert audited == 1

    # Registering again replaces the set; the same set again changes nothing.
    again = await run(register_card_merchant, MERCHANT, [isa.account.id, isa.account.id])
    assert (again.id, again.holders) == (record.id, (isa.account.id,))
    await run(register_card_merchant, MERCHANT, [isa.account.id])
    assert (
        await session.scalar(
            text(
                "select count(*) from core.events"
                " where type = 'finance.account_identifier.holders_changed' and subject_id = :i"
            ),
            {"i": record.id},
        )
        == 2
    )

    # An empty list removes the registration; the row stays.
    removed = await run(register_card_merchant, MERCHANT, [])
    assert (removed.id, removed.holders) == (record.id, ())
    assert await holders_of(session, record.id) == set()


@pytest.mark.parametrize(
    "case", ["snapshots", "liability", "closed", "system", "unknown", "bad_value"]
)
async def test_register_refusals(
    session: AsyncSession, run: Run, platform, ids: SystemIds, case: str
) -> None:
    _, invest, _ = platform
    if case == "snapshots":
        other = (
            await run(
                create_account,
                name="Snapshots",
                ledger_type="asset",
                subtype="bank",
                balance_mode="snapshots",
            )
        ).id
    elif case == "liability":
        other = (
            await run(
                create_account,
                name="Card",
                ledger_type="liability",
                subtype="other",
                balance_mode="hybrid",
            )
        ).id
    elif case == "closed":
        other = (
            await run(
                create_account,
                name="Old",
                ledger_type="asset",
                subtype="bank",
                balance_mode="hybrid",
            )
        ).id
        await run(close_account, other, DAY)
    elif case == "system":
        other = ids.in_transit
    else:
        other = UUID(int=7)
    merchant = "merch_bad-value" if case == "bad_value" else MERCHANT

    with pytest.raises(FinanceValidationError):
        await run(register_card_merchant, merchant, [invest.account.id, other])

    # Nothing is registered.
    i = models.AccountIdentifier
    found = await session.scalar(select(i.id).where(i.kind == "card_merchant"))
    if found is not None:
        assert await holders_of(session, found) == set()


async def test_other_kinds_still_single_holder(
    session: AsyncSession, run: Run, platform, ids: SystemIds, connector_tx: ConnectorTx
) -> None:
    monzo, invest, _ = platform
    bank = await run(assign_identifier, monzo.account.id, "uk_bank_account", MONZO_IDENTIFIER)
    assert (bank.account_id, bank.holders) == (monzo.account.id, ())
    # A card merchant is registered with its accounts, never assigned to one.
    with pytest.raises(FinanceValidationError):
        await run(assign_identifier, invest.account.id, "card_merchant", MERCHANT)
    # A registered card merchant can't be dismissed; an unregistered one can.
    record = await run(register_card_merchant, MERCHANT, [invest.account.id])
    with pytest.raises(FinanceValidationError):
        await run(dismiss_identifier, record.id)
    await run(register_card_merchant, MERCHANT, [])
    dismissed = await run(dismiss_identifier, record.id)
    assert dismissed.dismissed_at is not None
    # Registering it again takes it back from dismissed.
    back = await run(register_card_merchant, MERCHANT, [invest.account.id])
    assert back.dismissed_at is None
    # eligible_records: a record naming a bank account has that account as its holder, no
    # holders, and names an account; one naming the card merchant has holders, no holder,
    # and names no bank account.
    paying_monzo = await connector_tx(invest, "-20", external_id="to_monzo", identifier_id=bank.id)
    row = await _eligible_row(session, paying_monzo.id)
    assert (row.holder, row.holders, row.names_account) == (monzo.account.id, None, True)
    card = await _card_payment(connector_tx, monzo, record.id)
    row = await _eligible_row(session, card.id)
    assert (row.holder, row.holders, row.names_account) == (None, [invest.account.id], False)
    # A bank account no own account holds: no holder, no holders, names an account.
    stranger = await run(get_or_create_identifier, "uk_bank_account", "112233:12345678")
    paying_stranger = await connector_tx(
        monzo, "-30", external_id="to_stranger", identifier_id=stranger
    )
    row = await _eligible_row(session, paying_stranger.id)
    assert (row.holder, row.holders, row.names_account) == (None, None, True)


async def test_a_holder_row_states_its_identifier_s_kind(
    session: AsyncSession, run: Run, platform
) -> None:
    """A holder row's kind is its identifier's (the foreign key to (id, kind)): a bank
    account's row can't pass for a card merchant's. (Which kinds may have holders at all:
    test_identifiers.)"""
    monzo, invest, _ = platform
    bank = await run(assign_identifier, monzo.account.id, "uk_bank_account", MONZO_IDENTIFIER)
    with pytest.raises(IntegrityError, match="foreign key"):
        async with session.begin_nested():
            await session.execute(
                insert(models.AccountIdentifierHolder).values(
                    identifier_id=bank.id, kind="card_merchant", account_id=invest.account.id
                )
            )


async def test_closing_a_holder_keeps_it(
    session: AsyncSession, run: Run, platform, connector_tx: ConnectorTx, heal: Heal
) -> None:
    """A closed account stays a holder (its card-deposit history stands, as a bank
    identifier stays assigned); a registration keeps it, but can't newly add one."""
    monzo, invest, isa = platform
    record = await run(register_card_merchant, MERCHANT, [invest.account.id, isa.account.id])
    payment = await _card_payment(connector_tx, monzo, record.id)

    await run(close_account, isa.account.id, DAY)

    assert await holders_of(session, record.id) == {invest.account.id, isa.account.id}
    audited = await session.scalar(
        text(
            "select count(*) from core.events"
            " where type = 'finance.account_identifier.holders_changed' and subject_id = :i"
        ),
        {"i": record.id},
    )
    assert audited == 1  # the registration only
    assert set((await _eligible_row(session, payment.id)).holders) == {
        invest.account.id,
        isa.account.id,
    }
    # The holders can still be edited with the closed one kept...
    again = await run(
        register_card_merchant, MERCHANT, [invest.account.id, isa.account.id, monzo.account.id]
    )
    assert set(again.holders) == {invest.account.id, isa.account.id, monzo.account.id}
    # ...or removed (an explicit owner action), after which it can't come back.
    await run(register_card_merchant, MERCHANT, [invest.account.id])
    with pytest.raises(FinanceValidationError):
        await run(register_card_merchant, MERCHANT, [invest.account.id, isa.account.id])
    await heal()
    await assert_consistent(session)


async def _card_payment(
    connector_tx: ConnectorTx, monzo: Connected, identifier_id: UUID | None, **kw
):
    return await connector_tx(
        monzo,
        kw.pop("quantity", "-50"),
        external_id=kw.pop("external_id", "card_1"),
        identifier_id=identifier_id,
        bank_transfer=False,
        description="Trading 212",
        **kw,
    )


async def _deposit(connector_tx: ConnectorTx, account: Connected, **kw):
    return await connector_tx(
        account,
        kw.pop("quantity", "50"),
        external_id=kw.pop("external_id", "deposit_1"),
        bank_transfer=kw.pop("bank_transfer", False),
        description="Deposit",
        **kw,
    )


async def _eligible_row(session: AsyncSession, transaction_id: UUID):
    e = eligible_records(await system_account_ids(session))
    return (
        await session.execute(
            select(
                e.c.holder, e.c.holders, e.c.names_account, e.c.counterparty_identifier_id
            ).where(e.c.transaction_id == transaction_id)
        )
    ).one()


async def test_card_payment_stays_unassigned_until_paired(
    session: AsyncSession,
    run: Run,
    platform,
    ids: SystemIds,
    connector_tx: ConnectorTx,
    heal: Heal,
) -> None:
    monzo, invest, isa = platform
    record = await run(register_card_merchant, MERCHANT, [invest.account.id, isa.account.id])
    payment = await _card_payment(connector_tx, monzo, record.id)
    # An hour later: outside the near window, so Task 16's card tier doesn't pair them.
    deposit = await _deposit(connector_tx, invest, occurred_at=WHEN + timedelta(hours=1))
    await heal()

    row = await _eligible_row(session, payment.id)
    assert row.counterparty_identifier_id == record.id
    assert row.holder is None
    assert set(row.holders) == {invest.account.id, isa.account.id}
    assert await contra_account(session, payment.id, monzo.account.id) == ids.unassigned
    assert await contra_account(session, deposit.id, invest.account.id) == ids.unassigned
    assert await list_transfers(session) == []
    await assert_consistent(session)

    # Loaded into the pair relation with its holders, but nobody's candidate.
    relation = await pair_relation(
        session, await system_account_ids(session), await load_transfer_settings(session)
    )
    loaded = relation.records[payment.id]
    assert loaded.holders == frozenset({invest.account.id, isa.account.id})
    assert not loaded.identified
    assert relation.partner(payment.id) is None

    # The owner pairs it by hand with the deposit: an ordinary matched transfer.
    transfer = await run(pair_records, payment.id, deposit.id)
    assert (transfer.status, transfer.from_account_id, transfer.to_account_id) == (
        "matched",
        monzo.account.id,
        invest.account.id,
    )
    assert await contra_account(session, payment.id, monzo.account.id) == ids.in_transit
    assert await in_transit_balance(session, ids) == Decimal(0)
    await heal()
    [still] = await list_transfers(session)
    assert still.status == "matched"
    await assert_consistent(session)


async def test_registering_rematches_and_stays_unassigned(
    session: AsyncSession,
    run: Run,
    platform,
    ids: SystemIds,
    connector_tx: ConnectorTx,
    heal: Heal,
) -> None:
    monzo, invest, isa = platform
    record = await run(register_card_merchant, MERCHANT, [invest.account.id])
    payment = await _card_payment(connector_tx, monzo, record.id)
    # Outside the near window, so Task 16's card tier doesn't pair them.
    await _deposit(connector_tx, invest, bank_transfer=True, occurred_at=WHEN + timedelta(hours=1))
    await heal()

    # One holder or two, a card merchant never identifies the record (no `holder`).
    await run(register_card_merchant_and_rematch, MERCHANT, [invest.account.id, isa.account.id])
    row = await _eligible_row(session, payment.id)
    assert (row.holder, set(row.holders)) == (None, {invest.account.id, isa.account.id})
    assert await contra_account(session, payment.id, monzo.account.id) == ids.unassigned
    assert await list_transfers(session) == []
    await assert_consistent(session)


def _pairable(account: UUID, quantity: str, **kw) -> Pairable:
    defaults = {
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


def test_no_existing_tier_pairs_a_multi_holder_record() -> None:
    """None of the earlier tiers pairs a card payment; Task 16's card_tier is the one that
    does, and only with a connector's record naming nobody (test_card_deposits)."""
    monzo, invest, isa = UUID(int=10), UUID(int=11), UUID(int=12)
    for bank_transfer in (False, True):
        card = _pairable(
            monzo,
            "-50",
            holders=frozenset({invest, isa}),
            bank_transfer=bank_transfer,
            names_account=True,
        )
        deposits = [
            _pairable(invest, "50"),  # naming nobody
            _pairable(invest, "50", bank_transfer=True),  # a bank transfer naming nobody
            _pairable(invest, "50", bank_transfer=True, holder=monzo),  # naming Monzo
            _pairable(invest, "50", bank_transfer=True, weak_holder=monzo),  # Monzo's sort code
        ]
        for deposit in deposits:
            for r, c in ((card, deposit), (deposit, card)):
                assert registry_tier(r, c) is None
                assert weak_tier(r, c) is None
                assert cross_currency_tier(r, c) is None
        # A card payment whose holders' record names Monzo in another currency neither.
        across = _pairable(invest, "60", holder=monzo, instrument_id=UUID(int=2))
        assert cross_currency_tier(card, across) is None
        assert cross_currency_tier(across, card) is None
    # The qualification: card_tier pairs a connector's card payment with a connector's
    # deposit naming nobody, a bank transfer or not, and with none of the others above.
    connector = ("connection", "c:card")
    card = _pairable(monzo, "-50", holders=frozenset({invest, isa}), source=connector)
    for bank_transfer in (False, True):
        plain = _pairable(invest, "50", bank_transfer=bank_transfer, source=("connection", "c:d"))
        assert card_tier(card, plain, NEAR) == card_tier(plain, card, NEAR) == ONE_SIDED
    for named in (
        _pairable(invest, "50", bank_transfer=True, holder=monzo, names_account=True),
        _pairable(invest, "50", bank_transfer=True, weak_holder=monzo, names_account=True),
    ):
        assert card_tier(card, named, NEAR) is None


async def test_no_existing_tier_pairs_it_in_the_ledger(
    session: AsyncSession,
    run: Run,
    platform,
    ids: SystemIds,
    connector_tx: ConnectorTx,
    heal: Heal,
) -> None:
    monzo, invest, isa = platform
    monzo_identifier = await run(
        assign_identifier, monzo.account.id, "uk_bank_account", MONZO_IDENTIFIER
    )
    record = await run(register_card_merchant, MERCHANT, [invest.account.id, isa.account.id])
    payment = await _card_payment(connector_tx, monzo, record.id)
    # A bank transfer into Invest naming Monzo, and one into ISA naming nobody, same amount
    # and day: ONE_SIDED would take either for a record identified as theirs. ISA's is an
    # hour later, outside the near window, so Task 16's card tier doesn't take it either.
    await _deposit(connector_tx, invest, bank_transfer=True, identifier_id=monzo_identifier.id)
    await _deposit(
        connector_tx,
        isa,
        external_id="deposit_2",
        bank_transfer=True,
        occurred_at=WHEN + timedelta(hours=1),
    )
    await heal()

    assert await contra_account(session, payment.id, monzo.account.id) == ids.unassigned
    # The deposit naming Monzo is identified, so it waits on its own: never with the card.
    transfers = await list_transfers(session)
    assert all(payment.id not in (t.out_transaction_id, t.in_transaction_id) for t in transfers)
    await assert_consistent(session)


@pytest.mark.parametrize("paired", [True, False])
async def test_sync_closing_a_holder_then_heal_is_consistent(
    session: AsyncSession,
    run: Run,
    platform,
    connector_tx: ConnectorTx,
    heal: Heal,
    paired: bool,
) -> None:
    """A connector's sync closes a holder (Monzo a deleted pot, IBKR a closed account): the
    account stays the merchant's holder, so the heal the worker runs after the sync has
    nothing to undo, whether the card tier or the owner made the pair."""
    monzo, invest, isa = platform
    record = await run(register_card_merchant, MERCHANT, [invest.account.id, isa.account.id])
    payment = await _card_payment(connector_tx, monzo, record.id)
    deposit = await _deposit(connector_tx, isa)
    if paired:
        await run(pair_records, payment.id, deposit.id)
    await heal()
    before = await list_transfers(session)

    async with command_context(session, Actor("connector", "monzo"), "connection.sync", {}) as ctx:
        await close_account(ctx, isa.account.id, DAY)
    assert await heal() == 0

    assert await holders_of(session, record.id) == {invest.account.id, isa.account.id}
    relation = await pair_relation(
        session, await system_account_ids(session), await load_transfer_settings(session)
    )
    assert relation.records[payment.id].holders == frozenset({invest.account.id, isa.account.id})
    [transfer] = await list_transfers(session)
    assert transfer == before[0]
    assert (transfer.status, transfer.to_account_id) == ("matched", isa.account.id)
    await assert_consistent(session)
