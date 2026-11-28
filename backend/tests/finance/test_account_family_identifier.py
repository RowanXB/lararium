"""The account_family identifier: its value, its refusals, and whom a record naming it names."""

import uuid
from datetime import date

import pytest

from lararium.modules.finance.account_types import families
from lararium.modules.finance.accounts import close_account, set_type_key
from lararium.modules.finance.errors import FinanceValidationError
from lararium.modules.finance.identifiers import (
    assign_identifier,
    dismiss_identifier,
    ensure_identifier,
    family_identifier,
    identifier_account,
    named_accounts,
    normalize_identifier,
)
from lararium.modules.finance.system_accounts import get_or_create_system_account
from lararium.modules.finance.transfers import transfer_contra

FAMILY_TYPES = ["trading212.invest", "trading212.stocks_isa", "trading212.cash_isa"]


def test_normalize_accepts_only_a_catalog_family() -> None:
    assert normalize_identifier("account_family", " trading212 ") == "trading212"
    for bad in ("monzo", ""):
        with pytest.raises(FinanceValidationError, match="an account family is one of: "):
            normalize_identifier("account_family", bad)


def test_every_catalog_family_is_an_identifier_value() -> None:
    assert families()
    for family in families():
        assert normalize_identifier("account_family", family) == family


async def test_a_family_identifier_takes_no_account_and_no_dismissal(run, make_account) -> None:
    account = await make_account(balance_mode="hybrid")
    identifier_id = await run(family_identifier, "trading212")
    assert await run(family_identifier, "trading212") == identifier_id
    with pytest.raises(FinanceValidationError):
        await run(assign_identifier, account.id, "account_family", "trading212")
    with pytest.raises(FinanceValidationError):
        await run(ensure_identifier, account.id, "account_family", "trading212")
    with pytest.raises(FinanceValidationError):
        await run(dismiss_identifier, identifier_id)


@pytest.mark.parametrize("count", [1, 2, 3])
async def test_named_accounts_of_a_family(run, session, make_account, count: int) -> None:
    accounts = [
        await make_account(name=f"Family {n}", balance_mode="hybrid", type_key=key)
        for n, key in enumerate(FAMILY_TYPES[:count])
    ]
    if count == 3:
        await run(close_account, accounts[2].id, date(2026, 11, 1))
    await make_account(name="Elsewhere", balance_mode="hybrid", type_key="hsbc_uk")
    identifier_id = await run(family_identifier, "trading212")
    first, *rest = (a.id for a in accounts)

    named = await named_accounts(session, identifier_id, first)

    assert (
        named
        == {
            1: (None, frozenset()),
            2: (rest[0] if rest else None, frozenset()),
            3: (None, frozenset(rest)),
        }[count]
    )


async def test_a_cleared_type_still_excludes_the_accounts_own_id(
    run, session, make_account
) -> None:
    first = await make_account(name="One", balance_mode="hybrid", type_key=FAMILY_TYPES[0])
    second = await make_account(name="Two", balance_mode="hybrid", type_key=FAMILY_TYPES[1])
    identifier_id = await run(family_identifier, "trading212")
    await run(set_type_key, first.id, None)

    assert await named_accounts(session, identifier_id, first.id) == (second.id, frozenset())
    assert await named_accounts(session, identifier_id, second.id) == (None, frozenset())


async def test_named_accounts_of_an_assigned_identifier_is_its_account(
    run, session, make_account
) -> None:
    account = await make_account(balance_mode="hybrid")
    record = await run(assign_identifier, account.id, "uk_bank_account", "112233:12345678")
    assert await named_accounts(session, record.id, uuid.uuid4()) == (account.id, frozenset())
    assert await identifier_account(session, record.id) == account.id
    assert await named_accounts(session, uuid.uuid4(), account.id) == (None, frozenset())


@pytest.mark.parametrize("count", [0, 1, 2])
async def test_transfer_contra_of_a_family_move(run, session, make_account, count: int) -> None:
    unassigned = await run(get_or_create_system_account, "unassigned")
    in_transit = await run(get_or_create_system_account, "in_transit")
    own = await make_account(name="Own", balance_mode="hybrid", type_key=FAMILY_TYPES[0])
    for n in range(count):
        await make_account(name=f"Other {n}", balance_mode="hybrid", type_key=FAMILY_TYPES[n + 1])
    identifier_id = await run(family_identifier, "trading212")

    contra = await transfer_contra(
        session,
        source=None,
        own_account_id=own.id,
        counterparty_identifier_id=identifier_id,
        other_postings=0,
    )

    assert contra == (in_transit if count == 1 else unassigned)
