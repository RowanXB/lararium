"""The import routes, end to end with the synthetic Example Bank files."""

import json
from decimal import Decimal
from importlib.resources import files
from typing import Any

import httpx

from lararium.api.export_openapi import render_openapi
from lararium.modules.finance.importing import parsed
from lararium.modules.finance.importing.staging import NO_BALANCES_WARNING

EXAMPLES = files("lararium.modules.finance.importing") / "examples"
API = "/api/v1/finance"
# After every example file's dates, so holdings include all of them.
LATER = "2026-12-31"
QIF_CONFIG = {"format": "qif", "date_format": "%d/%m/%Y"}


def _example(name: str) -> bytes:
    return (EXAMPLES / name).read_bytes()


def _example_config(name: str) -> dict[str, Any]:
    return json.loads((EXAMPLES / name).read_text())


async def _gbp_id(client: httpx.AsyncClient) -> str:
    instruments = (await client.get(f"{API}/instruments")).json()
    return next(i["id"] for i in instruments if i["symbol"] == "GBP")


async def _account(client: httpx.AsyncClient, balance_mode: str = "transactions") -> str:
    body = {
        "name": f"Example Bank {balance_mode}",
        "ledger_type": "asset",
        "subtype": "bank",
        "balance_mode": balance_mode,
    }
    response = await client.post(f"{API}/accounts", json=body)
    assert response.status_code == 201, response.text
    return response.json()["id"]


async def _profile(client: httpx.AsyncClient, name: str, config: dict[str, Any]) -> str:
    response = await client.post(f"{API}/import-profiles", json={"name": name, "config": config})
    assert response.status_code == 201, response.text
    return response.json()["id"]


async def _upload(
    client: httpx.AsyncClient,
    account_id: str,
    profile_id: str,
    content: bytes,
    filename: str,
    instrument_id: str | None = None,
    headers: dict[str, str] | None = None,
) -> httpx.Response:
    data = {"account_id": account_id, "profile_id": profile_id}
    if instrument_id is not None:
        data["instrument_id"] = instrument_id
    return await client.post(
        f"{API}/imports",
        data=data,
        files={"file": (filename, content, "application/octet-stream")},
        headers=headers,
    )


async def _balance(client: httpx.AsyncClient, account_id: str) -> Decimal:
    holdings = (await client.get(f"{API}/holdings", params={"date": LATER})).json()
    return sum(
        (Decimal(h["quantity"]) for h in holdings if h["account_id"] == account_id), Decimal(0)
    )


async def _round_trip(
    client: httpx.AsyncClient, config: dict[str, Any], filename: str
) -> tuple[str, dict[str, Any]]:
    """Stage the example file into a fresh account and commit it; (account id, staged batch)."""
    gbp_id = await _gbp_id(client)
    account_id = await _account(client)
    profile_id = await _profile(client, f"Example Bank {filename}", config)
    staged = await _upload(client, account_id, profile_id, _example(filename), filename, gbp_id)
    assert staged.status_code == 201, staged.text
    batch = staged.json()
    committed = await client.post(f"{API}/imports/{batch['id']}/commit")
    assert committed.status_code == 200, committed.text
    assert committed.json()["status"] == "committed"

    again = await _upload(client, account_id, profile_id, _example(filename), filename, gbp_id)
    assert again.status_code == 201, again.text
    statement_rows = [row for row in again.json()["rows"] if row["kind"] == "statement"]
    assert {row["status"] for row in statement_rows} <= {"duplicate", "skipped"}
    assert again.json()["counts"]["new"] == 0
    # Each duplicate names the committed row it duplicates.
    committed_ids = {row["id"] for row in batch["rows"]}
    duplicates = [row for row in statement_rows if row["status"] == "duplicate"]
    assert duplicates
    assert all(row["duplicate_of"] in committed_ids for row in duplicates)
    assert again.json()["counts"]["duplicate"] == batch["counts"]["new"] - _adjustments(batch)
    return account_id, batch


def _adjustments(batch: dict[str, Any]) -> int:
    return sum(1 for row in batch["rows"] if row["kind"] == "opening_adjustment")


async def test_example_csv_end_to_end(client) -> None:
    account_id, batch = await _round_trip(
        client, _example_config("example-bank-csv.json"), "example-bank.csv"
    )
    lines = _example("example-bank.csv").decode().splitlines()[1:]
    zero_rows = [line for line in lines if ",0.00," in line]
    assert len(zero_rows) == 1
    # One row per non-zero line, plus the opening adjustment: the file opens at 1,000.00.
    assert batch["counts"]["new"] == len(lines) - len(zero_rows) + 1
    assert batch["counts"]["skipped"] == 1
    [adjustment] = [row for row in batch["rows"] if row["kind"] == "opening_adjustment"]
    # An export's first day is partial, so its adjustment is dated on it.
    assert (adjustment["quantity"], adjustment["booking_date"]) == ("1000.00", "2026-10-01")
    assert await _balance(client, account_id) == Decimal("1676.37")  # the last Balance
    # Date-only rows occur at local midnight of their date (Decision 6).
    assert all(
        row["occurred_at"] is not None for row in batch["rows"] if row["kind"] == "statement"
    )


