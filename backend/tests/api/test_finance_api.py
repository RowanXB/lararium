import json

import pytest

from lararium.api.export_openapi import render_openapi
from lararium.modules.finance import errors
from lararium.modules.finance.api import _ERRORS  # pyright: ignore[reportPrivateUsage]


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
    "quantity",
    # The last two have more digits than an amount may have (40 on each side of the point).
    ["\u0661\u0662", "1\uff10", "NaN", "Infinity", "1e3", "+1", "12\n", "1" * 41, "0." + "1" * 41],
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
        "message": "the postings don't balance per instrument",
        "residuals": {gbp_id: "-0.01"},
    }


@pytest.mark.parametrize(
    "half_pair",
    [
        {"cost_per_unit": "1.00"},
        {"cost_instrument_id": "GBP"},
        {"price_per_unit": "1.00"},
        {"price_instrument_id": "GBP"},
    ],
)
async def test_half_specified_cost_or_price_is_rejected_as_422(client, half_pair) -> None:
    a, b, gbp_id = (
        await _account(client, name="A"),
        await _account(client, name="B"),
        await _gbp_id(client),
    )
    extra = {key: gbp_id if value == "GBP" else value for key, value in half_pair.items()}
    response = await client.post(
        "/api/v1/finance/transactions",
        json={
            "occurred_at": "2026-11-10T12:00:00Z",
            "description": "half pair",
            "postings": [
                {"account_id": a, "instrument_id": gbp_id, "quantity": "-1.00"} | extra,
                {"account_id": b, "instrument_id": gbp_id, "quantity": "1.00"},
            ],
        },
    )
    assert response.status_code == 422


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
    assert (quantity["type"], quantity["pattern"]) == (
        "string",
        r"^-?[0-9]{1,40}(\.[0-9]{1,40})?$",
    )


MISSING_ID = "01900000-0000-7000-8000-000000000000"


@pytest.mark.parametrize(
    ("first_extra", "second_extra", "status", "error"),
    [
        # A posting weighed in its own instrument could hide any gap.
        ({"price_per_unit": "2", "price_instrument_id": "GBP"}, {}, 422, "invalid_input"),
        ({"cost_per_unit": "2", "cost_instrument_id": "GBP"}, {}, 422, "invalid_input"),
        ({"quantity": "0"}, {"quantity": "0.00"}, 422, "invalid_input"),
        ({}, {"account_id": MISSING_ID}, 404, "account_not_found"),
        ({"instrument_id": MISSING_ID}, {"instrument_id": MISSING_ID}, 404, "instrument_not_found"),
    ],
)
async def test_invalid_postings_are_client_errors(
    client, first_extra, second_extra, status, error
) -> None:
    a, b, gbp_id = (
        await _account(client, name="A"),
        await _account(client, name="B"),
        await _gbp_id(client),
    )

    def posting(account: str, quantity: str, extra: dict[str, str]) -> dict[str, str]:
        extra = {key: gbp_id if value == "GBP" else value for key, value in extra.items()}
        return {"account_id": account, "instrument_id": gbp_id, "quantity": quantity} | extra

    response = await client.post(
        "/api/v1/finance/transactions",
        json={
            "occurred_at": "2026-11-10T12:00:00Z",
            "description": "invalid",
            "postings": [posting(a, "10", first_extra), posting(b, "-10", second_extra)],
        },
    )
    assert response.status_code == status
    assert response.json()["detail"]["error"] == error


async def test_fiat_without_iso4217_code_is_rejected(client) -> None:
    response = await client.post(
        "/api/v1/finance/instruments",
        json={"kind": "fiat", "symbol": "NGN", "name": "Naira", "decimals": 2},
    )
    assert response.status_code == 422
    assert response.json()["detail"]["error"] == "invalid_input"


@pytest.mark.parametrize(
    "error_class",
    [c for c in _ERRORS if c is not errors.FinanceValidationError],  # that one takes a sentence
)
def test_every_error_message_explains_itself(error_class) -> None:
    # The web UI shows the message as is, so a bare id or code would explain nothing.
    message = str(error_class("XYZ"))
    assert "XYZ" in message
    assert message != "XYZ"


async def test_ledger_imbalance_has_a_message(client) -> None:
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
                {"account_id": a, "instrument_id": gbp_id, "quantity": "-1"},
                {"account_id": b, "instrument_id": gbp_id, "quantity": "2"},
            ],
        },
    )
    assert response.status_code == 422
    assert "don't balance" in response.json()["detail"]["message"]


async def test_net_worth_near_the_first_representable_day(client) -> None:
    response = await client.get("/api/v1/finance/net-worth", params={"end": "0001-06-01"})
    assert response.status_code == 200
    assert response.json()[0]["day"] == "0001-01-01"


@pytest.mark.parametrize(
    "params",
    [
        {"start": "0001-01-01", "end": "2026-11-10"},  # longer than the longest series
        {"start": "2026-11-11", "end": "2026-11-10"},  # ends before it starts
    ],
)
async def test_net_worth_range_out_of_bounds_is_rejected(client, params) -> None:
    response = await client.get("/api/v1/finance/net-worth", params=params)
    assert response.status_code == 422
    assert response.json()["detail"]["error"] == "invalid_input"
