import uuid
from datetime import date

import pytest
from sqlalchemy import text

from lararium.core.entities import get_or_create_organization, get_self_entity_id
from lararium.modules.finance.accounts import (
    close_account,
    create_account,
    get_account,
    list_accounts,
    rename_account,
)
from lararium.modules.finance.errors import AccountAlreadyClosedError, AccountNotFoundError


async def test_create_manual_bank_account(session, run) -> None:
    manual_bank = await run(get_or_create_organization, "Example Bank")
    acc = await run(
        create_account,
        name="Current account",
        ledger_type="asset",
        subtype="bank",
        balance_mode="hybrid",
        institution_entity_id=manual_bank,
    )
    assert (acc.source_kind, acc.source_connection_id, acc.status, acc.tax_wrapper) == (
        "manual",
        None,
        "open",
        "none",
    )
    assert acc.owner_entity_id == await get_self_entity_id(session)


async def test_lisa_wrapper_is_kept(make_account) -> None:
    acc = await make_account(name="Cash LISA", tax_wrapper="lisa")
    assert acc.tax_wrapper == "lisa"


async def test_pot_is_child_of_parent(make_account) -> None:
    parent = await make_account(name="Monzo")
    pot = await make_account(name="Savings pot", subtype="savings_pot", parent_account_id=parent.id)
    assert pot.parent_account_id == parent.id


async def test_unknown_parent_rejected(make_account) -> None:
    with pytest.raises(AccountNotFoundError):
        await make_account(parent_account_id=uuid.uuid4())


async def test_close_account(run, make_account) -> None:
    acc = await make_account()
    closed = await run(close_account, acc.id, date(2026, 6, 30))
    assert (closed.status, closed.closed_on) == ("closed", date(2026, 6, 30))
    with pytest.raises(AccountAlreadyClosedError):
        await run(close_account, acc.id, date(2026, 7, 1))


async def test_create_account_emits_event(session, make_account) -> None:
    acc = await make_account()
    data = await session.scalar(
        text("select data from core.events where type = 'finance.account.created'")
    )
    assert data == {"account_id": str(acc.id)}


async def test_rename_account_emits_updated(session, run, make_account) -> None:
    acc = await make_account(name="Old name")
    renamed = await run(rename_account, acc.id, "New name")
    assert renamed.name == "New name"
    assert (await get_account(session, acc.id)).name == "New name"
    data = await session.scalar(
        text("select data from core.events where type = 'finance.account.updated'")
    )
    assert data == {"account_id": str(acc.id)}


async def test_list_accounts_sorted_by_name(session, make_account) -> None:
    await make_account(name="Zeta")
    await make_account(name="Alpha")
    assert [a.name for a in await list_accounts(session)] == ["Alpha", "Zeta"]


async def test_missing_account_not_found(session, run) -> None:
    with pytest.raises(AccountNotFoundError):
        await get_account(session, uuid.uuid4())
    with pytest.raises(AccountNotFoundError):
        await run(rename_account, uuid.uuid4(), "x")
    with pytest.raises(AccountNotFoundError):
        await run(close_account, uuid.uuid4(), date(2026, 6, 30))