async def test_example_ofx_end_to_end(client) -> None:
    account_id, batch = await _round_trip(
        client, _example_config("example-bank-ofx.json"), "example-bank.ofx"
    )
    statement_rows = [row for row in batch["rows"] if row["kind"] == "statement"]
    assert len(statement_rows) == 5
    assert all(row["external_id"] for row in statement_rows)
    assert batch["statement"]["closing_balance"] == "2228.80"
    assert await _balance(client, account_id) == Decimal("2228.80")  # LEDGERBAL


async def test_example_qif_end_to_end(client) -> None:
    account_id, batch = await _round_trip(client, QIF_CONFIG, "example-bank.qif")
    assert batch["counts"]["new"] == 4
    assert batch["warnings"] == [NO_BALANCES_WARNING]
    assert await _balance(client, account_id) == Decimal("1232.30")  # the rows' sum


async def test_rollback_via_api(client) -> None:
    gbp_id = await _gbp_id(client)
    account_id = await _account(client)
    profile_id = await _profile(client, "Example Bank", _example_config("example-bank-csv.json"))
    batch = (
        await _upload(client, account_id, profile_id, _example("example-bank.csv"), "x.csv", gbp_id)
    ).json()
    assert (await client.post(f"{API}/imports/{batch['id']}/commit")).status_code == 200
    assert await _balance(client, account_id) == Decimal("1676.37")

    response = await client.post(f"{API}/imports/{batch['id']}/rollback")
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "rolled_back"
    assert response.json()["rolled_back_at"] is not None
    assert await _balance(client, account_id) == 0
    detail = (await client.get(f"{API}/imports/{batch['id']}")).json()
    assert detail["status"] == "rolled_back"
    listed = (await client.get(f"{API}/imports")).json()
    assert batch["id"] in {b["id"] for b in listed}


async def test_imported_transaction_is_voided_only_by_rollback(client) -> None:
    """Third review: a hand void left the import's row 'committed', so its openings never
    re-aligned and later statements were refused as disagreeing."""
    gbp_id = await _gbp_id(client)
    account_id = await _account(client)
    profile_id = await _profile(client, "Example Bank", _example_config("example-bank-csv.json"))
    batch = (
        await _upload(client, account_id, profile_id, _example("example-bank.csv"), "x.csv", gbp_id)
    ).json()
    assert (await client.post(f"{API}/imports/{batch['id']}/commit")).status_code == 200
    rows = (await client.get(f"{API}/imports/{batch['id']}")).json()["rows"]
    posted = [row["transaction_id"] for row in rows if row["transaction_id"] is not None]
    assert posted

    for transaction_id in (posted[0], posted[-1]):  # an adjustment and a statement row
        response = await client.post(f"{API}/transactions/{transaction_id}/void")
        assert response.status_code == 409, response.text
        assert response.json()["detail"]["error"] == "transaction_imported"
        assert "x.csv" in response.json()["detail"]["message"]
    assert await _balance(client, account_id) == Decimal("1676.37")

    assert (await client.post(f"{API}/imports/{batch['id']}/rollback")).status_code == 200
    assert await _balance(client, account_id) == 0


async def test_discard_and_state_errors(client) -> None:
    account_id = await _account(client)
    profile_id = await _profile(client, "Example Bank QIF", QIF_CONFIG)
    batch = (
        await _upload(
            client,
            account_id,
            profile_id,
            _example("example-bank.qif"),
            "x.qif",
            await _gbp_id(client),
        )
    ).json()
    discarded = await client.post(f"{API}/imports/{batch['id']}/discard")
    assert (discarded.status_code, discarded.json()["status"]) == (200, "discarded")
    response = await client.post(f"{API}/imports/{batch['id']}/commit")
    assert (response.status_code, response.json()["detail"]["error"]) == (409, "import_batch_state")


async def test_upload_too_large_is_413(client) -> None:
    account_id = await _account(client)
    profile_id = await _profile(client, "Example Bank QIF", QIF_CONFIG)
    content = b"x" * (parsed.MAX_IMPORT_BYTES + 1)
    response = await _upload(client, account_id, profile_id, content, "big.qif")
    assert response.status_code == 413
    assert response.json()["detail"]["error"] == "file_too_large"
    assert (await client.get(f"{API}/imports")).json() == []


async def test_empty_upload_is_422(client) -> None:
    account_id = await _account(client)
    profile_id = await _profile(client, "Example Bank QIF", QIF_CONFIG)
    response = await _upload(client, account_id, profile_id, b"", "empty.qif")
    assert response.status_code == 422
    assert response.json()["detail"]["error"] == "file_format"


