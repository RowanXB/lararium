"""A chain address names the other side of a deposit or withdrawal, so the transfer engine can
pair it with the owner's own wallet (design 5.12)."""

from datetime import timedelta
from decimal import Decimal

import pytest
from sqlalchemy import text

from lararium.core.actors import Actor
from lararium.core.commands import command_context
from lararium.modules.finance.accounts import create_account
from lararium.modules.finance.identifiers import (
    assign_identifier,
    get_or_create_identifier,
    list_identifiers,
)
from lararium.modules.finance.transfers import (
    find_transfer_violations,
    heal_transfers,
    suggestions,
)
from lararium.modules.finance.transfers.queries import list_transfers
from lararium.modules.finance.valuation import balances_asof

from .fixtures import BASE_TIME_MS, HOUR_MS
from .normalizing import (
    assert_balanced_and_quiet,
    at,
    keys,
    normalize_rows,
    plain,
    postings,
    transaction,
)
from .syncing import binance_connection
from .test_records_capital import _deposit, _text_time, _withdrawal

WALLET_ADDRESS = "0x" + "a" * 40
OTHER_ADDRESS = "0x" + "b" * 40


async def _heal(session) -> int:
    async with command_context(
        session, Actor("system", "transfers"), "finance.heal_transfers", {}
    ) as ctx:
        return await heal_transfers(ctx)


async def _wallet(run, address: str | None = WALLET_ADDRESS):
    account = await run(
        create_account,
        name="Own wallet",
        ledger_type="asset",
        subtype="other",
        balance_mode="hybrid",
    )
    if address is not None:
        await run(assign_identifier, account.id, "chain_address", address)
    return account


async def _holdings(session, day) -> dict[str, Decimal]:
    """Quantity of each account on the day, by name."""
    rows = await balances_asof(session, day)
    names = {
        r.id: r.name for r in await session.execute(text("select id, name from finance.accounts"))
    }
    return {names[r.account_id]: r.quantity for r in rows if r.quantity != 0}


def _day():
    return (at(BASE_TIME_MS) + timedelta(days=2)).date()


async def test_withdrawal_to_a_registered_address_is_a_transfer(session, run) -> None:
    connection = await binance_connection(run)
    wallet = await _wallet(run)
    row = _withdrawal(6, address=WALLET_ADDRESS)

    await normalize_rows(session, connection.id, ("withdrawals", [row]))
    await _heal(session)

    key = f"withdrawal:{row['id']}"
    assert plain(await postings(session, key)) == [
        ("Binance Spot", "AAA", Decimal(-2)),
        ("In transit", "AAA", Decimal(2)),
    ]
    [transfer] = await list_transfers(session)
    assert (transfer.status, transfer.to_account_id) == ("inferred", wallet.id)
    assert transfer.placeholder_transaction_id is not None
    held = await _holdings(session, _day())
    # The placeholder stands in for the wallet's receipt: only the fee left net worth.
    assert held["Own wallet"] == Decimal(2)
    assert held["Binance Spot"] == Decimal("-2.1")
    assert "In transit" not in held
    assert held["Own wallet"] + held["Binance Spot"] == Decimal("-0.1")
    assert await find_transfer_violations(session) == []
    await assert_balanced_and_quiet(session, connection.id)


async def test_withdrawal_to_an_unknown_address_appears_in_suggestions(session, run) -> None:
    connection = await binance_connection(run)
    row = _withdrawal(6, address=OTHER_ADDRESS)

    await normalize_rows(session, connection.id, ("withdrawals", [row]))
    await _heal(session)

    assert plain(await postings(session, f"withdrawal:{row['id']}"))[1][0] == "Unassigned"
    [suggestion] = await suggestions(session)
    assert (suggestion.kind, suggestion.value) == ("chain_address", OTHER_ADDRESS)
    assert (suggestion.sort_code, suggestion.account_number) == (None, None)
    assert (suggestion.outgoing, suggestion.incoming) == (1, 0)
    assert await list_transfers(session) == []


async def test_registering_later_re_evaluates_past_withdrawals(session, run) -> None:
    connection = await binance_connection(run)
    row = _withdrawal(6, address=WALLET_ADDRESS)
    await normalize_rows(session, connection.id, ("withdrawals", [row]))
    await _heal(session)
    assert await list_transfers(session) == []

    await _wallet(run)
    await _heal(session)

    [transfer] = await list_transfers(session)
    assert transfer.status == "inferred"
    assert plain(await postings(session, f"withdrawal:{row['id']}"))[1][0] == "In transit"
    assert await suggestions(session) == []
    assert await find_transfer_violations(session) == []


async def test_deposit_from_a_registered_address(session, run) -> None:
    connection = await binance_connection(run)
    wallet = await _wallet(run)
    row = _deposit(1, sourceAddress=WALLET_ADDRESS)

    await normalize_rows(session, connection.id, ("deposits", [row]))
    await _heal(session)

    assert plain(await postings(session, f"deposit:{row['id']}")) == [
        ("Binance Spot", "AAA", Decimal(5)),
        ("In transit", "AAA", Decimal(-5)),
    ]
    [transfer] = await list_transfers(session)
    assert (transfer.from_account_id, transfer.to_account_id) == (
        wallet.id,
        (await _spot(session)),
    )
    held = await _holdings(session, _day())
    assert held["Binance Spot"] + held["Own wallet"] == Decimal(5) - Decimal(5)
    assert await find_transfer_violations(session) == []


