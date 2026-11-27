import asyncio
import uuid
from datetime import date

import pytest
from sqlalchemy import exc, func, insert, select, text

from lararium.core.actors import Actor
from lararium.core.commands import command_context
from lararium.db import create_engine, make_sessionmaker
from lararium.modules.finance import models
from lararium.modules.finance.accounts import (
    AccountRecord,
    close_account,
    create_account,
    get_account,
    set_balance_mode,
)
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
    register_card_merchant,
    register_holders,
    unassign_identifier,
)
from lararium.modules.finance.system_accounts import get_or_create_system_account

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


# A payee bank account held by several own accounts (account type catalog design §8.1)

PAYEE = "11-22-33 87654321"
PAYEE_VALUE = "112233:87654321"


async def _payee_accounts(make_account) -> tuple[AccountRecord, AccountRecord]:
    return (
        await make_account(name="Test Invest", balance_mode="hybrid"),
        await make_account(name="Test ISA", balance_mode="hybrid"),
    )


async def _holder_rows(session, identifier_id: uuid.UUID) -> set[tuple[str, uuid.UUID]]:
    h = models.AccountIdentifierHolder
    rows = await session.execute(
        select(h.kind, h.account_id).where(h.identifier_id == identifier_id)
    )
    return {(row.kind, row.account_id) for row in rows}


async def test_register_holders_of_a_bank_account_replaces_the_set(
    session, run, make_account
) -> None:
    invest, isa = await _payee_accounts(make_account)

    record = await run(register_holders, "uk_bank_account", PAYEE, [isa.id, invest.id, isa.id])

    assert (record.kind, record.value, record.account_id) == ("uk_bank_account", PAYEE_VALUE, None)
    assert record.holders == tuple(sorted([invest.id, isa.id]))
    assert await _holder_rows(session, record.id) == {
        ("uk_bank_account", invest.id),
        ("uk_bank_account", isa.id),
    }
    [listed] = await list_identifiers(session)
    assert listed == record
    # Each holder lists it among its identifiers.
    assert await list_identifiers(session, account_id=isa.id) == [record]
    assert await identifier_account(session, record.id) is None
    assert await _count_events(session, "finance.account_identifier.holders_changed") == 1

    # Registering again replaces the set; the same set again changes nothing.
    again = await run(register_holders, "uk_bank_account", PAYEE_VALUE, [invest.id])
    assert (again.id, again.holders) == (record.id, (invest.id,))
    assert await run(register_holders, "uk_bank_account", PAYEE_VALUE, [invest.id]) == again
    assert await _count_events(session, "finance.account_identifier.holders_changed") == 2

    # An empty list removes them all: the row stays, registered to no account.
    removed = await run(register_holders, "uk_bank_account", PAYEE_VALUE, [])
    assert (removed.id, removed.account_id, removed.holders) == (record.id, None, ())
    assert await _holder_rows(session, record.id) == set()


async def test_register_holders_takes_it_back_from_dismissed(session, run, make_account) -> None:
    invest, _ = await _payee_accounts(make_account)
    identifier_id = await run(get_or_create_identifier, "uk_bank_account", PAYEE)
    await run(dismiss_identifier, identifier_id)

    record = await run(register_holders, "uk_bank_account", PAYEE, [invest.id])

    assert (record.id, record.dismissed_at) == (identifier_id, None)


@pytest.mark.parametrize(
    ("case", "message"),
    [
        ("system_account", "system account"),
        ("unknown_account", "does not exist"),
        ("expense_account", "asset account"),
        ("snapshots_account", "hybrid"),
        ("closed_account", "closed account"),
        ("not_a_bank_account", "bank account"),
    ],
)
async def test_register_holders_of_a_bank_account_refusals(
    session, run, make_account, case: str, message: str
) -> None:
    invest, _ = await _payee_accounts(make_account)
    value = PAYEE
    account_ids = [invest.id]
    if case == "system_account":
        account_ids.append(await run(get_or_create_system_account, "in_transit"))
    elif case == "unknown_account":
        account_ids.append(uuid.UUID(int=7))
    elif case == "expense_account":
        expense = await make_account(ledger_type="expense", subtype="other", balance_mode="hybrid")
        account_ids.append(expense.id)
    elif case == "snapshots_account":
        account_ids.append((await make_account(balance_mode="snapshots")).id)
    elif case == "closed_account":
        closed = await make_account(balance_mode="hybrid")
        await run(close_account, closed.id, date(2026, 11, 3))
        account_ids.append(closed.id)
    else:
        value = "U1234567"

    with pytest.raises(FinanceValidationError, match=message) as refused:
        await run(register_holders, "uk_bank_account", value, account_ids)

    assert "87654321" not in str(refused.value)
    h = models.AccountIdentifierHolder
    assert await session.scalar(select(func.count()).select_from(h)) == 0


