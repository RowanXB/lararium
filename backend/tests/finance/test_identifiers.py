import asyncio
import uuid

import pytest
from sqlalchemy import exc, text

from lararium.core.actors import Actor
from lararium.core.commands import command_context
from lararium.db import create_engine, make_sessionmaker
from lararium.modules.finance.accounts import get_account, set_balance_mode
from lararium.modules.finance.errors import (
    AccountIdentifierNotFoundError,
    DuplicateAccountIdentifierError,
    FinanceValidationError,
)
from lararium.modules.finance.identifiers import (
    assign_identifier,
    dismiss_identifier,
    ensure_identifier,
    get_or_create_identifier,
    identifier_account,
    list_identifiers,
    normalize_identifier,
    unassign_identifier,
)

VALUE = "112233:12345678"


async def _count_events(session, event_type: str) -> int:
    return await session.scalar(
        text("select count(*) from core.events where type = :t"), {"t": event_type}
    )


@pytest.mark.parametrize("spelling", ["11-22-33 12345678", "112233/12345678", "112233:12345678"])
def test_normalize_accepts_common_spellings(spelling) -> None:
    assert normalize_identifier("uk_bank_account", spelling) == VALUE


@pytest.mark.parametrize(
    "bad", ["1122 1234", "", "11-22-33", "ab2233:12345678", "112233:123456789"]
)
def test_normalize_rejects_anything_else(bad) -> None:
    with pytest.raises(FinanceValidationError, match="6-digit sort code and an 8-digit"):
        normalize_identifier("uk_bank_account", bad)


async def test_get_or_create_then_assign_unassign(session, run, make_account) -> None:
    account = await make_account(balance_mode="hybrid")
    first = await run(get_or_create_identifier, "uk_bank_account", "11-22-33 12345678")
    assert await run(get_or_create_identifier, "uk_bank_account", "112233/12345678") == first
    [row] = await list_identifiers(session)
    assert (row.id, row.value, row.account_id, row.dismissed_at, row.created_by) == (
        first,
        VALUE,
        None,
        None,
        "user",
    )
    assert await identifier_account(session, first) is None

    assigned = await run(assign_identifier, account.id, "uk_bank_account", VALUE)
    assert (assigned.id, assigned.account_id) == (first, account.id)
    assert await identifier_account(session, first) == account.id
    assert await list_identifiers(session, account_id=account.id) == [assigned]
    assert await _count_events(session, "finance.account_identifier.assigned") == 1
    # Assigning it to the same account again changes nothing.
    assert await run(assign_identifier, account.id, "uk_bank_account", VALUE) == assigned
    assert await _count_events(session, "finance.account_identifier.assigned") == 1

    unassigned = await run(unassign_identifier, first)
    assert (unassigned.id, unassigned.account_id) == (first, None)
    assert await identifier_account(session, first) is None
    assert [r.id for r in await list_identifiers(session)] == [first]
    assert await list_identifiers(session, account_id=account.id) == []
    assert await _count_events(session, "finance.account_identifier.unassigned") == 1


async def test_unknown_identifier_not_found(session, run) -> None:
    missing = uuid.uuid4()
    assert await identifier_account(session, missing) is None
    with pytest.raises(AccountIdentifierNotFoundError):
        await run(unassign_identifier, missing)
    with pytest.raises(AccountIdentifierNotFoundError):
        await run(dismiss_identifier, missing)


async def test_assigned_elsewhere_rejected(session, run, make_account) -> None:
    first = await make_account(name="Test Current", balance_mode="hybrid")
    second = await make_account(name="Test Savings", balance_mode="hybrid")
    await run(assign_identifier, first.id, "uk_bank_account", VALUE)
    with pytest.raises(DuplicateAccountIdentifierError, match="Test Current"):
        await run(assign_identifier, second.id, "uk_bank_account", "11-22-33 12345678")
    [row] = await list_identifiers(session)
    assert row.account_id == first.id


