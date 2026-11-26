"""/api/v1/connections: the IBKR connection routes and the Flex statement upload."""

import base64
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
from procrastinate.testing import InMemoryConnector
from pydantic import SecretStr
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from lararium.api.app import create_app
from lararium.config import Settings
from lararium.core.connections import get_connection, load_credential, set_status, update_config
from lararium.modules.finance import connections_api
from lararium.modules.finance.ibkr import sync as ibkr_sync
from lararium.worker.app import procrastinate_app

from ..finance.ibkr.fixtures import AccountSpec, RowSpec, build_statement
from .overrides import use_session

KEY = bytes(range(32))
OWNER = {"Tailscale-User-Login": "me@example.com"}
TOKEN = "tok-TEST-do-not-log-0123"
OTHER_TOKEN = "tok-TEST-second-4567"
ISA = AccountSpec("U0000002", date_opened=date(2021, 4, 6), date_closed=date(2025, 5, 2))
ISA_TYPE = "IRA-Individual Savings Account/UK"
BODY = {
    "display_name": "My IBKR",
    "token": TOKEN,
    "token_expires_on": "2027-03-01",
    "query_id": "123456",
}


@pytest.fixture
def sessions(session: AsyncSession) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(bind=session.bind, join_transaction_mode="create_savepoint")


@pytest.fixture
def queue() -> Any:
    with procrastinate_app.replace_connector(InMemoryConnector()) as app:
        yield app.connector


@pytest.fixture
async def api(session, sessions, tmp_path: Path, queue) -> AsyncIterator[httpx.AsyncClient]:
    settings = Settings(
        database_url="postgresql+psycopg://unused@localhost/unused",
        auth_mode="tailscale",
        owner_login="me@example.com",
        master_key=SecretStr(base64.b64encode(KEY).decode()),
        document_dir=tmp_path,
    )
    app = create_app(settings)

    use_session(app, session, sessions)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


async def _create(api: httpx.AsyncClient, **overrides: Any) -> dict[str, Any]:
    response = await api.post("/api/v1/connections/ibkr", json=BODY | overrides, headers=OWNER)
    assert response.status_code == 201, response.text
    return response.json()


async def _logged(session: AsyncSession) -> str:
    logged = await session.scalar(
        text(
            "select coalesce((select string_agg(args::text || changes::text, ' ')"
            " from core.audit_log), '')"
            " || coalesce((select string_agg(data::text, ' ') from core.events), '')"
        )
    )
    return logged or ""


async def _synced(api: httpx.AsyncClient, session: AsyncSession) -> dict[str, Any]:
    """A connection whose first sync has finished: only then does it take an upload."""
    created = await _create(api)
    await session.execute(
        text("update core.connections set cursor = cast(:c as jsonb) where id = :i"),
        {"c": '{"history_from": "2025-11-18", "fetched_to": "2026-11-17"}', "i": created["id"]},
    )
    return created


def _queued(queue) -> list[str]:
    return [job["task_name"] for job in queue.jobs.values()]


async def test_create_hides_token(api, session) -> None:
    created = await _create(api)

    assert created["connector"] == "ibkr"
    assert created["status"] == "healthy"
    assert TOKEN not in str(created)
    # Midnight at the start of 2027-03-02 in London (GMT).
    assert datetime.fromisoformat(created["credential_expires_at"]) == datetime(
        2027, 3, 2, tzinfo=UTC
    )
    logged = await _logged(session)
    assert TOKEN not in logged
    args = await session.scalar(
        text("select args from core.audit_log where command = 'create_ibkr_connection'")
    )
    assert args == {
        "display_name": "My IBKR",
        "token_expires_on": "2027-03-01",
        "query_id": "123456",
    }
    stored = await load_credential(session, KEY, created["id"], "flex_token")
    assert stored is not None and stored.reveal() == TOKEN
    record = await get_connection(session, created["id"])
    assert record.config == {"query_id": "123456"}


async def test_credential_expiry_is_london_midnight_in_summer(api) -> None:
    created = await _create(api, token_expires_on="2027-07-01")

    # BST: the start of 2 July in London is 23:00 UTC on 1 July.
    assert datetime.fromisoformat(created["credential_expires_at"]) == datetime(
        2027, 7, 1, 23, tzinfo=UTC
    )


async def test_create_defers_sync(api, queue) -> None:
    await _create(api)

    assert _queued(queue) == ["sync_connection"]