async def test_a_bank_account_with_an_account_refuses_holders(session, run, make_account) -> None:
    invest, isa = await _payee_accounts(make_account)
    assigned = await run(assign_identifier, invest.id, "uk_bank_account", PAYEE)

    with pytest.raises(FinanceValidationError, match="holders") as refused:
        await run(register_holders, "uk_bank_account", PAYEE, [isa.id])

    assert "87654321" not in str(refused.value)
    assert await _holder_rows(session, assigned.id) == set()
    [row] = await list_identifiers(session)
    assert (row.account_id, row.holders) == (invest.id, ())


@pytest.mark.parametrize("register", ["assign_identifier", "ensure_identifier"])
async def test_a_held_bank_account_refuses_an_account(
    session, run, make_account, register: str
) -> None:
    invest, isa = await _payee_accounts(make_account)
    record = await run(register_holders, "uk_bank_account", PAYEE, [invest.id])
    if register == "assign_identifier":
        # The owner registering it is refused.
        with pytest.raises(FinanceValidationError, match="holders") as refused:
            await run(assign_identifier, isa.id, "uk_bank_account", PAYEE)
        assert "87654321" not in str(refused.value)
    else:
        # A connector's sync leaves it, as it leaves one on another account.
        assert await run(ensure_identifier, isa.id, "uk_bank_account", PAYEE) is False
    [row] = await list_identifiers(session)
    assert (row.id, row.account_id, row.holders) == (record.id, None, (invest.id,))
    assert await _count_events(session, "finance.account_identifier.assigned") == 0
    # Once its holders are removed, it can be registered to an account.
    await run(register_holders, "uk_bank_account", PAYEE, [])
    assert (await run(assign_identifier, isa.id, "uk_bank_account", PAYEE)).account_id == isa.id


async def test_a_held_bank_account_cannot_be_dismissed(session, run, make_account) -> None:
    invest, _ = await _payee_accounts(make_account)
    record = await run(register_holders, "uk_bank_account", PAYEE, [invest.id])

    with pytest.raises(FinanceValidationError, match="holders"):
        await run(dismiss_identifier, record.id)

    assert (await list_identifiers(session))[0].dismissed_at is None
    await run(register_holders, "uk_bank_account", PAYEE, [])
    assert (await run(dismiss_identifier, record.id)).dismissed_at is not None


@pytest.mark.parametrize(
    ("kind", "value", "stated"),
    [
        ("ibkr_account", "U1234567", "ibkr_account"),  # no such kind of holder
        ("uk_bank_account", PAYEE, "card_merchant"),  # not its identifier's kind
        ("uk_bank_account", PAYEE, None),  # every row states its kind
    ],
)
async def test_only_bank_accounts_and_card_merchants_have_holder_rows(
    session, run, make_account, kind: str, value: str, stated: str | None
) -> None:
    invest, _ = await _payee_accounts(make_account)
    identifier_id = await run(get_or_create_identifier, kind, value)
    values = {"identifier_id": identifier_id, "account_id": invest.id}
    if stated is not None:
        values["kind"] = stated
    with pytest.raises(exc.IntegrityError):
        async with session.begin_nested():
            await session.execute(insert(models.AccountIdentifierHolder).values(values))


