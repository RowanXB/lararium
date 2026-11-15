import pytest
from sqlalchemy import exc, text

from lararium.modules.finance.accounts import get_account, set_balance_mode
from lararium.modules.finance.errors import (
    AccountIdentifierNotFoundError,
    DuplicateAccountIdentifierError,
    FinanceValidationError,
)
from lararium.modules.finance.identifiers import (
    add_identifier,
    ensure_identifier,
    list_identifiers,
    lookup_account,
    normalize_identifier,
    remove_identifier,
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


async def test_add_lookup_remove(session, run, make_account) -> None:
    account = await make_account(balance_mode="hybrid")
    added = await run(add_identifier, account.id, "uk_bank_account", "11-22-33 12345678")
    assert (added.account_id, added.kind, added.value, added.created_by) == (
        account.id,
        "uk_bank_account",
        VALUE,
        "user",
    )
    assert await lookup_account(session, "uk_bank_account", "112233/12345678") == account.id
    assert await list_identifiers(session, account.id) == [added]
    assert await list_identifiers(session) == [added]
    assert await _count_events(session, "finance.account_identifier.added") == 1

    removed = await run(remove_identifier, added.id)
    assert removed == added
    assert await lookup_account(session, "uk_bank_account", VALUE) is None
    assert await list_identifiers(session) == []
    assert await _count_events(session, "finance.account_identifier.removed") == 1
    with pytest.raises(AccountIdentifierNotFoundError):
        await run(remove_identifier, added.id)


async def test_lookup_of_a_malformed_value_finds_nothing(session) -> None:
    assert await lookup_account(session, "uk_bank_account", "not a bank account") is None


async def test_duplicate_identifier_rejected(session, run, make_account) -> None:
    first = await make_account(name="Test Current", balance_mode="hybrid")
    second = await make_account(name="Test Savings", balance_mode="hybrid")
    await run(add_identifier, first.id, "uk_bank_account", VALUE)
    with pytest.raises(DuplicateAccountIdentifierError, match="Test Current"):
        await run(add_identifier, second.id, "uk_bank_account", "11-22-33 12345678")
    assert await lookup_account(session, "uk_bank_account", VALUE) == first.id


async def test_snapshots_account_refuses_identifier(session, run, make_account) -> None:
    account = await make_account(balance_mode="snapshots")
    with pytest.raises(FinanceValidationError, match="switch the account to hybrid"):
        await run(add_identifier, account.id, "uk_bank_account", VALUE)
    switched = await run(set_balance_mode, account.id, "hybrid")
    assert switched.balance_mode == "hybrid"
    assert (await get_account(session, account.id)).balance_mode == "hybrid"
    added = await run(add_identifier, account.id, "uk_bank_account", VALUE)
    assert added.account_id == account.id


@pytest.mark.parametrize("ledger_type", ["income", "expense", "equity"])
async def test_only_assets_and_liabilities_carry_identifiers(
    run, make_account, ledger_type
) -> None:
    account = await make_account(ledger_type=ledger_type, subtype="other", balance_mode="hybrid")
    with pytest.raises(FinanceValidationError):
        await run(add_identifier, account.id, "uk_bank_account", VALUE)
    with pytest.raises(FinanceValidationError):
        await run(ensure_identifier, account.id, "uk_bank_account", VALUE)


async def test_liability_accepts_identifier(run, make_account) -> None:
    card = await make_account(ledger_type="liability", subtype="other", balance_mode="hybrid")
    added = await run(add_identifier, card.id, "uk_bank_account", VALUE)
    assert added.account_id == card.id


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
    events = await _count_events(session, "finance.account_identifier.added")
    assert events == 1

    assert await run(ensure_identifier, other.id, "uk_bank_account", VALUE) is False
    assert await lookup_account(session, "uk_bank_account", VALUE) == owner.id
    assert await list_identifiers(session, other.id) == []
    assert await _count_events(session, "finance.account_identifier.added") == events


async def test_ensure_identifier_refuses_snapshots_account(run, make_account) -> None:
    account = await make_account(balance_mode="snapshots")
    with pytest.raises(FinanceValidationError, match="switch the account to hybrid"):
        await run(ensure_identifier, account.id, "uk_bank_account", VALUE)


async def test_value_check_enforced_by_database(session, make_account) -> None:
    account = await make_account(balance_mode="hybrid")
    with pytest.raises(exc.IntegrityError, match="check constraint"):
        await session.execute(
            text(
                "insert into finance.account_identifiers (account_id, kind, value, created_by)"
                " values (:a, 'uk_bank_account', '11-22-33 12345678', 'user')"
            ),
            {"a": account.id},
        )
