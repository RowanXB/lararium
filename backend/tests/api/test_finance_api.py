import json
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from lararium.api.export_openapi import render_openapi
from lararium.core.connections import create_connection
from lararium.modules.finance import errors
from lararium.modules.finance.accounts import create_account
from lararium.modules.finance.api import _ERRORS  # pyright: ignore[reportPrivateUsage]
from lararium.modules.finance.instruments import get_currency
from lararium.modules.finance.ledger import PostingInput, TransactionInput, post_transaction


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


def test_every_finance_error_has_a_status() -> None:
    # An unmapped one would be a 500. LedgerImbalanceError has its own handler.
    defined = {
        value
        for value in vars(errors).values()
        if isinstance(value, type) and issubclass(value, errors.FinanceError)
    }
    assert defined - {errors.FinanceError, errors.LedgerImbalanceError} == set(_ERRORS)


@pytest.mark.parametrize(
    "error_class",
    # Errors raised with a whole sentence (template "{}") are left out.
    [c for c in _ERRORS if c.template != "{}"],
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


async def test_voiding_a_derived_transaction_is_refused(client, session, run) -> None:
    gbp = await get_currency(session, "GBP")
    a, b = [
        await run(
            create_account, name=name, ledger_type="asset", subtype="bank", balance_mode="hybrid"
        )
        for name in ("A", "B")
    ]
    placeholder = await run(
        post_transaction,
        TransactionInput(
            occurred_at=datetime(2026, 11, 10, 12, tzinfo=UTC),
            description="Transfer from A",
            status="inferred",
            origin="inference",
            postings=[
                PostingInput(a.id, gbp.id, Decimal("-100")),
                PostingInput(b.id, gbp.id, Decimal("100")),
            ],
        ),
    )
    response = await client.post(f"/api/v1/finance/transactions/{placeholder.id}/void")
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert detail["error"] == "invalid_input"
    assert "derived" in detail["message"]


async def test_excluding_an_account_and_cash_flow(client) -> None:
    account_id, gbp_id = await _account(client), await _gbp_id(client)
    snapshot = {"instrument_id": gbp_id, "quantity": "100", "as_of": "2026-11-01T12:00:00Z"}
    await client.post(f"/api/v1/finance/accounts/{account_id}/snapshots", json=snapshot)
    day = {"start": "2026-11-02", "end": "2026-11-02"}
    assert (await client.get("/api/v1/finance/net-worth", params=day)).json()[0]["value"] == "100"

    url = f"/api/v1/finance/accounts/{account_id}/reporting"
    excluded = await client.post(url, json={"excluded": True})
    assert excluded.json()["excluded_from_reports"] is True
    assert (await client.get("/api/v1/finance/net-worth", params=day)).json()[0]["value"] == "0"
    assert (
        await client.get("/api/v1/finance/holdings", params={"date": "2026-11-02"})
    ).json() == []
    # The Accounts page still shows a hidden account's balance.
    every = {"date": "2026-11-02", "all_accounts": "true"}
    shown = (await client.get("/api/v1/finance/holdings", params=every)).json()
    assert [h["value"] for h in shown] == ["100"]
    listed = (await client.get("/api/v1/finance/accounts")).json()
    assert [a["excluded_from_reports"] for a in listed if a["id"] == account_id] == [True]

    assert (await client.post(url, json={"excluded": False})).json()[
        "excluded_from_reports"
    ] is False
    assert (await client.get("/api/v1/finance/net-worth", params=day)).json()[0]["value"] == "100"

    flow = (await client.get("/api/v1/finance/cash-flow", params=day | {"bucket": "day"})).json()
    assert flow == {
        "start": "2026-11-02",
        "end": "2026-11-02",
        "bucket": "day",
        "income": "0",
        "spending": "0",
        "net": "0",
        "missing_count": 0,
        "periods": [{"start": "2026-11-02", "income": "0", "spending": "0"}],
        "categories": [],
        "payees": [],
        "hidden_accounts": [],
    }
    bad = await client.get("/api/v1/finance/cash-flow", params={"bucket": "year"})
    assert bad.status_code == 422


# Account types

API = "/api/v1/finance"
_CASH_ISA = {
    "name": "Cash ISA",
    "ledger_type": "asset",
    "subtype": "savings_pot",
    "balance_mode": "transactions",
    "tax_wrapper": "isa",
    "type_key": "trading212.cash_isa",
}


async def test_account_types_lists_the_catalog(client) -> None:
    types = (await client.get(f"{API}/account-types")).json()
    assert [t["key"] for t in types][:6] == [
        "monzo",
        "ibkr",
        "etoro",
        "trading212.invest",
        "trading212.stocks_isa",
        "trading212.cash_isa",
    ]
    cash = types[5]
    assert cash["api"] is None and cash["family"] == "trading212"
    assert cash["name"] == "Trading 212 Cash ISA"
    assert cash["shape"] == {
        "ledger_type": "asset",
        "subtype": "savings_pot",
        "tax_wrapper": "isa",
        "balance_mode": "transactions",
    }
    assert cash["funding"] == ["bank_transfer"]
    assert [f["label"] for f in cash["files"]] == ["History CSV"]
    assert cash["files"][0]["config"]["format"] == "csv"
    invest = types[3]
    assert invest["api"] == {"connector": "trading212", "config": {"account_kind": "invest"}}
    assert invest["funding"] == ["bank_transfer", "card"]
    assert invest["shape"] is None and invest["files"][0]["config"] is None


async def test_create_from_a_type(client) -> None:
    response = await client.post(f"{API}/accounts", json=_CASH_ISA)
    assert response.status_code == 201
    created = response.json()
    assert created["type_key"] == "trading212.cash_isa"
    plain = await _account(client, name="Other", institution_name="Trading 212")
    listed = {a["id"]: a for a in (await client.get(f"{API}/accounts")).json()}
    assert created["institution_entity_id"] == listed[plain]["institution_entity_id"]
    assert listed[plain]["type_key"] is None


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("subtype", "bank"),
        ("ledger_type", "liability"),
        ("tax_wrapper", "none"),
        ("balance_mode", "hybrid"),
        ("institution_name", "Trading 212"),
    ],
)
async def test_create_from_a_type_refuses_a_disagreeing_shape(client, field, value) -> None:
    before = len((await client.get(f"{API}/accounts")).json())
    response = await client.post(f"{API}/accounts", json=_CASH_ISA | {field: value})
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert detail["error"] == "type_mismatch"
    assert field in detail["message"] and "Trading 212 Cash ISA" in detail["message"]
    assert len((await client.get(f"{API}/accounts")).json()) == before


