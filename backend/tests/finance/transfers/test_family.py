"""A record naming an account family (account family identifier design §5.2): its row in
eligible_records resolves to the family's other accounts, as identifiers.named_accounts does.
It pairs only within the family (§5.3): with a record naming the family back, or with one
naming nobody when both are timed within the near window; and such a record naming nobody
is that move's, not a shared payee's or its account's funding's."""

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
from lararium.modules.finance.accounts import AccountRecord, create_account, set_type_key
from lararium.modules.finance.identifiers import family_identifier, named_accounts
from lararium.modules.finance.system_accounts import system_account_ids
from lararium.modules.finance.transfers.contra import eligible_records
from lararium.modules.finance.transfers.pairs import (
    MUTUAL,
    ONE_SIDED,
    Known,
    Pairable,
    _Decisions,
    _registry_edges,
    card_tier,
    registry_tier,
)
from lararium.modules.finance.transfers.settings import DEFAULTS

from .conftest import WHEN, Connected, ConnectorTx, Run

FAMILY_TYPES = ["trading212.invest", "trading212.stocks_isa", "trading212.cash_isa"]

type MakeFamily = Callable[[int], Awaitable[tuple[Connected, list[AccountRecord]]]]


@pytest.fixture
def family_accounts(run: Run) -> MakeFamily:
    """A connected Trading 212 Invest account and n - 1 manual accounts of the family's
    other types."""

    async def _make(n: int) -> tuple[Connected, list[AccountRecord]]:
        connection = await run(
            create_connection, connector="trading212", display_name="T212", config={}
        )
        first = await run(
            create_account,
            name="Invest",
            ledger_type="asset",
            subtype="broker",
            balance_mode="transactions",
            source_connection_id=connection.id,
            type_key=FAMILY_TYPES[0],
        )
        others = [
            await run(
                create_account,
                name=f"Other {key}",
                ledger_type="asset",
                subtype="broker",
                balance_mode="transactions",
                type_key=key,
            )
            for key in FAMILY_TYPES[1:n]
        ]
        return Connected(connection.id, first), others

    return _make


async def _eligible_row(session: AsyncSession, transaction_id: UUID) -> Any:
    e = eligible_records(await system_account_ids(session))
    return (await session.execute(select(e).where(e.c.transaction_id == transaction_id))).one()


@pytest.mark.parametrize("n", [1, 2, 3])
async def test_eligible_row_of_a_family_move(
    session: AsyncSession,
    run: Run,
    family_accounts: MakeFamily,
    connector_tx: ConnectorTx,
    n: int,
) -> None:
    first, others = await family_accounts(n)
    identifier_id = await run(family_identifier, "trading212")
    sent = await connector_tx(first, "-100", identifier_id=identifier_id)

    row = await _eligible_row(session, sent.id)

    holder, holders = await named_accounts(session, identifier_id, first.account.id)
    assert holders == {o.id for o in others} - {holder}
    assert row.family and row.names_account
    assert row.holder == holder
    assert (row.payee_holders is None and not holders) or set(row.payee_holders) == holders


async def test_a_retyped_account_still_excludes_itself(
    session: AsyncSession,
    run: Run,
    family_accounts: MakeFamily,
    connector_tx: ConnectorTx,
) -> None:
    first, others = await family_accounts(3)
    identifier_id = await run(family_identifier, "trading212")
    sent = await connector_tx(first, "-100", identifier_id=identifier_id)
    await run(set_type_key, first.account.id, None)

    row = await _eligible_row(session, sent.id)

    # Retyped, the account is no longer of the family: the two others are the set.
    assert row.holder is None
    assert set(row.payee_holders) == {o.id for o in others}
    assert first.account.id not in row.payee_holders


# The tier on its own (account family identifier design §5.3).

# Invest, Stocks ISA and Cash ISA, of one family; Monzo, of none.
INVEST, STOCKS, CASH, MONZO = UUID(int=20), UUID(int=21), UUID(int=22), UUID(int=23)
T0 = WHEN
NEAR = DEFAULTS.near_window


def _pairable(account: UUID, quantity: str, **kw: Any) -> Pairable:
    defaults: dict[str, Any] = {
        "id": uuid4(),
        "source": ("connection", f"{account}:{quantity}:{uuid4()}"),
        "instrument_id": UUID(int=1),
        "occurred_at": T0,
        "booking_date": T0.date(),
        "holder": None,
        "bank_transfer": False,
        "timed": True,
    }
    return Pairable(account_id=account, quantity=Decimal(quantity), **(defaults | kw))


def _move(account: UUID, quantity: str, family: set[UUID], **kw: Any) -> Pairable:
    """A record naming the family `family`, on one of its accounts (§5.2): identified to
    the one other account, or to the set of two or more."""
    others = family - {account}
    named: dict[str, Any] = (
        {"holder": next(iter(others))} if len(others) == 1 else {"payee_holders": frozenset(others)}
    )
    return _pairable(account, quantity, family=True, names_account=True, **(named | kw))


THREE = {INVEST, STOCKS, CASH}


def test_two_family_moves_pair_within_the_family() -> None:
    sent = _move(INVEST, "-100", THREE)
    received = _move(STOCKS, "100", THREE)
    assert registry_tier(sent, received) == registry_tier(received, sent) == ONE_SIDED
    # A family of two: each is identified to the other's account.
    pair = _move(INVEST, "-100", {INVEST, STOCKS}), _move(STOCKS, "100", {INVEST, STOCKS})
    assert registry_tier(*pair) == registry_tier(*reversed(pair)) == MUTUAL
    # A family move on an account outside the set.
    elsewhere = _move(MONZO, "100", {MONZO, STOCKS, CASH})
    assert registry_tier(sent, elsewhere) is None and registry_tier(elsewhere, sent) is None
    # However it is marked, a family move is no record naming nobody.
    for marked in (
        replace(received, bank_transfer=True),
        replace(received, funded_by_transfer=True),
        # Untimed, or far apart: time decides nothing between two family moves.
        replace(received, timed=False),
        replace(received, occurred_at=T0 + timedelta(days=1)),
    ):
        assert registry_tier(sent, marked) == registry_tier(marked, sent) == ONE_SIDED, marked