async def test_update_from_needs_reauth_to_healthy_and_clears_expiry_reminder(
    api, session, run, queue
) -> None:
    created = await _create(api)
    connection_id = created["id"]
    await run(set_status, connection_id, "needs_reauth", error="The token was rejected")
    await run(update_config, connection_id, {ibkr_sync.EXPIRY_NOTIFIED_KEY: "2027-03-02"})
    queue.jobs.clear()

    response = await api.put(
        f"/api/v1/connections/{connection_id}/ibkr",
        json={"token": OTHER_TOKEN, "token_expires_on": "2027-09-01", "query_id": "654321"},
        headers=OWNER,
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert (body["status"], body["last_error"]) == ("healthy", None)
    assert datetime.fromisoformat(body["credential_expires_at"]) == datetime(
        2027, 9, 1, 23, tzinfo=UTC
    )
    assert OTHER_TOKEN not in response.text
    record = await get_connection(session, connection_id)
    assert record.config == {"query_id": "654321"}
    stored = await load_credential(session, KEY, connection_id, "flex_token")
    assert stored is not None and stored.reveal() == OTHER_TOKEN
    assert OTHER_TOKEN not in await _logged(session)
    assert _queued(queue) == ["sync_connection"]


@pytest.mark.parametrize("omit", [True, False])
async def test_update_without_query_id_keeps_the_stored_one(api, session, omit) -> None:
    created = await _create(api)
    body = {"token": OTHER_TOKEN, "token_expires_on": "2027-09-01"}
    response = await api.put(
        f"/api/v1/connections/{created['id']}/ibkr",
        json=body if omit else body | {"query_id": None},
        headers=OWNER,
    )

    assert response.status_code == 200, response.text
    record = await get_connection(session, created["id"])
    assert record.config == {"query_id": "123456"}
    stored = await load_credential(session, KEY, created["id"], "flex_token")
    assert stored is not None and stored.reveal() == OTHER_TOKEN


async def test_create_still_requires_query_id(api) -> None:
    body = {k: v for k, v in BODY.items() if k != "query_id"}
    response = await api.post("/api/v1/connections/ibkr", json=body, headers=OWNER)
    assert response.status_code == 422


async def test_update_keeps_a_failing_status(api, session, run) -> None:
    created = await _create(api)
    await run(set_status, created["id"], "failing", error="boom")

    response = await api.put(
        f"/api/v1/connections/{created['id']}/ibkr",
        json={"token": OTHER_TOKEN, "token_expires_on": "2027-09-01", "query_id": "654321"},
        headers=OWNER,
    )

    assert response.status_code == 200
    assert response.json()["status"] == "failing"


async def test_update_rejects_disabled_422(api, run) -> None:
    created = await _create(api)
    await run(set_status, created["id"], "disabled")

    response = await api.put(
        f"/api/v1/connections/{created['id']}/ibkr",
        json={"token": OTHER_TOKEN, "token_expires_on": "2027-09-01", "query_id": "654321"},
        headers=OWNER,
    )

    assert response.status_code == 422
    assert response.json()["detail"]["error"] == "not_updatable"


async def test_update_rejects_monzo_connection_422(api, run) -> None:
    from lararium.core.connections import create_connection

    monzo = await run(
        lambda ctx: create_connection(ctx, connector="monzo", display_name="M", config={})
    )

    response = await api.put(
        f"/api/v1/connections/{monzo.id}/ibkr",
        json={"token": OTHER_TOKEN, "token_expires_on": "2027-09-01", "query_id": "654321"},
        headers=OWNER,
    )

    assert response.status_code == 422
    assert OTHER_TOKEN not in response.text


async def test_update_unknown_connection_404(api) -> None:
    response = await api.put(
        "/api/v1/connections/01900000-0000-7000-8000-000000000001/ibkr",
        json={"token": OTHER_TOKEN, "token_expires_on": "2027-09-01", "query_id": "654321"},
        headers=OWNER,
    )

    assert response.status_code == 404


@pytest.mark.parametrize("query_id", ["", "12a", "1234567890123", " 12", "١٢٣"])
async def test_query_id_validated_422(api, query_id) -> None:
    created = await _create(api)

    post = await api.post(
        "/api/v1/connections/ibkr", json=BODY | {"query_id": query_id}, headers=OWNER
    )
    put = await api.put(
        f"/api/v1/connections/{created['id']}/ibkr",
        json={"token": TOKEN, "token_expires_on": "2027-09-01", "query_id": query_id},
        headers=OWNER,
    )

    assert (post.status_code, put.status_code) == (422, 422)


async def test_ibkr_operation_ids(api) -> None:
    schema = (await api.get("/openapi.json", headers=OWNER)).json()

    ids = {
        (path, method): op["operationId"]
        for path, methods in schema["paths"].items()
        for method, op in methods.items()
    }
    assert ids["/api/v1/connections/ibkr", "post"] == "create_ibkr_connection"
    assert ids["/api/v1/connections/{connection_id}/ibkr", "put"] == "update_ibkr_connection"
    assert (
        ids["/api/v1/connections/{connection_id}/ibkr-statement", "post"] == "upload_ibkr_statement"
    )


def _isa_statement() -> bytes:
    return build_statement(
        [ISA],
        period=(date(2026, 11, 1), date(2026, 11, 10)),
        rows=[RowSpec("AccountInformation", ISA.account_id, customerType=ISA_TYPE)],
    )


async def _upload(
    api, connection_id: str, content: bytes, content_type: str = "application/xml"
) -> httpx.Response:
    return await api.post(
        f"/api/v1/connections/{connection_id}/ibkr-statement",
        files={"file": ("isa.xml", content, content_type)},
        headers=OWNER,
    )


async def test_upload_ingests_and_normalizes(api, session) -> None:
    created = await _synced(api, session)

    response = await _upload(api, created["id"], _isa_statement())

    assert response.status_code == 202, response.text
    row = (
        await session.execute(text("select status, tax_wrapper, closed_on from finance.accounts"))
    ).one()
    assert (row.status, row.tax_wrapper, row.closed_on) == ("closed", "isa", date(2025, 5, 2))
    audit = await session.scalar(
        text("select args from core.audit_log where command = 'upload_ibkr_statement'")
    )
    assert audit["filename"] == "isa.xml" and "sha256" in audit
    assert TOKEN not in await _logged(session)


async def test_upload_twice_writes_nothing(api, session) -> None:
    created = await _synced(api, session)
    content = _isa_statement()
    await _upload(api, created["id"], content)

    async def counts() -> tuple[int, int, int]:
        return (
            await session.scalar(text("select count(*) from finance.accounts")),
            await session.scalar(text("select count(*) from finance.transactions")),
            await session.scalar(text("select count(*) from finance.ibkr_accounts")),
        )

    before = await counts()
    response = await _upload(api, created["id"], content)

    assert response.status_code == 202
    assert await counts() == before


async def test_upload_too_large_413(api, session, monkeypatch) -> None:
    created = await _synced(api, session)
    monkeypatch.setattr(connections_api, "MAX_IBKR_UPLOAD_BYTES", 1024)

    response = await _upload(api, created["id"], b"<x>" + b" " * 2048 + b"</x>")

    assert response.status_code == 413
    assert response.json()["detail"]["error"] == "file_too_large"


async def test_upload_limit_is_64_mib() -> None:
    assert connections_api.MAX_IBKR_UPLOAD_BYTES == 64 * 1024 * 1024


async def test_upload_rejects_other_content_type_415(api, session) -> None:
    created = await _synced(api, session)

    response = await _upload(api, created["id"], _isa_statement(), "application/pdf")

    assert response.status_code == 415


async def test_upload_bad_statement_422_names_the_field_only(api, session) -> None:
    created = await _synced(api, session)
    bad = build_statement(
        [ISA],
        period=(date(2026, 11, 1), date(2026, 11, 10)),
        rows=[RowSpec("AccountInformation", ISA.account_id, dateOpened="not-a-date-SECRET")],
    )

    response = await _upload(api, created["id"], bad)

    assert response.status_code == 422
    assert "SECRET" not in response.text
    assert await session.scalar(text("select count(*) from finance.accounts")) == 0


async def test_upload_not_xml_422(api, session) -> None:
    created = await _synced(api, session)

    response = await _upload(api, created["id"], b"hello")

    assert response.status_code == 422


async def test_upload_to_non_ibkr_connection_422(api, run) -> None:
    from lararium.core.connections import create_connection

    monzo = await run(
        lambda ctx: create_connection(ctx, connector="monzo", display_name="M", config={})
    )

    response = await _upload(api, str(monzo.id), _isa_statement())

    assert response.status_code == 422


@pytest.mark.parametrize("content_type", ["application/octet-stream", ""])
async def test_upload_accepts_generic_type_for_xml_name(api, session, content_type) -> None:
    created = await _synced(api, session)

    response = await _upload(api, created["id"], _isa_statement(), content_type)

    assert response.status_code == 202, response.text
    assert await session.scalar(text("select count(*) from finance.accounts")) == 1


@pytest.mark.parametrize("content_type", ["application/octet-stream", ""])
async def test_upload_generic_type_needs_xml_name_415(api, session, content_type) -> None:
    created = await _synced(api, session)

    response = await api.post(
        f"/api/v1/connections/{created['id']}/ibkr-statement",
        files={"file": ("statement.pdf", _isa_statement(), content_type)},
        headers=OWNER,
    )

    assert response.status_code == 415


async def test_upload_accepts_uppercase_xml_name(api, session) -> None:
    created = await _synced(api, session)

    response = await api.post(
        f"/api/v1/connections/{created['id']}/ibkr-statement",
        files={"file": ("STATEMENT.XML", _isa_statement(), "application/octet-stream")},
        headers=OWNER,
    )

    assert response.status_code == 202


async def test_upload_pairs_transfers(api, session) -> None:
    from lararium.core.actors import Actor
    from lararium.core.commands import command_context
    from lararium.modules.finance.transfers import heal_transfers, list_transfers

    from ..finance.ibkr.test_normalize_records import ETFA

    created = await _synced(api, session)
    day = date(2026, 11, 10)
    gia = AccountSpec("U0000001", date_opened=day)
    other = AccountSpec("U0000002", date_opened=day)
    legs = [
        RowSpec(
            "Transfer",
            "U0000001",
            **ETFA,
            transactionID="6000001",
            quantity="5",
            direction="IN",
            account="U0000002",
        ),
        RowSpec(
            "Transfer",
            "U0000002",
            **ETFA,
            transactionID="6000002",
            quantity="5",
            direction="OUT",
            account="U0000001",
        ),
    ]
    content = build_statement([gia, other], period=(day, day), rows=legs)

    response = await _upload(api, created["id"], content)

    assert response.status_code == 202, response.text
    [transfer] = await list_transfers(session)
    assert transfer.status == "matched"
    # The upload's own heal paired them: nothing is left for another one.
    async with command_context(session, Actor("system", "t"), "t", {}) as ctx:
        assert await heal_transfers(ctx) == 0


async def test_upload_to_disabled_connection_keeps_it_disabled(api, session, run) -> None:
    created = await _synced(api, session)
    await run(set_status, created["id"], "disabled")

    response = await _upload(api, created["id"], _isa_statement())

    assert response.status_code == 202
    assert (await get_connection(session, created["id"])).status == "disabled"


async def test_update_rejects_expiry_in_past_422(api, session) -> None:
    created = await _create(api)

    response = await api.put(
        f"/api/v1/connections/{created['id']}/ibkr",
        json={"token": OTHER_TOKEN, "token_expires_on": "2020-01-01", "query_id": "654321"},
        headers=OWNER,
    )

    assert response.status_code == 422
    assert response.json()["detail"]["message"] == "the token's expiry date is in the past"
    stored = await load_credential(session, KEY, created["id"], "flex_token")
    assert stored is not None and stored.reveal() == TOKEN


async def test_update_checks_status_under_the_row_lock(api, session, run, monkeypatch) -> None:
    """A disable that lands after the route's first read must still be seen."""
    created = await _create(api)
    real = connections_api.lock_connection
    seen: list[str] = []

    async def disable_then_lock(ctx, connection_id):
        await run(set_status, connection_id, "disabled")
        record = await real(ctx, connection_id)
        seen.append(record.status)
        return record

    monkeypatch.setattr(connections_api, "lock_connection", disable_then_lock)

    response = await api.put(
        f"/api/v1/connections/{created['id']}/ibkr",
        json={"token": OTHER_TOKEN, "token_expires_on": "2027-09-01", "query_id": "654321"},
        headers=OWNER,
    )

    assert seen == ["disabled"]
    assert response.status_code == 422
    stored = await load_credential(session, KEY, created["id"], "flex_token")
    assert stored is not None and stored.reveal() == TOKEN


async def test_upload_waits_for_the_first_sync_409(api, session) -> None:
    # Until the first sync has finished, the worker may be writing the same rows.
    created = await _create(api)

    response = await _upload(api, created["id"], _isa_statement())

    assert response.status_code == 409
    assert response.json()["detail"]["message"] == "wait for the first sync to finish"
    assert await session.scalar(text("select count(*) from core.raw_records")) == 0


async def test_create_rejects_expiry_in_past_422(api, session) -> None:
    response = await api.post(
        "/api/v1/connections/ibkr",
        json=BODY | {"token_expires_on": "2020-01-01"},
        headers=OWNER,
    )

    assert response.status_code == 422
    assert response.json()["detail"]["message"] == "the token's expiry date is in the past"
    assert await session.scalar(text("select count(*) from core.connections")) == 0
