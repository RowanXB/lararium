"""Registering the own accounts a shared payee bank account is held by, through the API
(account type catalog design §8.1)."""

from typing import Any

import pytest

from lararium.modules.finance.system_accounts import get_or_create_system_account
from tests.finance.monzo.test_card_merchants import platform_accounts

FINANCE = "/api/v1/finance"
REGISTER = f"{FINANCE}/accounts/identifiers/holders"
PAYEE = "11-22-33 87654321"


@pytest.fixture(autouse=True)
async def system_accounts(run) -> None:
    for key in ("unassigned", "in_transit", "opening_balances"):
        await run(get_or_create_system_account, key)


def _body(*account_ids: object, value: str = PAYEE) -> dict[str, Any]:
    return {
        "kind": "uk_bank_account",
        "value": value,
        "account_ids": [str(a) for a in account_ids],
    }


async def test_register_replace_and_remove(client, run) -> None:
    invest, isa = await platform_accounts(run)

    response = await client.post(REGISTER, json=_body(isa.id, invest.id))

    assert response.status_code == 200
    body = response.json()
    assert (body["kind"], body["value"], body["account_id"]) == (
        "uk_bank_account",
        "112233:87654321",
        None,
    )
    assert body["holder_account_ids"] == sorted([str(invest.id), str(isa.id)])
    # Listed among each holder's identifiers.
    listed = await client.get(f"{FINANCE}/accounts/{isa.id}/identifiers")
    assert [i["id"] for i in listed.json()] == [body["id"]]

    replaced = await client.post(REGISTER, json=_body(isa.id))
    assert (replaced.status_code, replaced.json()["id"]) == (200, body["id"])
    assert replaced.json()["holder_account_ids"] == [str(isa.id)]

    removed = await client.post(REGISTER, json=_body())
    assert (removed.status_code, removed.json()["holder_account_ids"]) == (200, [])


async def test_removing_it_from_one_holder_keeps_the_others(client, run) -> None:
    invest, isa = await platform_accounts(run)
    registered = (await client.post(REGISTER, json=_body(invest.id, isa.id))).json()

    response = await client.delete(f"{FINANCE}/accounts/{isa.id}/identifiers/{registered['id']}")

    assert response.status_code == 204
    [left] = (await client.get(f"{FINANCE}/accounts/{invest.id}/identifiers")).json()
    assert (left["id"], left["holder_account_ids"]) == (registered["id"], [str(invest.id)])
    assert (await client.get(f"{FINANCE}/accounts/{isa.id}/identifiers")).json() == []


@pytest.mark.parametrize(
    "case", ["system_account", "unknown_account", "has_an_account", "bad_value", "bad_kind"]
)
async def test_refusals_422(client, run, case: str) -> None:
    invest, isa = await platform_accounts(run)
    body = _body(invest.id)
    if case == "system_account":
        body = _body(invest.id, await run(get_or_create_system_account, "in_transit"))
    elif case == "unknown_account":
        body = _body(invest.id, "0190a0b0-0000-7000-8000-000000000099")
    elif case == "has_an_account":
        added = await client.post(
            f"{FINANCE}/accounts/{isa.id}/identifiers",
            json={"kind": "uk_bank_account", "value": PAYEE},
        )
        assert added.status_code == 201
    elif case == "bad_value":
        body = _body(invest.id, value="11-22-33")
    else:
        body = {**body, "kind": "ibkr_account"}

    response = await client.post(REGISTER, json=body)

    assert response.status_code == 422
    if case != "bad_kind":  # refused by the body's schema
        message = response.json()["detail"]["message"]
        assert message and "87654321" not in message


async def test_a_held_payee_refuses_an_account_422(client, run) -> None:
    invest, isa = await platform_accounts(run)
    assert (await client.post(REGISTER, json=_body(invest.id))).status_code == 200

    added = await client.post(
        f"{FINANCE}/accounts/{isa.id}/identifiers",
        json={"kind": "uk_bank_account", "value": PAYEE},
    )

    assert added.status_code == 422
    assert "holders" in added.json()["detail"]["message"]


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
    registered = await client.post(REGISTER, json=_body(invest.id, isa.id))
    assert registered.status_code == 200
    assert len(heals) == 1

    removed = await client.post(REGISTER, json=_body())
    assert removed.status_code == 200
    assert len(heals) == 2