async def test_create_from_a_statement_type_takes_either_side(client) -> None:
    body = {
        "name": "Credit card",
        "ledger_type": "liability",
        "subtype": "bank",
        "balance_mode": "transactions",
        "type_key": "hsbc_uk",
    }
    response = await client.post(f"{API}/accounts", json=body)
    assert response.status_code == 201
    assert response.json()["ledger_type"] == "liability"
    assert response.json()["type_key"] == "hsbc_uk"


async def test_create_refuses_an_api_type_and_an_unknown_one(client) -> None:
    before = len((await client.get(f"{API}/accounts")).json())
    api_type = await client.post(
        f"{API}/accounts", json=_CASH_ISA | {"type_key": "trading212.invest"}
    )
    assert api_type.status_code == 422
    assert api_type.json()["detail"]["error"] == "type_not_creatable"
    assert "Trading 212 Invest" in api_type.json()["detail"]["message"]
    unknown = await client.post(f"{API}/accounts", json=_CASH_ISA | {"type_key": "nope"})
    assert unknown.status_code == 422
    assert unknown.json()["detail"]["error"] == "unknown_type"
    assert "nope" not in unknown.json()["detail"]["message"]
    assert len((await client.get(f"{API}/accounts")).json()) == before


async def test_patch_sets_the_type_of_an_importable_account(client) -> None:
    account_id = await _account(client, name="Old current", balance_mode="transactions")
    response = await client.patch(f"{API}/accounts/{account_id}", json={"type_key": "lloyds"})
    assert response.status_code == 200
    assert response.json()["type_key"] == "lloyds"
    assert response.json()["name"] == "Old current"


