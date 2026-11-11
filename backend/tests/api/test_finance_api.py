import json

import pytest

from lararium.api.export_openapi import render_openapi


async def _gbp_id(client) -> str:
    instruments = (await client.get("/api/v1/finance/instruments")).json()
    return next(i["id"] for i in instruments if i["symbol"] == "GBP")


async def _account(client, **overrides) -> str:
    body = {"name": "Bank", "ledger_type": "asset", "subtype": "bank", "balance_mode": "snapshots"}
    response = await client.post("/api/v1/finance/accounts", json=body | overrides)
    assert response.status_code == 201
    return response.json()["id"]


async def test_create_and_list_accounts(client) -> None:
    await _account(client, name="Current account", institution_name="Example Bank")
    names = [a["name"] for a in (await client.get("/api/v1/finance/accounts")).json()]
    assert "Current account" in names


async def test_amounts_must_be_strings(client) -> None:
    account_id, gbp_id = await _account(client), await _gbp_id(client)
    response = await client.post(
        f"/api/v1/finance/accounts/{account_id}/snapshots",
        json={"instrument_id": gbp_id, "quantity": 0.1, "as_of": "2026-11-10T12:00:00Z"},
    )
    assert response.status_code == 422


@pytest.mark.parametrize(
    "quantity", ["\u0661\u0662", "1\uff10", "NaN", "Infinity", "1e3", "+1", "12\n"]
)
async def test_amounts_must_be_plain_ascii_decimals(client, quantity) -> None:
    account_id, gbp_id = await _account(client), await _gbp_id(client)
    response = await client.post(
        f"/api/v1/finance/accounts/{account_id}/snapshots",
        json={"instrument_id": gbp_id, "quantity": quantity, "as_of": "2026-11-10T12:00:00Z"},
    )
    assert response.status_code == 422


async def test_extreme_precision_round_trip(client) -> None:
    account_id = await _account(client, subtype="wallet")
    token = (
        await client.post(
            "/api/v1/finance/instruments",
            json={
                "kind": "crypto",
                "symbol": "TKN",
                "name": "Token",
                "decimals": 18,
                "quote_currency": "GBP",
                "identifiers": [],
            },
        )
    ).json()
    await client.post(
        "/api/v1/finance/prices",
        json={
            "instrument_id": token["id"],
            "quote_currency": "GBP",
            "price": "1",
            "as_of_date": "2026-11-10",
        },
    )
    await client.post(
        f"/api/v1/finance/accounts/{account_id}/snapshots",
        json={
            "instrument_id": token["id"],
            "quantity": "123456789012.123456789012345678",
            "as_of": "2026-11-10T12:00:00Z",
        },
    )
    holdings = (await client.get("/api/v1/finance/holdings", params={"date": "2026-11-10"})).json()
    [row] = [h for h in holdings if h["symbol"] == "TKN"]
    assert row["quantity"] == "123456789012.123456789012345678"
    assert row["value"] == "123456789012.123456789012345678"


async def test_unbalanced_transaction_returns_residuals(client) -> None:
    a, b, gbp_id = (
        await _account(client, name="A"),
        await _account(client, name="B"),
        await _gbp_id(client),
    )
    response = await client.post(
        "/api/v1/finance/transactions",
        json={
            "occurred_at": "2026-11-10T12:00:00Z",
            "description": "bad",
            "postings": [
                {"account_id": a, "instrument_id": gbp_id, "quantity": "-100.00"},
                {"account_id": b, "instrument_id": gbp_id, "quantity": "99.99"},
            ],
        },
    )
    assert response.status_code == 422
    assert response.json()["detail"] == {
        "error": "ledger_imbalance",
        "residuals": {gbp_id: "-0.01"},
    }


def test_openapi_export_is_deterministic() -> None:
    first, second = render_openapi(), render_openapi()
    assert first == second
    assert "/api/v1/finance/net-worth" in json.loads(first)["paths"]


async def test_finance_errors_map_to_http_statuses(client) -> None:
    missing = "01900000-0000-7000-8000-000000000000"
    renamed = await client.patch(f"/api/v1/finance/accounts/{missing}", json={"name": "X"})
    assert renamed.status_code == 404
    assert renamed.json()["detail"]["error"] == "account_not_found"

    account_id = await _account(client)
    close = {"closed_on": "2026-11-10"}
    assert (
        await client.post(f"/api/v1/finance/accounts/{account_id}/close", json=close)
    ).is_success
    again = await client.post(f"/api/v1/finance/accounts/{account_id}/close", json=close)
    assert again.status_code == 409
    assert again.json()["detail"]["error"] == "account_already_closed"

    unknown = await client.put("/api/v1/finance/settings", json={"reporting_currency": "ZZZ"})
    assert unknown.status_code == 422
    assert unknown.json()["detail"]["error"] == "unknown_currency"


async def test_reporting_currency_round_trip(client) -> None:
    assert (await client.get("/api/v1/finance/settings")).json() == {"reporting_currency": "GBP"}
    updated = await client.put("/api/v1/finance/settings", json={"reporting_currency": "usd"})
    assert updated.json() == {"reporting_currency": "USD"}
    assert (await client.get("/api/v1/finance/settings")).json() == {"reporting_currency": "USD"}


async def test_net_worth_defaults_to_365_days_up_to_today(client) -> None:
    from datetime import datetime
    from zoneinfo import ZoneInfo

    points = (await client.get("/api/v1/finance/net-worth")).json()
    assert len(points) == 365
    assert points[-1]["day"] == datetime.now(ZoneInfo("Europe/London")).date().isoformat()


def test_openapi_operation_ids_and_decimal_schema() -> None:
    document = json.loads(render_openapi())
    operation_ids = {
        op["operationId"] for path in document["paths"].values() for op in path.values()
    }
    assert {"create_account", "record_snapshot", "get_net_worth", "health"} <= operation_ids
    quantity = document["components"]["schemas"]["SnapshotCreate"]["properties"]["quantity"]
    assert (quantity["type"], quantity["pattern"]) == ("string", r"^-?[0-9]+(\.[0-9]+)?$")