async def test_header_only_csv_is_422_empty_statement(client) -> None:
    account_id = await _account(client)
    config = {
        "format": "csv",
        "columns": {"date": "Date", "description": ["Description"], "amount": "Amount"},
        "date_format": "%d/%m/%Y",
    }
    profile_id = await _profile(client, "Example Bank CSV", config)
    response = await _upload(client, account_id, profile_id, b"Date,Description,Amount\n", "e.csv")
    assert response.status_code == 422
    assert response.json()["detail"] == {
        "error": "empty_statement",
        "message": "the file lists no transactions: there is nothing to import",
    }
    assert (await client.get(f"{API}/imports")).json() == []


async def test_unreadable_file_is_422(client) -> None:
    account_id = await _account(client)
    profile_id = await _profile(client, "Example Bank QIF", QIF_CONFIG)
    response = await _upload(client, account_id, profile_id, b"not a statement\n", "x.qif")
    assert response.status_code == 422
    assert response.json()["detail"]["error"] == "file_format"


async def test_upload_to_snapshots_account_is_422(client) -> None:
    account_id = await _account(client, balance_mode="snapshots")
    profile_id = await _profile(client, "Example Bank QIF", QIF_CONFIG)
    response = await _upload(client, account_id, profile_id, _example("example-bank.qif"), "x.qif")
    assert response.status_code == 422
    assert response.json()["detail"]["error"] == "import_not_allowed"


async def test_unknown_ids_are_404(client) -> None:
    missing = "01900000-0000-7000-8000-000000000000"
    account_id = await _account(client)
    response = await _upload(client, account_id, missing, b"!Type:Bank\n", "x.qif")
    assert (response.status_code, response.json()["detail"]["error"]) == (
        404,
        "import_profile_not_found",
    )
    response = await client.get(f"{API}/imports/{missing}")
    assert (response.status_code, response.json()["detail"]["error"]) == (
        404,
        "import_batch_not_found",
    )
    response = await client.put(
        f"{API}/import-profiles/{missing}", json={"name": "X", "config": QIF_CONFIG}
    )
    assert (response.status_code, response.json()["detail"]["error"]) == (
        404,
        "import_profile_not_found",
    )


async def test_profile_config_validation_is_422(client) -> None:
    bad = {"format": "csv", "columns": {"date": "Date", "description": ["D"]}, "date_format": "%d"}
    response = await client.post(f"{API}/import-profiles", json={"name": "Bad", "config": bad})
    assert response.status_code == 422
    response = await client.post(f"{API}/import-profiles", json={"name": "", "config": QIF_CONFIG})
    assert response.status_code == 422


async def test_duplicate_profile_name_is_409(client) -> None:
    await _profile(client, "Example Bank", QIF_CONFIG)
    response = await client.post(
        f"{API}/import-profiles", json={"name": "example bank", "config": QIF_CONFIG}
    )
    assert response.status_code == 409
    assert response.json()["detail"]["error"] == "duplicate_profile_name"


async def test_update_and_list_profiles(client) -> None:
    profile_id = await _profile(client, "Example Bank", QIF_CONFIG)
    config = QIF_CONFIG | {"date_format": "%Y-%m-%d"}
    response = await client.put(
        f"{API}/import-profiles/{profile_id}", json={"name": "Example Bank QIF", "config": config}
    )
    assert response.status_code == 200, response.text
    assert (response.json()["version"], response.json()["config"]["date_format"]) == (2, "%Y-%m-%d")
    listed = (await client.get(f"{API}/import-profiles")).json()
    assert [p["name"] for p in listed] == ["Example Bank QIF"]


async def test_cross_site_upload_refused(client) -> None:
    account_id = await _account(client)
    profile_id = await _profile(client, "Example Bank QIF", QIF_CONFIG)
    response = await _upload(
        client,
        account_id,
        profile_id,
        _example("example-bank.qif"),
        "x.qif",
        headers={"Sec-Fetch-Site": "cross-site"},
    )
    assert response.status_code == 403
    assert (await client.get(f"{API}/imports")).json() == []


async def test_import_catalog(client) -> None:
    catalog = (await client.get(f"{API}/import-catalog")).json()
    recommended = {(e["institution"], e["format"]) for e in catalog if e["recommended"]}
    assert ("HSBC UK", "pdf") in recommended
    created = await client.post(
        f"{API}/import-profiles", json={"name": "From catalog", "config": catalog[0]["config"]}
    )
    assert created.status_code == 201, created.text


def test_import_operation_ids() -> None:
    document = json.loads(render_openapi())
    operation_ids = {
        op["operationId"] for path in document["paths"].values() for op in path.values()
    }
    assert {
        "list_import_profiles",
        "create_import_profile",
        "update_import_profile",
        "stage_import",
        "list_import_batches",
        "get_import_batch",
        "commit_import",
        "discard_import",
        "rollback_import",
        "get_import_catalog",
        "list_account_types",
    } <= operation_ids