async def test_patch_refuses_what_cannot_take_imports(client, run) -> None:
    snapshots = await _account(client)  # balance from snapshots
    refused = await client.patch(f"{API}/accounts/{snapshots}", json={"type_key": "lloyds"})
    assert refused.status_code == 422
    assert refused.json()["detail"]["error"] == "import_not_allowed"

    connection = await run(create_connection, connector="etoro", display_name="eToro", config={})
    connected = await run(
        create_account,
        name="eToro",
        ledger_type="asset",
        subtype="broker",
        balance_mode="transactions",
        source_connection_id=connection.id,
    )
    refused = await client.patch(f"{API}/accounts/{connected.id}", json={"type_key": "lloyds"})
    assert refused.status_code == 422
    assert refused.json()["detail"]["error"] == "import_not_allowed"

    account_id = await _account(client, name="Current", balance_mode="transactions")
    url = f"{API}/accounts/{account_id}"
    api_type = await client.patch(url, json={"type_key": "trading212.invest"})
    assert api_type.status_code == 422
    assert api_type.json()["detail"]["error"] == "type_not_creatable"
    unknown = await client.patch(url, json={"type_key": "nope"})
    assert unknown.status_code == 422
    assert unknown.json()["detail"]["error"] == "unknown_type"
    assert (await client.patch(url, json={})).status_code == 422
    # A rename sent with a refused type change does not happen either.
    both = await client.patch(url, json={"name": "Renamed", "type_key": "nope"})
    assert both.status_code == 422
    listed = {a["id"]: a for a in (await client.get(f"{API}/accounts")).json()}
    assert listed[account_id]["name"] == "Current"
    assert listed[account_id]["type_key"] is None

    # The type must agree with the account's shape: a bank account is no Cash ISA.
    mismatch = await client.patch(url, json={"type_key": "trading212.cash_isa"})
    assert mismatch.status_code == 422
    detail = mismatch.json()["detail"]
    assert detail["error"] == "type_mismatch"
    assert "subtype" in detail["message"] and "Trading 212 Cash ISA" in detail["message"]
    listed = {a["id"]: a for a in (await client.get(f"{API}/accounts")).json()}
    assert listed[account_id]["type_key"] is None

    typed = (await client.post(f"{API}/accounts", json=_CASH_ISA)).json()["id"]
    cleared = await client.patch(f"{API}/accounts/{typed}", json={"type_key": None})
    assert cleared.status_code == 200
    assert cleared.json()["type_key"] is None


async def test_patch_refuses_to_clear_a_connected_accounts_type(client, run) -> None:
    connection = await run(
        create_connection, connector="trading212", display_name="Trading 212", config={}
    )
    connected = await run(
        create_account,
        name="Invest",
        ledger_type="asset",
        subtype="broker",
        balance_mode="transactions",
        source_connection_id=connection.id,
        type_key="trading212.invest",
    )
    refused = await client.patch(f"{API}/accounts/{connected.id}", json={"type_key": None})
    assert refused.status_code == 422
    assert refused.json()["detail"]["error"] == "import_not_allowed"
    listed = {a["id"]: a for a in (await client.get(f"{API}/accounts")).json()}
    assert listed[str(connected.id)]["type_key"] == "trading212.invest"


async def test_patch_refuses_a_type_on_the_wrong_side(client) -> None:
    body = {"name": "Card", "ledger_type": "liability", "subtype": "savings_pot"}
    account_id = await _account(client, **body, balance_mode="transactions", tax_wrapper="isa")
    url = f"{API}/accounts/{account_id}"
    refused = await client.patch(url, json={"type_key": "trading212.cash_isa"})
    assert refused.status_code == 422
    assert refused.json()["detail"]["error"] == "type_mismatch"
    assert "ledger_type" in refused.json()["detail"]["message"]


async def test_rename_still_works(client) -> None:
    typed = (await client.post(f"{API}/accounts", json=_CASH_ISA)).json()["id"]
    response = await client.patch(f"{API}/accounts/{typed}", json={"name": "ISA"})
    assert response.status_code == 200
    assert response.json()["name"] == "ISA"
    assert response.json()["type_key"] == "trading212.cash_isa"
    both = await client.patch(f"{API}/accounts/{typed}", json={"name": "ISA 2", "type_key": None})
    assert both.json()["name"] == "ISA 2" and both.json()["type_key"] is None


async def test_a_type_key_no_longer_in_the_catalog_reads_as_none(client, run) -> None:
    record = await run(
        create_account,
        name="Gone",
        ledger_type="asset",
        subtype="bank",
        balance_mode="transactions",
        type_key="retired.type",
    )
    listed = {a["id"]: a for a in (await client.get(f"{API}/accounts")).json()}
    assert listed[str(record.id)]["type_key"] is None
