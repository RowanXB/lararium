"""Registering card merchants through the API (eToro design §5.12, Task 15)."""

from typing import Any

import pytest

from lararium.core.connections import create_connection
from lararium.modules.finance.accounts import create_account
from lararium.modules.finance.system_accounts import get_or_create_system_account
from tests.finance.monzo.fixtures import ACCOUNT_ID
from tests.finance.monzo.test_card_merchants import card_payment, platform_accounts
from tests.finance.monzo.test_normalize_transfers import monzo_connection, sync_monzo

FINANCE = "/api/v1/finance"
MERCHANT = "merch_TEST212"
REGISTER = f"{FINANCE}/accounts/identifiers/card-merchants"


@pytest.fixture(autouse=True)
async def system_accounts(run) -> None:
    for key in ("unassigned", "in_transit", "opening_balances"):
        await run(get_or_create_system_account, key)


async def _suggestions(client) -> list[dict[str, Any]]:
    response = await client.get(f"{FINANCE}/transfer-suggestions")
    assert response.status_code == 200
    return response.json()


async def test_suggest_register_and_list(client, session, run) -> None:
    invest, isa = await platform_accounts(run)
    connection = await monzo_connection(run)
    payment = card_payment(-5000, website="https://www.trading212.com")
    await sync_monzo(session, connection, transactions={ACCOUNT_ID: [payment]})

    [suggestion] = await _suggestions(client)
    assert suggestion == {
        "kind": "card_merchant",
        "merchant_id": MERCHANT,
        "name": "Test Broker",
        "host": "www.trading212.com",
        "outgoing": 1,
        "incoming": 0,
        "latest": "2026-03-01",
        "account_ids": sorted([str(invest.id), str(isa.id)]),
    }

    response = await client.post(
        REGISTER, json={"merchant_id": MERCHANT, "account_ids": suggestion["account_ids"]}
    )
    assert response.status_code == 200
    body = response.json()
    assert (body["kind"], body["value"], body["account_id"]) == ("card_merchant", MERCHANT, None)
    assert sorted(body["holder_account_ids"]) == suggestion["account_ids"]
    assert await _suggestions(client) == []
    [listed] = (await client.get(f"{FINANCE}/accounts/{invest.id}/identifiers")).json()
    assert listed["id"] == body["id"]

    # Removing it from one holder's identifiers keeps the other.
    removed = await client.delete(f"{FINANCE}/accounts/{invest.id}/identifiers/{body['id']}")
    assert removed.status_code == 204
    assert (await client.get(f"{FINANCE}/accounts/{invest.id}/identifiers")).json() == []
    [kept] = (await client.get(f"{FINANCE}/accounts/{isa.id}/identifiers")).json()
    assert kept["holder_account_ids"] == [str(isa.id)]


async def test_dismissed_suggestion_stays_dismissed(client, session, run) -> None:
    await platform_accounts(run)
    connection = await monzo_connection(run)
    payment = card_payment(-5000, website="https://www.trading212.com")
    await sync_monzo(session, connection, transactions={ACCOUNT_ID: [payment]})

    response = await client.post(
        f"{FINANCE}/transfer-suggestions/card-merchants/dismiss", json={"merchant_id": MERCHANT}
    )
    assert response.status_code == 204
    assert await _suggestions(client) == []
    await sync_monzo(session, connection, transactions={ACCOUNT_ID: [payment]})
    assert await _suggestions(client) == []


async def test_snapshots_account_refused_422(client, run) -> None:
    invest, _ = await platform_accounts(run)
    connection = await run(create_connection, connector="etoro", display_name="eToro", config={})
    snapshots = await run(
        create_account,
        name="MoneyFarm",
        ledger_type="asset",
        subtype="broker",
        balance_mode="snapshots",
        source_connection_id=connection.id,
    )

    response = await client.post(
        REGISTER, json={"merchant_id": MERCHANT, "account_ids": [str(invest.id), str(snapshots.id)]}
    )

    assert response.status_code == 422
    assert response.json()["detail"]["message"]


@pytest.mark.parametrize("merchant_id", ["merch_", "tx_TEST", "merch_a b"])
async def test_bad_merchant_id_refused_422(client, run, merchant_id: str) -> None:
    invest, _ = await platform_accounts(run)
    response = await client.post(
        REGISTER, json={"merchant_id": merchant_id, "account_ids": [str(invest.id)]}
    )
    assert response.status_code == 422


async def test_unknown_account_refused_422(client, run) -> None:
    response = await client.post(
        REGISTER,
        json={"merchant_id": MERCHANT, "account_ids": ["0190a0b0-0000-7000-8000-000000000099"]},
    )
    assert response.status_code == 422


async def test_malformed_website_leaves_the_other_suggestions(client, session, run) -> None:
    await platform_accounts(run)
    connection = await monzo_connection(run)
    payments = [
        card_payment(-100, tx_id="tx_CARD0001", merchant="merch_BAD", website="https://[abc"),
        card_payment(-100, tx_id="tx_CARD0002", website="https://www.trading212.com"),
    ]
    await sync_monzo(session, connection, transactions={ACCOUNT_ID: payments})

    assert [s["merchant_id"] for s in await _suggestions(client)] == [MERCHANT]


async def test_closing_a_holder_through_the_api(client, run) -> None:
    invest, isa = await platform_accounts(run)
    body = {"merchant_id": MERCHANT, "account_ids": [str(invest.id), str(isa.id)]}
    assert (await client.post(REGISTER, json=body)).status_code == 200

    closed = await client.post(
        f"{FINANCE}/accounts/{isa.id}/close", json={"closed_on": "2026-03-02"}
    )

    assert closed.status_code == 200
    # The closed account stays a holder, so its card-deposit history stands.
    [kept] = (await client.get(f"{FINANCE}/accounts/{invest.id}/identifiers")).json()
    assert kept["holder_account_ids"] == sorted([str(invest.id), str(isa.id)])
    edited = await client.post(REGISTER, json={"merchant_id": MERCHANT, "account_ids": []})
    assert (edited.status_code, edited.json()["holder_account_ids"]) == (200, [])


async def test_register_and_remove_heal_once(client, run, monkeypatch) -> None:
    """healing_command heals at the end of the command, so the routes don't heal again."""
    from lararium.modules.finance import transfers
    from lararium.modules.finance.transfers import engine

    heals: list[object] = []
    real = engine.heal_transfers

    async def counted(ctx: Any) -> int:
        heals.append(ctx)
        return await real(ctx)

    monkeypatch.setattr(engine, "heal_transfers", counted)
    monkeypatch.setattr(transfers, "heal_transfers", counted)
    invest, isa = await platform_accounts(run)
    body = {"merchant_id": MERCHANT, "account_ids": [str(invest.id), str(isa.id)]}
    registered = await client.post(REGISTER, json=body)
    assert registered.status_code == 200
    assert len(heals) == 1

    removed = await client.delete(
        f"{FINANCE}/accounts/{invest.id}/identifiers/{registered.json()['id']}"
    )
    assert removed.status_code == 204
    assert len(heals) == 2