@pytest.mark.parametrize("first", ["holders", "account"])
async def test_concurrent_holders_and_account_one_wins(
    backend_url: str, owner_url: str, migrated: None, first: str
) -> None:
    """Holders and an account registered on one bank account at once, with real commits:
    whichever holds the row first wins, and the other, waiting on it, is refused."""
    value = "998877:11223344"
    command = "test.holders_race"
    engine, owner = create_engine(backend_url), create_engine(owner_url)
    sessions = make_sessionmaker(engine)
    actor = Actor("user", "test")

    async def committed(fn, /, *args, hold: float = 0.0, wait: float = 0.0, **kwargs):
        await asyncio.sleep(wait)
        async with sessions() as s, command_context(s, actor, command, {}) as ctx:
            result = await fn(ctx, *args, **kwargs)
            # Still uncommitted while the other runs.
            await asyncio.sleep(hold)
            return result

    def account(name: str):
        return committed(
            create_account, name=name, ledger_type="asset", subtype="bank", balance_mode="hybrid"
        )

    ids: list[uuid.UUID] = []
    try:
        invest, isa = await account("Race Invest"), await account("Race ISA")
        ids += [invest.id, isa.id]
        ids.append(await committed(get_or_create_identifier, "uk_bank_account", value))
        hold, wait = (0.5, 0.0), (0.0, 0.1)
        if first == "account":
            hold, wait = hold[::-1], wait[::-1]
        results = await asyncio.gather(
            committed(
                register_holders,
                "uk_bank_account",
                value,
                [invest.id],
                hold=hold[0],
                wait=wait[0],
            ),
            committed(
                assign_identifier, isa.id, "uk_bank_account", value, hold=hold[1], wait=wait[1]
            ),
            return_exceptions=True,
        )
        loser = results[1] if first == "holders" else results[0]
        assert isinstance(loser, FinanceValidationError)
        async with owner.connect() as conn:
            account_id = await conn.scalar(
                text("select account_id from finance.account_identifiers where value = :v"),
                {"v": value},
            )
            holders = await conn.scalar(
                text(
                    "select count(*) from finance.account_identifier_holders h"
                    " join finance.account_identifiers i on i.id = h.identifier_id"
                    " where i.value = :v"
                ),
                {"v": value},
            )
        assert (account_id, holders) == ((None, 1) if first == "holders" else (isa.id, 0))
    finally:
        async with owner.begin() as conn:
            await conn.execute(
                text(
                    "delete from finance.account_identifier_holders where identifier_id in"
                    " (select id from finance.account_identifiers where value = :v)"
                ),
                {"v": value},
            )
            await conn.execute(
                text("delete from core.events where subject_id = any(:ids)"), {"ids": ids}
            )
            await conn.execute(
                text("delete from finance.account_identifiers where value = :v"), {"v": value}
            )
            await conn.execute(
                text("delete from finance.accounts where id = any(:ids)"), {"ids": ids}
            )
            await conn.execute(
                text("delete from core.audit_log where command = :c"), {"c": command}
            )
        await engine.dispose()
        await owner.dispose()


@pytest.mark.parametrize("registration", ["assign", "bank_holders", "card_merchant"])
async def test_a_registration_s_row_lock_lets_a_transaction_name_the_identifier(
    backend_url: str, owner_url: str, migrated: None, registration: str
) -> None:
    """A registration locks its identifier's row FOR NO KEY UPDATE, not FOR UPDATE: a
    transaction inserted meanwhile naming the identifier (its foreign key check takes a KEY
    SHARE lock on the row) is not blocked, so a sync posting it doesn't wait on, or
    deadlock with, a registration that rematches transfers. Checked with the lock the
    foreign key check takes, under a lock timeout, while the registration is uncommitted."""
    kind, value = (
        ("card_merchant", "merch_LOCKTEST1")
        if registration == "card_merchant"
        else ("uk_bank_account", "998877:55667788")
    )
    command = "test.registration_lock"
    engine, owner = create_engine(backend_url), create_engine(owner_url)
    sessions = make_sessionmaker(engine)
    actor = Actor("user", "test")
    ids: list[uuid.UUID] = []
    try:
        async with sessions() as s, command_context(s, actor, command, {}) as ctx:
            account = await create_account(
                ctx, name="Lock Test", ledger_type="asset", subtype="bank", balance_mode="hybrid"
            )
            identifier_id = await get_or_create_identifier(ctx, kind, value)
        ids += [account.id, identifier_id]

        async with sessions() as s, command_context(s, actor, command, {}) as ctx:
            if registration == "assign":
                await assign_identifier(ctx, account.id, kind, value)
            elif registration == "bank_holders":
                await register_holders(ctx, "uk_bank_account", value, [account.id])
            else:
                await register_card_merchant(ctx, value, [account.id])
            # The registration holds the row, uncommitted, while a sync names it.
            async with engine.connect() as sync:
                await sync.execute(text("set local lock_timeout = '2s'"))
                await sync.execute(
                    text("select id from finance.account_identifiers where id = :id for key share"),
                    {"id": identifier_id},
                )
                await sync.rollback()
    finally:
        async with owner.begin() as conn:
            await conn.execute(
                text(
                    "delete from finance.account_identifier_holders where identifier_id = any(:ids)"
                ),
                {"ids": ids},
            )
            await conn.execute(
                text("delete from core.events where subject_id = any(:ids)"), {"ids": ids}
            )
            await conn.execute(
                text("delete from finance.account_identifiers where id = any(:ids)"), {"ids": ids}
            )
            await conn.execute(
                text("delete from finance.accounts where id = any(:ids)"), {"ids": ids}
            )
            await conn.execute(
                text("delete from core.audit_log where command = :c"), {"c": command}
            )
        await engine.dispose()
        await owner.dispose()