async def test_dismiss_only_unassigned_and_assign_clears_it(session, run, make_account) -> None:
    account = await make_account(balance_mode="hybrid")
    identifier_id = await run(get_or_create_identifier, "uk_bank_account", VALUE)
    dismissed = await run(dismiss_identifier, identifier_id)
    assert dismissed.dismissed_at is not None
    assert await _count_events(session, "finance.account_identifier.dismissed") == 1
    # Dismissing again changes nothing.
    assert await run(dismiss_identifier, identifier_id) == dismissed
    assert await _count_events(session, "finance.account_identifier.dismissed") == 1

    assigned = await run(assign_identifier, account.id, "uk_bank_account", VALUE)
    assert (assigned.account_id, assigned.dismissed_at) == (account.id, None)
    with pytest.raises(FinanceValidationError, match="not assigned"):
        await run(dismiss_identifier, identifier_id)
    assert (await list_identifiers(session))[0].account_id == account.id


async def test_snapshots_account_refuses_identifier(session, run, make_account) -> None:
    account = await make_account(balance_mode="snapshots")
    with pytest.raises(FinanceValidationError, match="switch the account to hybrid"):
        await run(assign_identifier, account.id, "uk_bank_account", VALUE)
    switched = await run(set_balance_mode, account.id, "hybrid")
    assert switched.balance_mode == "hybrid"
    assert (await get_account(session, account.id)).balance_mode == "hybrid"
    assigned = await run(assign_identifier, account.id, "uk_bank_account", VALUE)
    assert assigned.account_id == account.id


@pytest.mark.parametrize("ledger_type", ["income", "expense", "equity"])
async def test_only_assets_and_liabilities_carry_identifiers(
    run, make_account, ledger_type
) -> None:
    account = await make_account(ledger_type=ledger_type, subtype="other", balance_mode="hybrid")
    with pytest.raises(FinanceValidationError):
        await run(assign_identifier, account.id, "uk_bank_account", VALUE)
    with pytest.raises(FinanceValidationError):
        await run(ensure_identifier, account.id, "uk_bank_account", VALUE)


async def test_liability_accepts_identifier(run, make_account) -> None:
    card = await make_account(ledger_type="liability", subtype="other", balance_mode="hybrid")
    assigned = await run(assign_identifier, card.id, "uk_bank_account", VALUE)
    assert assigned.account_id == card.id


async def test_balance_mode_switch_only_snapshots_to_hybrid(session, run, make_account) -> None:
    hybrid = await make_account(balance_mode="hybrid")
    transactions = await make_account(balance_mode="transactions")
    snapshots = await make_account(balance_mode="snapshots")
    for account, mode in [
        (hybrid, "snapshots"),
        (hybrid, "transactions"),
        (transactions, "hybrid"),
        (transactions, "snapshots"),
        (snapshots, "transactions"),
    ]:
        with pytest.raises(FinanceValidationError):
            await run(set_balance_mode, account.id, mode)
        assert (await get_account(session, account.id)).balance_mode == account.balance_mode
    assert await _count_events(session, "finance.account.updated") == 0

    # The current mode is a no-op, with no event.
    assert (await run(set_balance_mode, hybrid.id, "hybrid")).balance_mode == "hybrid"
    assert await _count_events(session, "finance.account.updated") == 0

    await run(set_balance_mode, snapshots.id, "hybrid")
    assert await _count_events(session, "finance.account.updated") == 1


async def test_ensure_identifier_idempotent_and_never_steals(session, run, make_account) -> None:
    owner = await make_account(name="Test Current", balance_mode="transactions")
    other = await make_account(name="Test Other", balance_mode="transactions")
    assert await run(ensure_identifier, owner.id, "uk_bank_account", "11-22-33 12345678") is True
    assert await run(ensure_identifier, owner.id, "uk_bank_account", VALUE) is False
    events = await _count_events(session, "finance.account_identifier.assigned")
    assert events == 1

    assert await run(ensure_identifier, other.id, "uk_bank_account", VALUE) is False
    [row] = await list_identifiers(session)
    assert row.account_id == owner.id
    assert await list_identifiers(session, account_id=other.id) == []
    assert await _count_events(session, "finance.account_identifier.assigned") == events


