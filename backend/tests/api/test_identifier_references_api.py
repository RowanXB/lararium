"""Registering references on a shared payee bank account through the API (account type
catalog design §8.1)."""

from typing import Any

import pytest

from lararium.modules.finance.system_accounts import get_or_create_system_account
from tests.finance.monzo.test_card_merchants import platform_accounts

FINANCE = "/api/v1/finance"
REGISTER = f"{FINANCE}/accounts/identifiers/references"
PAYEE = "11-22-33 87654321"


@pytest.fixture(autouse=True)
async def system_accounts(run) -> None:
    for key in ("unassigned", "in_transit", "opening_balances"):
        await run(get_or_create_system_account, key)


async def test_register_replace_and_remove(client, run) -> None:
    invest, isa = await platform_accounts(run)
    references = [
        {"account_id": str(invest.id), "reference": " ref-b "},
        {"account_id": str(isa.id), "reference": "REF-A"},
    ]

    response = await client.post(REGISTER, json={"value": PAYEE, "references": references})

    assert response.status_code == 200
    body = response.json()
    assert (body["kind"], body["value"], body["account_id"]) == (
        "uk_bank_account",
        "112233:87654321",
        None,
    )
    assert body["references"] == [
        {"account_id": str(isa.id), "reference": "REF-A"},
        {"account_id": str(invest.id), "reference": "REF-B"},
    ]

    replaced = await client.post(
        REGISTER,
        json={"value": PAYEE, "references": [{"account_id": str(isa.id), "reference": "REF-C"}]},
    )
    assert (replaced.status_code, replaced.json()["id"]) == (200, body["id"])
    assert replaced.json()["references"] == [{"account_id": str(isa.id), "reference": "REF-C"}]

    removed = await client.post(REGISTER, json={"value": PAYEE, "references": []})
    assert (removed.status_code, removed.json()["references"]) == (200, [])


@pytest.mark.parametrize(
    "case",
    [
        "same_reference",
        "same_account",
        "blank_reference",
        "system_account",
        "unknown_account",
        "has_an_account",
        "bad_value",
    ],
)
async def test_refusals_422(client, run, case: str) -> None:
    invest, isa = await platform_accounts(run)
    value = PAYEE
    references = [{"account_id": str(invest.id), "reference": "REF-A"}]
    if case == "same_reference":
        references.append({"account_id": str(isa.id), "reference": "ref-a"})
    elif case == "same_account":
        references.append({"account_id": str(invest.id), "reference": "REF-B"})
    elif case == "blank_reference":
        references.append({"account_id": str(isa.id), "reference": "  "})
    elif case == "system_account":
        in_transit = await run(get_or_create_system_account, "in_transit")
        references.append({"account_id": str(in_transit), "reference": "REF-B"})
    elif case == "unknown_account":
        references.append(
            {"account_id": "0190a0b0-0000-7000-8000-000000000099", "reference": "REF-B"}
        )
    elif case == "has_an_account":
        added = await client.post(
            f"{FINANCE}/accounts/{isa.id}/identifiers",
            json={"kind": "uk_bank_account", "value": PAYEE},
        )
        assert added.status_code == 201
    else:
        value = "11-22-33"

    response = await client.post(REGISTER, json={"value": value, "references": references})

    assert response.status_code == 422
    message = response.json()["detail"]["message"]
    assert message
    assert "REF" not in message and "87654321" not in message


async def test_identifier_with_references_refuses_an_account_422(client, run) -> None:
    invest, isa = await platform_accounts(run)
    references = [{"account_id": str(invest.id), "reference": "REF-A"}]
    registered = await client.post(REGISTER, json={"value": PAYEE, "references": references})
    assert registered.status_code == 200

    added = await client.post(
        f"{FINANCE}/accounts/{isa.id}/identifiers",
        json={"kind": "uk_bank_account", "value": PAYEE},
    )

    assert added.status_code == 422
    assert "references" in added.json()["detail"]["message"]


async def test_register_and_remove_heal_once(client, run, monkeypatch) -> None:
    """healing_command heals at the end of the command, so the route doesn't heal again."""
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
    references = [
        {"account_id": str(invest.id), "reference": "REF-A"},
        {"account_id": str(isa.id), "reference": "REF-B"},
    ]
    registered = await client.post(REGISTER, json={"value": PAYEE, "references": references})
    assert registered.status_code == 200
    assert len(heals) == 1

    removed = await client.post(REGISTER, json={"value": PAYEE, "references": []})
    assert removed.status_code == 200
    assert len(heals) == 2
