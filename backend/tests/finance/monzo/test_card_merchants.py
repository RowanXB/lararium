"""Monzo naming registered card merchants (eToro design §5.12, Task 15), and the suggestion to
register one whose website is on a connected platform's domain."""

from datetime import date
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from lararium.core.connections import create_connection
from lararium.modules.finance.accounts import AccountRecord, create_account
from lararium.modules.finance.identifiers import (
    dismiss_card_merchant,
    register_card_merchant,
)
from lararium.modules.finance.system_accounts import get_or_create_system_account
from lararium.modules.finance.transfers import card_merchant_suggestions, list_transfers
from tests.finance.monzo.fixtures import ACCOUNT_ID, make_merchant, make_transaction
from tests.finance.monzo.test_normalize_transfers import (
    account_map,
    contra,
    faster_payment,
    heal,
    identifier_row,
    monzo_connection,
    sync_monzo,
    transaction_row,
)

MERCHANT = "merch_TEST212"


def card_payment(
    amount: int, *, tx_id: str = "tx_CARD0001", merchant: str = MERCHANT, website: str
) -> dict[str, Any]:
    return make_transaction(
        id=tx_id,
        amount=amount,
        local_amount=amount,
        scheme="mastercard",
        description="TRADING 212",
        merchant=make_merchant(id=merchant, name="Test Broker", metadata={"website": website}),
    )


async def platform_accounts(run, connector: str = "trading212") -> list[AccountRecord]:
    """Two accounts on connections of the connector's, as Trading 212's Invest and ISA."""
    found: list[AccountRecord] = []
    for name in ("Invest", "ISA"):
        connection = await run(create_connection, connector=connector, display_name=name, config={})
        found.append(
            await run(
                create_account,
                name=name,
                ledger_type="asset",
                subtype="broker",
                balance_mode="transactions",
                source_connection_id=connection.id,
            )
        )
    return found


async def test_monzo_card_payment_identified_after_register(session: AsyncSession, run) -> None:
    invest, isa = await platform_accounts(run)
    connection = await monzo_connection(run)
    payment = card_payment(-5000, website="https://www.trading212.com")
    await sync_monzo(session, connection, transactions={ACCOUNT_ID: [payment]})
    assert (await transaction_row(session, "tx_CARD0001")).counterparty_identifier_id is None

    record = await run(register_card_merchant, MERCHANT, [invest.id, isa.id])
    # The next sync replays the history, though it fetches nothing new.
    await sync_monzo(session, connection, transactions={})
    await heal(session)

    row = await transaction_row(session, "tx_CARD0001")
    assert (row.counterparty_identifier_id, row.bank_transfer) == (record.id, False)
    monzo = (await account_map(session, connection))[ACCOUNT_ID]
    unassigned = await run(get_or_create_system_account, "unassigned")
    assert await contra(session, row.id, monzo) == unassigned
    assert await list_transfers(session) == []

    # A sync with the registration unchanged replays nothing, and keeps it.
    report = await sync_monzo(session, connection, transactions={})
    assert (report.created, report.updated, report.unchanged) == (0, 0, 0)

    # Removing the registration un-names it on the next sync.
    await run(register_card_merchant, MERCHANT, [])
    await sync_monzo(session, connection, transactions={})
    assert (await transaction_row(session, "tx_CARD0001")).counterparty_identifier_id is None


async def test_bank_payment_still_names_its_bank_account(session: AsyncSession, run) -> None:
    invest, _ = await platform_accounts(run)
    await run(register_card_merchant, MERCHANT, [invest.id])
    connection = await monzo_connection(run)
    payment = faster_payment(-10000) | {"merchant": make_merchant(id=MERCHANT)}

    await sync_monzo(session, connection, transactions={ACCOUNT_ID: [payment]})

    row = await transaction_row(session, "tx_FPS0001")
    identifier = await identifier_row(session, "112233:12345678")
    assert row.counterparty_identifier_id == identifier.id