async def test_ensure_identifier_assigns_a_counterparty_row(session, run, make_account) -> None:
    account = await make_account(balance_mode="transactions")
    identifier_id = await run(get_or_create_identifier, "uk_bank_account", VALUE)
    assert await run(ensure_identifier, account.id, "uk_bank_account", VALUE) is True
    assert await identifier_account(session, identifier_id) == account.id


async def test_ensure_identifier_claims_a_dismissed_row(session, run, make_account) -> None:
    # A connector's own account is a fact, so it outweighs a dismissal.
    account = await make_account(balance_mode="transactions")
    identifier_id = await run(get_or_create_identifier, "uk_bank_account", VALUE)
    await run(dismiss_identifier, identifier_id)
    assert await run(ensure_identifier, account.id, "uk_bank_account", VALUE) is True
    [row] = await list_identifiers(session)
    assert (row.id, row.account_id, row.dismissed_at) == (identifier_id, account.id, None)


async def test_ensure_identifier_refuses_snapshots_account(run, make_account) -> None:
    account = await make_account(balance_mode="snapshots")
    with pytest.raises(FinanceValidationError, match="switch the account to hybrid"):
        await run(ensure_identifier, account.id, "uk_bank_account", VALUE)


async def test_concurrent_get_or_create_one_row(
    backend_url: str, owner_url: str, migrated: None
) -> None:
    # Two syncs naming the same bank account before either commits must share one row.
    value = "998877:87654321"
    engine, owner = create_engine(backend_url), create_engine(owner_url)
    sessions = make_sessionmaker(engine)
    actor = Actor("connector", "test")

    async def ask() -> uuid.UUID:
        async with sessions() as s, command_context(s, actor, "test.shared_identifier", {}) as ctx:
            identifier_id = await get_or_create_identifier(ctx, "uk_bank_account", value)
            # Still uncommitted while the other asks, as in a sync's transaction.
            await asyncio.sleep(0.5)
            return identifier_id

    try:
        first, second = await asyncio.gather(ask(), ask())
        assert first == second
        async with owner.connect() as conn:
            rows = await conn.scalar(
                text("select count(*) from finance.account_identifiers where value = :v"),
                {"v": value},
            )
        assert rows == 1
    finally:
        async with owner.begin() as conn:
            await conn.execute(
                text("delete from finance.account_identifiers where value = :v"), {"v": value}
            )
            await conn.execute(
                text("delete from core.audit_log where command = 'test.shared_identifier'")
            )
        await engine.dispose()
        await owner.dispose()


async def test_value_check_enforced_by_database(session) -> None:
    with pytest.raises(exc.IntegrityError, match="check constraint"):
        await session.execute(
            text(
                "insert into finance.account_identifiers (kind, value, created_by)"
                " values ('uk_bank_account', '11-22-33 12345678', 'user')"
            )
        )


async def test_dismissed_row_cannot_be_assigned_by_database(session, make_account) -> None:
    account = await make_account(balance_mode="hybrid")
    with pytest.raises(exc.IntegrityError, match="check constraint"):
        await session.execute(
            text(
                "insert into finance.account_identifiers"
                " (kind, value, account_id, dismissed_at, created_by)"
                " values ('uk_bank_account', :v, :a, now(), 'user')"
            ),
            {"v": VALUE, "a": account.id},
        )


@pytest.mark.parametrize("spelling", ["U1234567", " u1234567 ", "DU123456"])
def test_normalize_ibkr_account_upper_cases(spelling) -> None:
    assert normalize_identifier("ibkr_account", spelling) == spelling.strip().upper()


@pytest.mark.parametrize(
    "bad", ["", "1234567", "U12", "ABCD1234567", "U123456789012345", "U12 345"]
)
def test_normalize_ibkr_account_rejects_anything_else(bad) -> None:
    with pytest.raises(FinanceValidationError, match="IBKR account id"):
        normalize_identifier("ibkr_account", bad)
