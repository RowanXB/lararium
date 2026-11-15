"""The transfers and identifiers API (/api/v1/finance/transfers, .../identifiers)."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text

from lararium.core.connections import create_connection
from lararium.core.settings_store import set_setting
from lararium.db import create_engine
from lararium.modules.finance import transfers_api
from lararium.modules.finance.accounts import AccountRecord, create_account
from lararium.modules.finance.identifiers import get_or_create_identifier
from lararium.modules.finance.instruments import get_currency
from lararium.modules.finance.ledger import (
    CounterpartyFields,
    PostingInput,
    TransactionInput,
    TransactionRecord,
    upsert_connector_transaction,
)
from lararium.modules.finance.system_accounts import get_or_create_system_account
from lararium.modules.finance.transfers import transfer_contra

FINANCE = "/api/v1/finance"
WHEN = datetime(2026, 11, 2, 12, tzinfo=UTC)
# Invented bank details.
A_IDENTIFIER = "445566:11112222"
B_IDENTIFIER = "112233:12345678"
C_IDENTIFIER = "778899:33334444"


@pytest.fixture(autouse=True)
async def system_accounts(run) -> None:
    # transfer_contra needs them, as a sync's normalizer creates them first.
    for key in ("unassigned", "in_transit", "opening_balances"):
        await run(get_or_create_system_account, key)


async def _connected(run, name: str) -> tuple[UUID, AccountRecord]:
    connection = await run(create_connection, connector="monzo", display_name=name, config={})
    account = await run(
        create_account,
        name=name,
        ledger_type="asset",
        subtype="bank",
        balance_mode="transactions",
        source_connection_id=connection.id,
    )
    return connection.id, account


async def _manual(client, name: str = "B", balance_mode: str = "hybrid") -> str:
    body = {"name": name, "ledger_type": "asset", "subtype": "bank", "balance_mode": balance_mode}
    response = await client.post(f"{FINANCE}/accounts", json=body)
    assert response.status_code == 201
    return response.json()["id"]


async def _connector_tx(
    session,
    run,
    bank: tuple[UUID, AccountRecord],
    quantity: str,
    *,
    external_id: str = "tx_1",
    occurred_at: datetime = WHEN,
    identifier_id: UUID | None = None,
    name: str = "Test Owner",
) -> TransactionRecord:
    """Post a connector transaction as a normalizer would (contra from transfer_contra)."""
    connection_id, account = bank
    gbp = await get_currency(session, "GBP")
    contra = await transfer_contra(
        session,
        source=("connection", f"{connection_id}:{external_id}"),
        own_account_id=account.id,
        counterparty_identifier_id=identifier_id,
    )
    amount = Decimal(quantity)
    record, _ = await run(
        upsert_connector_transaction,
        TransactionInput(
            occurred_at=occurred_at,
            description="Transfer",
            status="booked",
            origin="connector",
            connection_id=connection_id,
            external_id=external_id,
            postings=[
                PostingInput(account.id, gbp.id, amount),
                PostingInput(contra, gbp.id, -amount),
            ],
            counterparty=CounterpartyFields(
                identifier_id=identifier_id, name=name, bank_transfer=True
            ),
        ),
    )
    return record


async def _register(client, account_id: str, value: str) -> Any:
    return await client.post(
        f"{FINANCE}/accounts/{account_id}/identifiers",
        json={"kind": "uk_bank_account", "value": value},
    )


async def _transfers(client, **params: Any) -> list[dict[str, Any]]:
    response = await client.get(f"{FINANCE}/transfers", params=params)
    assert response.status_code == 200
    return response.json()


async def _placeholders(session) -> list[Any]:
    rows = await session.execute(
        text("select id from finance.transactions where origin = 'inference' and status <> 'void'")
    )
    return list(rows)


@pytest.fixture
async def inferred(client, session, run) -> tuple[AccountRecord, str, TransactionRecord]:
    """A's payment of 100 to the manual account B, matched by registering B's identifier."""
    a = await _connected(run, "A")
    b_id = await _manual(client, "B")
    identifier = await run(get_or_create_identifier, "uk_bank_account", B_IDENTIFIER)
    record = await _connector_tx(session, run, a, "-100", identifier_id=identifier)
    assert (await _register(client, b_id, "11-22-33 12345678")).status_code == 201
    return a[1], b_id, record


async def test_register_identifier_matches_history(client, session, inferred) -> None:
    a, b_id, record = inferred

    [transfer] = await _transfers(client)

    assert transfer["status"] == "inferred"
    assert transfer["review_reason"] is None
    assert (transfer["from_account_id"], transfer["from_account_name"]) == (str(a.id), "A")
    assert (transfer["to_account_id"], transfer["to_account_name"]) == (b_id, "B")
    assert (transfer["amount"], transfer["currency"]) == ("100", "GBP")
    assert transfer["out_transaction_id"] == str(record.id)
    assert transfer["in_transaction_id"] is None
    [placeholder] = await _placeholders(session)
    assert transfer["placeholder_transaction_id"] == str(placeholder.id)
    assert (transfer["days_in_transit"], transfer["overdue"]) == (None, False)

    [identifier] = (await client.get(f"{FINANCE}/accounts/{b_id}/identifiers")).json()
    assert identifier["value"] == B_IDENTIFIER
    assert (identifier["sort_code"], identifier["account_number"]) == ("112233", "12345678")
    assert identifier["account_id"] == b_id

    assert await _transfers(client, status="matched") == []
    assert len(await _transfers(client, status=["inferred", "matched"])) == 1


async def test_remove_identifier_unmatches(client, session, inferred) -> None:
    _, b_id, _ = inferred
    [identifier] = (await client.get(f"{FINANCE}/accounts/{b_id}/identifiers")).json()

    response = await client.delete(f"{FINANCE}/accounts/{b_id}/identifiers/{identifier['id']}")

    assert response.status_code == 204
    assert (await client.get(f"{FINANCE}/accounts/{b_id}/identifiers")).json() == []
    assert await _transfers(client) == []
    assert await _placeholders(session) == []
    # The registry row stays, as transactions name it, so it is a suggestion again.
    [suggestion] = (await client.get(f"{FINANCE}/transfer-suggestions")).json()
    assert suggestion["identifier_id"] == identifier["id"]
    # It is no longer on B.
    again = await client.delete(f"{FINANCE}/accounts/{b_id}/identifiers/{identifier['id']}")
    assert again.status_code == 404
    assert again.json()["detail"]["error"] == "account_identifier_not_found"


async def test_snapshots_account_identifier_422_with_message(client) -> None:
    account_id = await _manual(client, "Savings", balance_mode="snapshots")

    response = await _register(client, account_id, B_IDENTIFIER)

    assert response.status_code == 422
    detail = response.json()["detail"]
    assert detail["error"] == "invalid_input"
    assert "switch the account to hybrid" in detail["message"]

    switched = await client.post(
        f"{FINANCE}/accounts/{account_id}/balance-mode", json={"balance_mode": "hybrid"}
    )
    assert switched.status_code == 200
    assert switched.json()["balance_mode"] == "hybrid"
    assert (await _register(client, account_id, B_IDENTIFIER)).status_code == 201

    back = await client.post(
        f"{FINANCE}/accounts/{account_id}/balance-mode", json={"balance_mode": "snapshots"}
    )
    assert back.status_code == 422


async def test_duplicate_identifier_409(client) -> None:
    first, second = await _manual(client, "First"), await _manual(client, "Second")
    assert (await _register(client, first, B_IDENTIFIER)).status_code == 201
    # Registering it again on the same account is a no-op.
    assert (await _register(client, first, "112233/12345678")).status_code == 201

    response = await _register(client, second, B_IDENTIFIER)

    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail["error"] == "duplicate_account_identifier"
    assert "First" in detail["message"]


async def test_reject_and_unreject_round_trip(client, session, inferred) -> None:
    _, _, record = inferred
    [transfer] = await _transfers(client)

    rejected = await client.post(f"{FINANCE}/transfers/{transfer['id']}/reject")

    assert rejected.status_code == 200
    body = rejected.json()
    assert (body["id"], body["status"]) == (transfer["id"], "rejected")
    assert (body["out_transaction_id"], body["placeholder_transaction_id"]) == (
        str(record.id),
        None,
    )
    assert await _placeholders(session) == []
    again = await client.post(f"{FINANCE}/transfers/{transfer['id']}/reject")
    assert again.status_code == 422

    unrejected = await client.post(f"{FINANCE}/transfers/{transfer['id']}/unreject")

    assert unrejected.status_code == 204
    [restored] = await _transfers(client)
    assert (restored["status"], restored["out_transaction_id"]) == ("inferred", str(record.id))
    assert len(await _placeholders(session)) == 1
    # The transfer was reopened under a new id; the old one is gone.
    gone = await client.post(f"{FINANCE}/transfers/{transfer['id']}/unreject")
    assert gone.status_code == 404


async def test_pair_candidate_flow(client, session, run) -> None:
    a, c = await _connected(run, "A"), await _connected(run, "C")
    c_identifier = await run(get_or_create_identifier, "uk_bank_account", C_IDENTIFIER)
    assert (await _register(client, str(c[1].id), C_IDENTIFIER)).status_code == 201
    await _connector_tx(
        session, run, c, "-5", external_id="card_1", occurred_at=WHEN - timedelta(days=10)
    )
    loose = [
        await _connector_tx(
            session, run, c, "100", external_id=f"in_{n}", occurred_at=WHEN + timedelta(hours=n)
        )
        for n in (1, 2)
    ]
    out_record = await _connector_tx(
        session, run, a, "-100", external_id="out_1", identifier_id=c_identifier
    )
    # Heal runs in every mutating request; registering A's identifier is one.
    assert (await _register(client, str(a[1].id), A_IDENTIFIER)).status_code == 201
    [transfer] = await _transfers(client)
    assert (transfer["status"], transfer["review_reason"]) == ("awaiting", "several_candidates")

    response = await client.get(f"{FINANCE}/transfers/{transfer['id']}/candidates")

    assert response.status_code == 200
    found = response.json()
    assert [(c["transaction_id"], c["reason"]) for c in found] == [
        (str(loose[0].id), "exact"),
        (str(loose[1].id), "exact"),
    ]
    assert (found[0]["account_name"], found[0]["quantity"]) == ("C", "100")

    paired = await client.post(
        f"{FINANCE}/transfers/{transfer['id']}/pair", json={"transaction_id": str(loose[0].id)}
    )

    assert paired.status_code == 200
    body = paired.json()
    assert (body["status"], body["review_reason"]) == ("matched", None)
    assert (body["out_transaction_id"], body["in_transaction_id"]) == (
        str(out_record.id),
        str(loose[0].id),
    )
    assert await _transfers(client) == [body]

    # Unpaired, A's payment is never paired with that deposit again, which leaves the other.
    unpaired = await client.post(f"{FINANCE}/transfers/{body['id']}/unpair")
    assert unpaired.status_code == 204
    [rematched] = await _transfers(client)
    assert (rematched["status"], rematched["in_transaction_id"]) == ("matched", str(loose[1].id))


async def test_unpair_is_undone_by_pair(client, session, run) -> None:
    a, c = await _connected(run, "A"), await _connected(run, "C")
    a_identifier = await run(get_or_create_identifier, "uk_bank_account", A_IDENTIFIER)
    c_identifier = await run(get_or_create_identifier, "uk_bank_account", C_IDENTIFIER)
    out_record = await _connector_tx(
        session, run, a, "-100", external_id="out_1", identifier_id=c_identifier
    )
    in_record = await _connector_tx(
        session,
        run,
        c,
        "100",
        external_id="in_1",
        occurred_at=WHEN + timedelta(hours=1),
        identifier_id=a_identifier,
    )
    assert (await _register(client, str(a[1].id), A_IDENTIFIER)).status_code == 201
    assert (await _register(client, str(c[1].id), C_IDENTIFIER)).status_code == 201
    [matched] = await _transfers(client)
    assert matched["status"] == "matched"
    assert (await client.post(f"{FINANCE}/transfers/{matched['id']}/unpair")).status_code == 204
    [waiting] = [t for t in await _transfers(client) if t["out_transaction_id"]]

    found = (await client.get(f"{FINANCE}/transfers/{waiting['id']}/candidates")).json()
    assert [(x["transaction_id"], x["reason"]) for x in found] == [(str(in_record.id), "unpaired")]
    paired = await client.post(
        f"{FINANCE}/transfers/{waiting['id']}/pair", json={"transaction_id": str(in_record.id)}
    )

    assert paired.status_code == 200
    body = paired.json()
    assert (body["status"], body["out_transaction_id"], body["in_transaction_id"]) == (
        "matched",
        str(out_record.id),
        str(in_record.id),
    )
    assert await _transfers(client) == [body]


async def test_pair_refuses_a_non_candidate(client, session, run, inferred) -> None:
    [transfer] = await _transfers(client)
    response = await client.post(
        f"{FINANCE}/transfers/{transfer['id']}/pair", json={"transaction_id": str(uuid4())}
    )
    assert response.status_code == 422
    assert response.json()["detail"]["error"] == "invalid_input"


async def test_suggestions_group_and_dismiss(client, session, run) -> None:
    a = await _connected(run, "A")
    busy = await run(get_or_create_identifier, "uk_bank_account", B_IDENTIFIER)
    quiet = await run(get_or_create_identifier, "uk_bank_account", C_IDENTIFIER)
    await run(get_or_create_identifier, "uk_bank_account", A_IDENTIFIER)  # named by nothing
    for n, (quantity, name) in enumerate([("-10", "Test Owner"), ("-20", "T Owner"), ("30", "")]):
        await _connector_tx(
            session,
            run,
            a,
            quantity,
            external_id=f"busy_{n}",
            occurred_at=WHEN - timedelta(days=n),
            identifier_id=busy,
            name=name,
        )
    await _connector_tx(
        session,
        run,
        a,
        "-5",
        external_id="quiet",
        occurred_at=WHEN + timedelta(days=3),
        identifier_id=quiet,
    )

    response = await client.get(f"{FINANCE}/transfer-suggestions")

    assert response.status_code == 200
    assert response.json() == [
        {
            "identifier_id": str(busy),
            "sort_code": "112233",
            "account_number": "12345678",
            "names": ["T Owner", "Test Owner"],
            "outgoing": 2,
            "incoming": 1,
            "latest": "2026-11-02",
        },
        {
            "identifier_id": str(quiet),
            "sort_code": "778899",
            "account_number": "33334444",
            "names": ["Test Owner"],
            "outgoing": 1,
            "incoming": 0,
            "latest": "2026-11-05",
        },
    ]

    dismissed = await client.post(f"{FINANCE}/transfer-suggestions/{busy}/dismiss")

    assert dismissed.status_code == 204
    [left] = (await client.get(f"{FINANCE}/transfer-suggestions")).json()
    assert left["identifier_id"] == str(quiet)
    unknown = await client.post(f"{FINANCE}/transfer-suggestions/{uuid4()}/dismiss")
    assert unknown.status_code == 404
    assert unknown.json()["detail"]["error"] == "account_identifier_not_found"


async def test_registered_identifier_is_not_a_suggestion_nor_dismissable(client, inferred) -> None:
    _, b_id, _ = inferred
    assert (await client.get(f"{FINANCE}/transfer-suggestions")).json() == []
    [identifier] = (await client.get(f"{FINANCE}/accounts/{b_id}/identifiers")).json()
    response = await client.post(f"{FINANCE}/transfer-suggestions/{identifier['id']}/dismiss")
    assert response.status_code == 422


async def test_void_of_a_leg_heals(client, session, inferred) -> None:
    _, _, record = inferred
    assert len(await _placeholders(session)) == 1

    response = await client.post(f"{FINANCE}/transactions/{record.id}/void")

    assert response.status_code == 200
    assert await _placeholders(session) == []
    assert await _transfers(client) == []


async def test_awaiting_shows_days_in_transit_and_overdue(client, session, run) -> None:
    a, c = await _connected(run, "A"), await _connected(run, "C")
    c_identifier = await run(get_or_create_identifier, "uk_bank_account", C_IDENTIFIER)
    sent_at = datetime.now(UTC) - timedelta(days=10)
    await _connector_tx(session, run, a, "-100", occurred_at=sent_at, identifier_id=c_identifier)
    assert (await _register(client, str(c[1].id), C_IDENTIFIER)).status_code == 201

    [transfer] = await _transfers(client, status="awaiting")

    assert transfer["days_in_transit"] in (10, 11)  # 10 days, in the owner's timezone
    assert transfer["overdue"] is True


async def test_unknown_transfer_404(client) -> None:
    unknown = uuid4()
    calls = [
        client.get(f"{FINANCE}/transfers/{unknown}/candidates"),
        client.post(f"{FINANCE}/transfers/{unknown}/reject"),
        client.post(f"{FINANCE}/transfers/{unknown}/unreject"),
        client.post(f"{FINANCE}/transfers/{unknown}/unpair"),
        client.post(f"{FINANCE}/transfers/{unknown}/pair", json={"transaction_id": str(uuid4())}),
    ]
    for call in calls:
        response = await call
        assert response.status_code == 404
        assert response.json()["detail"]["error"] == "transfer_not_found"


async def test_unknown_account_404(client) -> None:
    unknown = uuid4()
    for response in [
        await client.get(f"{FINANCE}/accounts/{unknown}/identifiers"),
        await _register(client, str(unknown), B_IDENTIFIER),
        await client.delete(f"{FINANCE}/accounts/{unknown}/identifiers/{uuid4()}"),
        await client.post(
            f"{FINANCE}/accounts/{unknown}/balance-mode", json={"balance_mode": "hybrid"}
        ),
    ]:
        assert response.status_code == 404
        assert response.json()["detail"]["error"] == "account_not_found"


async def test_malformed_transfer_setting_is_not_a_500(client, run) -> None:
    await run(set_setting, "finance.transfers", [2, 7])
    account_id = await _manual(client)

    for response in [
        await client.get(f"{FINANCE}/transfers"),
        await _register(client, account_id, B_IDENTIFIER),
    ]:
        assert response.status_code == 503
        detail = response.json()["detail"]
        assert detail["error"] == "invalid_transfer_settings"
        assert "finance.transfers must be an object" in detail["message"]


async def test_heal_waiting_on_the_matching_lock_times_out_as_503(
    client, run, backend_url, monkeypatch
) -> None:
    monkeypatch.setattr(transfers_api, "LOCK_TIMEOUT", "100ms")
    identifier = await run(get_or_create_identifier, "uk_bank_account", B_IDENTIFIER)
    other = create_engine(backend_url)
    try:
        async with other.connect() as conn:
            # Another transaction (a sync's heal, say) holds the matching lock.
            await conn.execute(text("select pg_advisory_xact_lock(hashtext('finance.transfers'))"))

            response = await client.post(f"{FINANCE}/transfer-suggestions/{identifier}/dismiss")

            await conn.rollback()
    finally:
        await other.dispose()
    assert response.status_code == 503
    assert response.json()["detail"]["error"] == "query_timeout"


async def test_hand_posted_in_transit_refused(client, session, run) -> None:
    # Only matching posts on In transit: a transfer's legs and its placeholders.
    in_transit = await run(get_or_create_system_account, "in_transit")
    b_id = await _manual(client)
    gbp = await get_currency(session, "GBP")
    count = text("select count(*) from finance.transactions")
    before = await session.scalar(count)

    response = await client.post(
        f"{FINANCE}/transactions",
        json={
            "occurred_at": WHEN.isoformat(),
            "description": "By hand",
            "postings": [
                {"account_id": b_id, "instrument_id": str(gbp.id), "quantity": "100"},
                {"account_id": str(in_transit), "instrument_id": str(gbp.id), "quantity": "-100"},
            ],
        },
    )

    assert response.status_code == 422
    assert response.json()["detail"] == {
        "error": "invalid_input",
        "message": "In transit holds only transfers between your accounts, which are"
        " matched automatically; post against another account",
    }
    assert await session.scalar(count) == before


async def test_identifier_on_a_system_account_refused(client, run) -> None:
    in_transit = await run(get_or_create_system_account, "in_transit")

    response = await _register(client, str(in_transit), B_IDENTIFIER)

    assert response.status_code == 422
    assert response.json()["detail"] == {
        "error": "invalid_input",
        "message": "a system account can't have identifiers",
    }