async def _suggested(session: AsyncSession) -> dict[str, Any]:
    return {s.merchant_id: s for s in await card_merchant_suggestions(session)}


async def test_suggestion_offered_for_trading212_merchant(session: AsyncSession, run) -> None:
    connection = await monzo_connection(run)
    payments = [
        card_payment(-5000, website="https://www.trading212.com/"),
        card_payment(-2000, tx_id="tx_CARD0002", website="https://www.trading212.com/"),
        card_payment(1000, tx_id="tx_CARD0003", website="https://www.trading212.com/"),
        card_payment(-100, tx_id="tx_CARD0004", merchant="merch_FAKE", website="nottrading212.com"),
        card_payment(-100, tx_id="tx_CARD0005", merchant="merch_ROOT", website="trading212.com"),
        card_payment(-100, tx_id="tx_CARD0006", merchant="merch_TORO", website="https://etoro.com"),
    ]
    await sync_monzo(session, connection, transactions={ACCOUNT_ID: payments})

    # No platform connected: nothing to offer.
    assert await card_merchant_suggestions(session) == []

    invest, isa = await platform_accounts(run)
    suggested = await _suggested(session)
    # A subdomain or the domain itself, never a name merely containing it; no eToro
    # connection, so no eToro merchant.
    assert set(suggested) == {MERCHANT, "merch_ROOT"}
    first = suggested[MERCHANT]
    assert (first.name, first.host, first.outgoing, first.incoming, first.latest) == (
        "Test Broker",
        "www.trading212.com",
        2,
        1,
        date(2026, 3, 1),
    )
    assert set(first.account_ids) == {invest.id, isa.id}

    # Registered: no longer a suggestion.
    await run(register_card_merchant, MERCHANT, [invest.id])
    assert set(await _suggested(session)) == {"merch_ROOT"}
    # Unregistered again: a suggestion again.
    await run(register_card_merchant, MERCHANT, [])
    assert set(await _suggested(session)) == {MERCHANT, "merch_ROOT"}
    # Dismissed: it stays dismissed.
    await run(dismiss_card_merchant, "merch_ROOT")
    await sync_monzo(session, connection, transactions={ACCOUNT_ID: payments})
    assert set(await _suggested(session)) == {MERCHANT}


async def test_suggestion_offers_only_accounts_that_can_be_registered(
    session: AsyncSession, run
) -> None:
    connection = await monzo_connection(run)
    await sync_monzo(
        session,
        connection,
        transactions={ACCOUNT_ID: [card_payment(-100, website="https://www.etoro.com")]},
    )
    etoro = await run(create_connection, connector="etoro", display_name="eToro", config={})
    accounts: dict[str, UUID] = {}
    for name, mode in (("Trading", "transactions"), ("MoneyFarm", "snapshots")):
        accounts[name] = (
            await run(
                create_account,
                name=name,
                ledger_type="asset",
                subtype="broker",
                balance_mode=mode,
                source_connection_id=etoro.id,
            )
        ).id

    [suggestion] = await card_merchant_suggestions(session)
    assert suggestion.account_ids == (accounts["Trading"],)


@pytest.mark.parametrize(
    "website",
    [
        "https://trading212.com.evil.example",
        "https://evil.example/trading212.com",
        "not a url",
        "https://[trading212.com",
        "//a]b.com",
    ],
)
async def test_suggestion_matches_the_host_only(session: AsyncSession, run, website: str) -> None:
    await platform_accounts(run)
    connection = await monzo_connection(run)
    # The hostile website on one merchant; another merchant is still suggested.
    payments = [
        card_payment(-100, website=website),
        card_payment(-100, tx_id="tx_CARD0002", merchant="merch_GOOD", website="trading212.com"),
    ]
    await sync_monzo(session, connection, transactions={ACCOUNT_ID: payments})
    assert [s.merchant_id for s in await card_merchant_suggestions(session)] == ["merch_GOOD"]
