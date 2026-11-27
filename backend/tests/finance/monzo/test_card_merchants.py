"""Monzo naming registered card merchants (eToro design §5.12, Task 15), and the suggestion to
register one whose website is on a connected platform's domain."""

from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from lararium.core.connections import create_connection
from lararium.modules.finance.accounts import AccountRecord, create_account
from lararium.modules.finance.cashflow import cash_flow
from lararium.modules.finance.identifiers import (
    dismiss_card_merchant,
    register_card_merchant,
)
from lararium.modules.finance.instruments import get_currency
from lararium.modules.finance.ledger import (
    CounterpartyFields,
    PostingInput,
    TransactionInput,
    upsert_connector_transaction,
)
from lararium.modules.finance.system_accounts import get_or_create_system_account
from lararium.modules.finance.transfers import (
    card_merchant_suggestions,
    find_transfer_violations,
    list_transfers,
    reject,
    transfer_contra,
)
from tests.finance.monzo.fixtures import ACCOUNT_ID, make_merchant, make_transaction
from tests.finance.monzo.test_normalize_transfers import (
    B_IDENTIFIER,
    account_map,
    contra,
    faster_payment,
    heal,
    identifier_row,
    manual_bank,
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


async def test_a_card_refund_from_a_registered_merchant_still_pairs(
    session: AsyncSession, run
) -> None:
    """A Trading 212 withdrawal paid back to the registered card pairs with the Monzo refund
    as before; the refund flag leaves it out of the cash-flow report."""
    gbp = await get_currency(session, "GBP")
    invest, isa = await platform_accounts(run)
    await run(register_card_merchant, MERCHANT, [invest.id, isa.id])
    connection = await monzo_connection(run)
    await sync_monzo(
        session,
        connection,
        transactions={ACCOUNT_ID: [card_payment(5000, website="https://www.trading212.com")]},
    )
    when = datetime(2026, 3, 1, 12, 0, 5, tzinfo=UTC)
    contra_id = await transfer_contra(
        session,
        source=("connection", f"{invest.source_connection_id}:wd_1"),
        own_account_id=invest.id,
        counterparty_identifier_id=None,
        other_postings=0,
        never_transfer=False,
    )
    await run(
        upsert_connector_transaction,
        TransactionInput(
            occurred_at=when,
            description="Withdrawal",
            status="booked",
            origin="connector",
            connection_id=invest.source_connection_id,
            external_id="wd_1",
            postings=[
                PostingInput(invest.id, gbp.id, Decimal("-50")),
                PostingInput(contra_id, gbp.id, Decimal("50")),
            ],
            counterparty=CounterpartyFields(name="Withdrawal", bank_transfer=False),
        ),
    )
    await heal(session)

    row = await transaction_row(session, "tx_CARD0001")
    refund = await session.scalar(
        text("select refund from finance.transactions where id = :i"), {"i": row.id}
    )
    assert refund is True
    [transfer] = await list_transfers(session)
    assert (transfer.status, transfer.in_transaction_id) == ("matched", row.id)
    flow = await cash_flow(session, date(2026, 3, 1), date(2026, 3, 31))
    assert (flow.income, flow.spending) == (Decimal(0), Decimal(0))
    assert await find_transfer_violations(session) == []


async def _replay_state(session: AsyncSession) -> dict[str, Any]:
    """What a replay must leave as it was: each Monzo transaction's category, labels, place,
    category splits and contra; every transfer; every owner decision on transfers."""
    rows = await session.execute(
        text(
            "select t.external_id, t.status, t.category, t.labels, t.include_in_spending,"
            " t.payment_scheme, t.place_entity_id, t.bank_transfer,"
            " (select array_agg(s.category || '=' || s.quantity::text order by s.position)"
            "  from finance.transaction_category_splits s where s.transaction_id = t.id)"
            " as splits,"
            " (select array_agg(p.account_id::text || ' ' || p.quantity::text"
            "  order by p.position) from finance.postings p where p.transaction_id = t.id)"
            " as postings"
            " from finance.transactions t join core.connections c on c.id = t.connection_id"
            " where c.connector = 'monzo' order by t.external_id"
        )
    )
    transfers = sorted(
        (
            t.id,
            t.status,
            t.review_reason,
            t.out_transaction_id,
            t.in_transaction_id,
            t.settlement_transaction_id,
        )
        for t in await list_transfers(session)
    )
    decisions = await session.execute(
        text(
            "select kind, subject_kind, subject_ref, other_kind, other_ref"
            " from finance.transfer_decisions order by 1, 2, 3, 4, 5"
        )
    )
    return {
        "transactions": [tuple(r) for r in rows],
        "transfers": transfers,
        "decisions": [tuple(r) for r in decisions],
    }


async def test_registration_replay_leaves_categories_places_transfers_and_decisions(
    session: AsyncSession, run
) -> None:
    """Registering a card merchant makes the next sync replay Monzo's history (Task 15): the
    replay names the merchant's card payments, and changes nothing else the history made or
    the owner decided."""
    gbp = await get_currency(session, "GBP")
    invest, isa = await platform_accounts(run)
    await manual_bank(run, gbp.id, identifier=B_IDENTIFIER)
    await manual_bank(run, gbp.id, identifier="445566:87654321", name="Bank C")
    connection = await monzo_connection(run)
    history = [
        card_payment(-5000, website="https://www.trading212.com"),
        # Another merchant's card payment, with a category split and a place of its own.
        make_transaction(
            id="tx_GROCER",
            amount=-1234,
            local_amount=-1234,
            scheme="mastercard",
            labels=["weekly"],
            categories={"groceries": -1000, "eating_out": -234},
            merchant=make_merchant(),
        ),
        # A Faster Payment to Bank B: a transfer, left as the engine made it.
        faster_payment(-10000),
        # One to Bank C, which the owner rejects.
        faster_payment(-2500, tx_id="tx_FPS0002", sort_code="44-55-66", account_number="87654321"),
    ]
    await sync_monzo(session, connection, transactions={ACCOUNT_ID: history})
    await heal(session)
    to_c = (await transaction_row(session, "tx_FPS0002")).id
    [rejected] = [t for t in await list_transfers(session) if t.out_transaction_id == to_c]
    await run(reject, rejected.id)
    await heal(session)
    before = await _replay_state(session)
    assert len(before["transfers"]) == 2 and before["decisions"]
    places = {r[0]: r[6] for r in before["transactions"]}
    assert places["tx_CARD0001"] is not None and places["tx_GROCER"] is not None

    record = await run(register_card_merchant, MERCHANT, [invest.id, isa.id])
    report = await sync_monzo(session, connection, transactions={})
    await heal(session)

    # The replay ran and named the merchant's payment, and only that changed.
    assert report.updated >= 1
    assert report.updated + report.unchanged == len(history)
    card = await transaction_row(session, "tx_CARD0001")
    assert card.counterparty_identifier_id == record.id
    assert await _replay_state(session) == before
    assert await find_transfer_violations(session) == []
    assert await heal(session) == 0


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
        "https://a]b.com",
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