def test_a_family_move_takes_a_record_naming_nobody_only_within_the_near_window() -> None:
    sent = _move(INVEST, "-100", THREE)
    for pairs, other in (
        (True, _pairable(CASH, "100", bank_transfer=True, occurred_at=T0 + NEAR)),
        (
            False,
            _pairable(
                CASH, "100", bank_transfer=True, occurred_at=T0 + NEAR + timedelta(seconds=1)
            ),
        ),
        (False, _pairable(CASH, "100", bank_transfer=True, timed=False)),
        (
            True,
            _pairable(
                STOCKS, "100", funded_by_transfer=True, occurred_at=T0 + timedelta(seconds=30)
            ),
        ),
        (True, _pairable(STOCKS, "100", occurred_at=T0 - NEAR, source=("import", "k"))),
        # It names someone: an account, a bank account, a card merchant.
        (False, _pairable(CASH, "100", bank_transfer=True, holder=MONZO, names_account=True)),
        (False, _pairable(CASH, "100", bank_transfer=True, names_account=True)),
        (False, _pairable(CASH, "100", holders=frozenset({MONZO}))),
        (False, _pairable(MONZO, "100", bank_transfer=True)),  # outside the family
    ):
        expected = ONE_SIDED if pairs else None
        assert registry_tier(sent, other) == registry_tier(other, sent) == expected, other
    # The family move must be timed too.
    untimed = replace(sent, timed=False)
    assert registry_tier(untimed, _pairable(CASH, "100", bank_transfer=True)) is None
    # With one other account, the same: a record there naming nobody, near it.
    two = _move(INVEST, "-100", {INVEST, CASH})
    assert registry_tier(two, _pairable(CASH, "100", bank_transfer=True)) == ONE_SIDED
    far = _pairable(CASH, "100", bank_transfer=True, occurred_at=T0 + timedelta(hours=1))
    assert registry_tier(two, far) is None and registry_tier(far, two) is None


def test_a_family_move_is_never_a_payee_or_card_record_s() -> None:
    payee = _pairable(
        MONZO, "-100", bank_transfer=True, names_account=True, payee_holders=frozenset(THREE)
    )
    for received in (
        _move(STOCKS, "100", THREE),
        _move(STOCKS, "100", THREE, bank_transfer=True),
        _move(STOCKS, "100", THREE, funded_by_transfer=True),
    ):
        assert registry_tier(payee, received) is None, received
        assert registry_tier(received, payee) is None, received
    card = _pairable(MONZO, "-100", holders=frozenset({STOCKS}))
    received = _move(STOCKS, "100", THREE)
    assert card_tier(card, received, NEAR) is None and card_tier(received, card, NEAR) is None


_NO_DECISIONS = _Decisions(frozenset(), frozenset(), ())
_ALL_KNOWN = Known(frozenset(), {})


def test_family_evidence_comes_first() -> None:
    """The real shape: a Cash ISA row of an internal move, a bank transfer naming nobody,
    within the near window of its TRANSFER, and a same-amount payment from Monzo to the
    shared payee the same day. The row is the TRANSFER's alone; the payment keeps its own
    deposit, two minutes or more from any family move."""
    sent = _move(INVEST, "-100", THREE)
    row = _pairable(CASH, "100", bank_transfer=True, occurred_at=T0 + timedelta(seconds=18))
    payment = _pairable(
        MONZO,
        "-100",
        bank_transfer=True,
        names_account=True,
        payee_holders=frozenset(THREE),
        occurred_at=T0 - timedelta(hours=3),
    )
    deposit = _pairable(
        STOCKS, "100", funded_by_transfer=True, occurred_at=T0 - timedelta(hours=3, seconds=-5)
    )
    same, _, _ = _registry_edges([sent, row, payment, deposit], DEFAULTS, _NO_DECISIONS, _ALL_KNOWN)
    assert same.tiers[row.id] == {sent.id: ONE_SIDED}
    assert same.tiers[payment.id] == {deposit.id: ONE_SIDED}
    # Far from the family move, the row is the payment's candidate again.
    far = replace(row, occurred_at=T0 + NEAR + timedelta(seconds=1))
    same, _, _ = _registry_edges([sent, far, payment], DEFAULTS, _NO_DECISIONS, _ALL_KNOWN)
    assert same.tiers[far.id] == {payment.id: ONE_SIDED}
    # A row it takes through its funding alone gives way too, and only to a family move:
    # an identified record naming its account keeps it.
    funded = replace(row, bank_transfer=False, funded_by_transfer=True)
    named = _pairable(MONZO, "-100", bank_transfer=True, holder=CASH, names_account=True)
    same, _, _ = _registry_edges([sent, funded, named], DEFAULTS, _NO_DECISIONS, _ALL_KNOWN)
    assert same.tiers[funded.id] == {sent.id: ONE_SIDED}
    same, _, _ = _registry_edges([sent, row, named], DEFAULTS, _NO_DECISIONS, _ALL_KNOWN)
    assert same.tiers[row.id] == {sent.id: ONE_SIDED, named.id: ONE_SIDED}