async def _spot(session):
    return await session.scalar(text("select id from finance.accounts where name = 'Binance Spot'"))


async def test_internal_binance_transfer_is_unidentified(session, run) -> None:
    connection = await binance_connection(run)
    await _wallet(run)
    row = _deposit(1, transferType=1, txId="Off-chain transfer 1", sourceAddress=WALLET_ADDRESS)
    out = _withdrawal(
        6, id="w-int", transferType=1, txId="Internal transfer 2", address=WALLET_ADDRESS
    )

    await normalize_rows(session, connection.id, ("deposits", [row]), ("withdrawals", [out]))
    await _heal(session)

    assert [r.kind for r in await list_identifiers(session)] == ["chain_address"]  # the wallet's
    assert await list_transfers(session) == []
    for key in (f"deposit:{row['id']}", f"withdrawal:{out['id']}"):
        assert "In transit" not in {p[0] for p in plain(await postings(session, key))}
    assert await suggestions(session) == []


@pytest.mark.parametrize("spelling", [WALLET_ADDRESS, "0x" + "A" * 40, "0X" + "a" * 40])
async def test_evm_addresses_match_whatever_their_case(session, run, spelling) -> None:
    connection = await binance_connection(run)
    await _wallet(run, WALLET_ADDRESS)
    row = _withdrawal(6, address=spelling)

    await normalize_rows(session, connection.id, ("withdrawals", [row]))
    await _heal(session)

    [transfer] = await list_transfers(session)
    assert transfer.status == "inferred"
    values = {r.value for r in await list_identifiers(session)}
    assert values == {WALLET_ADDRESS}


async def test_binance_deposit_address_is_not_registered(session, run) -> None:
    connection = await binance_connection(run)
    row = _deposit(1, address=OTHER_ADDRESS, sourceAddress=WALLET_ADDRESS)
    bare = _deposit(1, id="2002", address=OTHER_ADDRESS, sourceAddress=None)

    await normalize_rows(session, connection.id, ("deposits", [row, bare]))

    assert [r.value for r in await list_identifiers(session)] == [WALLET_ADDRESS]
    assert plain(await postings(session, f"deposit:{bare['id']}"))[1][0] == "Unassigned"
    await assert_balanced_and_quiet(session, connection.id)


async def test_chain_address_checked_by_database(session, run) -> None:
    from sqlalchemy import exc

    for bad in ("0x123", "0x" + "g!" * 20, "a" * 129):
        with pytest.raises(exc.IntegrityError):
            async with session.begin_nested():
                await session.execute(
                    text(
                        "insert into finance.account_identifiers (kind, value, created_by)"
                        " values ('chain_address', :v, 'user')"
                    ),
                    {"v": bad},
                )
    await run(get_or_create_identifier, "chain_address", WALLET_ADDRESS)


async def test_an_address_that_is_no_chain_address_is_flagged_not_failed(session, run) -> None:
    connection = await binance_connection(run)
    row = _withdrawal(6, address="short")
    deposit = _deposit(1, sourceAddress="has spaces and is long enough to pass length")

    report = await normalize_rows(
        session, connection.id, ("withdrawals", [row]), ("deposits", [deposit])
    )

    assert sorted(report.new_flags) == [
        "deposits.sourceAddress is not a chain address",
        "withdrawals.address is not a chain address",
    ]
    assert len(await keys(session, connection.id)) == 3  # both, and the fee
    assert await list_identifiers(session) == []
    assert all("short" not in flag and "spaces" not in flag for flag in report.new_flags)


async def test_failed_withdrawal_to_a_registered_address_stops_being_a_transfer(
    session, run
) -> None:
    connection = await binance_connection(run)
    await _wallet(run)
    processing = _withdrawal(4, address=WALLET_ADDRESS, completeTime=None)
    await normalize_rows(session, connection.id, ("withdrawals", [processing]))
    await _heal(session)
    assert len(await list_transfers(session)) == 1

    refunded_ms = BASE_TIME_MS + 2 * HOUR_MS
    failed = _withdrawal(5, address=WALLET_ADDRESS, completeTime=_text_time(refunded_ms))
    await normalize_rows(session, connection.id, ("withdrawals", [failed]))
    await _heal(session)

    key = f"withdrawal:{failed['id']}"
    assert await list_transfers(session) == [] or all(
        t.status == "rejected" for t in await list_transfers(session)
    )
    derived = await session.scalar(
        text(
            "select count(*) from finance.transactions where origin in ('inference', 'rule')"
            " and status <> 'void'"
        )
    )
    assert derived == 0
    assert plain(await postings(session, key)) == [
        ("Binance Spot", "AAA", Decimal(-2)),
        ("Unassigned", "AAA", Decimal(2)),
    ]
    assert len(await postings(session, f"{key}:refund")) == 3
    assert (await transaction(session, f"{key}:refund")).status == "booked"
    assert await _holdings(session, _day()) == {}
    assert await find_transfer_violations(session) == []
    assert await _heal(session) == 0
    await assert_balanced_and_quiet(session, connection.id)
