"""/api/v1/connections: the optional Trading 212 export upload, and Trading 212's review items
(design §5.3a, §5.7)."""

import base64
from collections.abc import AsyncIterator
from decimal import Decimal
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
from lararium.core.connections import create_connection, get_connection, set_status
from lararium.deps import get_session, get_session_factory
from lararium.modules.finance import connections_api
from lararium.modules.finance.trading212.normalize import Trading212DataError
from lararium.worker.app import procrastinate_app

from ..finance.trading212.fixtures import summary_json, transaction_item
from ..finance.trading212.test_export import REF, export_csv, row
from ..finance.trading212.test_normalize import Ledger, _postings

KEY = bytes(range(32))
OWNER = {"Tailscale-User-Login": "me@example.com"}
BODY = {
    "display_name": "Trading 212 Invest",
    "account_kind": "invest",
    "api_key": "t212-KEY-TEST-do-not-log",
    "api_secret": "t212-SECRET-TEST-do-not-log",
}
ACCOUNT_ID = 12345678
SUMMARY = f'{{"id": {ACCOUNT_ID}, "currency": "GBP", "totalValue": 10.5}}'
CSV = "text/csv"


@pytest.fixture
def sessions(session: AsyncSession) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(bind=session.bind, join_transaction_mode="create_savepoint")


@pytest.fixture
def queue() -> Any:
    with procrastinate_app.replace_connector(InMemoryConnector()) as app:
        yield app.connector


@pytest.fixture
async def api(session, sessions, queue, tmp_path: Path) -> AsyncIterator[httpx.AsyncClient]:
    settings = Settings(
        database_url="postgresql+psycopg://unused@localhost/unused",
        auth_mode="tailscale",
        owner_login="me@example.com",
        master_key=SecretStr(base64.b64encode(KEY).decode()),
        document_dir=tmp_path,
    )
    app = create_app(settings)

    async def _test_session() -> AsyncIterator[AsyncSession]:
        yield session

    async def _test_http() -> AsyncIterator[httpx.AsyncClient]:
        def summary(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, text=SUMMARY)

        async with httpx.AsyncClient(transport=httpx.MockTransport(summary)) as http:
            yield http

    app.dependency_overrides[get_session] = _test_session
    app.dependency_overrides[get_session_factory] = lambda: sessions
    app.dependency_overrides[connections_api.get_http] = _test_http
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


def _files(root: Path) -> list[Path]:
    return [path for path in root.rglob("*") if path.is_file()]


async def _create(api) -> dict[str, Any]:
    response = await api.post("/api/v1/connections/trading212", json=BODY, headers=OWNER)
    assert response.status_code == 201, response.text
    return response.json()


async def _synced(api, session) -> tuple[dict[str, Any], Ledger]:
    """A connection whose first sync has stored its summary and one deposit."""
    created = await _create(api)
    ledger = Ledger(session, await get_connection(session, created["id"]))
    await ledger.normalize(
        ("summary", summary_json(account_id=ACCOUNT_ID)), ("transactions", transaction_item())
    )
    return created, ledger


async def _upload(api, connection_id: str, content: bytes, name: str = "export.csv"):
    return await api.post(
        f"/api/v1/connections/{connection_id}/trading212-export",
        files={"file": (name, content, CSV)},
        headers=OWNER,
    )


async def _documents(session) -> int:
    return await session.scalar(text("select count(*) from core.documents"))


async def test_upload_before_first_sync_409(api, session, tmp_path) -> None:
    created = await _create(api)
    response = await _upload(api, created["id"], export_csv(row()))
    assert response.status_code == 409
    assert response.json()["detail"]["error"] == "first_sync_pending"
    assert await _documents(session) == 0
    assert _files(tmp_path) == []


async def test_upload_ingests(api, session, tmp_path) -> None:
    created, _ = await _synced(api, session)
    body = export_csv(row(), row(reference="ref-2"))

    response = await _upload(api, created["id"], body)

    assert response.status_code == 202, response.text
    assert response.json() == {"raw_records": 2}
    linked = await session.scalar(
        text(
            "select t.external_id from finance.trading212_export_rows e"
            " join finance.transactions t on t.id = e.transaction_id where e.id = :i"
        ),
        {"i": REF},
    )
    assert linked == f"transaction:{REF}"
    document = (
        await session.execute(text("select sha256, source, original_filename from core.documents"))
    ).one()
    assert (document.source, document.original_filename) == ("finance.trading212", "export.csv")
    [blob] = _files(tmp_path)
    assert blob.name == document.sha256 and blob.read_bytes() == body
    audit = await session.scalar(
        text("select args from core.audit_log where command = 'upload_trading212_export'")
    )
    assert audit["filename"] == "export.csv" and "sha256" in audit
    assert REF not in str(audit)


async def test_upload_too_large_413(api, session, monkeypatch, tmp_path) -> None:
    created, _ = await _synced(api, session)
    monkeypatch.setattr(connections_api, "MAX_TRADING212_UPLOAD_BYTES", 1024)
    response = await _upload(api, created["id"], b"A" * 2048)
    assert response.status_code == 413
    assert _files(tmp_path) == []


def test_upload_limit_is_16_mib() -> None:
    assert connections_api.MAX_TRADING212_UPLOAD_BYTES == 16 * 1024 * 1024


@pytest.mark.parametrize(
    ("body", "message"),
    [
        (b"Action,ID\xff\r\n", "the export is not UTF-8 text"),
        (export_csv(row(total="secret-ish")), "the export's Total is not a number"),
    ],
)
async def test_a_bad_file_422(api, session, tmp_path, body, message) -> None:
    created, _ = await _synced(api, session)
    response = await _upload(api, created["id"], body)
    assert response.status_code == 422
    assert response.json()["detail"]["error"] == "export_invalid"
    assert response.json()["detail"]["message"].startswith(message)
    assert "secret-ish" not in response.text
    assert await _documents(session) == 0
    assert _files(tmp_path) == []


async def test_other_accounts_ids_refused_422(api, session, run, tmp_path) -> None:
    created, _ = await _synced(api, session)
    theirs = await run(
        lambda ctx: create_connection(
            ctx, connector="trading212", display_name="ISA", config={"account_kind": "isa"}
        )
    )
    await Ledger(session, theirs).normalize(
        ("summary", summary_json(account_id=20000002)),
        ("transactions", transaction_item(reference="theirs-1")),
    )

    response = await _upload(api, created["id"], export_csv(row(reference="theirs-1")))

    assert response.status_code == 422
    assert response.json()["detail"] == {
        "error": "export_invalid",
        "message": "this export is of another Trading 212 account",
    }
    assert "theirs-1" not in response.text
    assert await _documents(session) == 0
    assert _files(tmp_path) == []


async def test_upload_to_another_connector_422(api, run) -> None:
    other = await run(
        lambda ctx: create_connection(ctx, connector="etoro", display_name="E", config={})
    )
    response = await _upload(api, str(other.id), export_csv(row()))
    assert response.status_code == 422
    assert response.json()["detail"]["message"] == "this is not a Trading 212 connection"


async def test_upload_to_disabled_connection_422(api, session, run, tmp_path) -> None:
    created, _ = await _synced(api, session)
    await run(set_status, created["id"], "disabled")
    response = await _upload(api, created["id"], export_csv(row()))
    assert response.status_code == 422
    assert response.json()["detail"]["message"] == "a disabled connection can't take an export"
    assert _files(tmp_path) == []


async def test_a_failed_upload_leaves_no_file(api, session, monkeypatch, tmp_path) -> None:
    created, _ = await _synced(api, session)

    async def fail(ctx) -> int:
        raise RuntimeError("heal failed")

    # The last database work, after the export's rows and its normalizing.
    monkeypatch.setattr(connections_api.transfers, "heal_transfers", fail)

    with pytest.raises(RuntimeError):
        await _upload(api, created["id"], export_csv(row(reference="only-in-export")))

    assert await _documents(session) == 0
    assert _files(tmp_path) == []
    rows = await session.scalar(
        text("select count(*) from core.raw_records where record_type = 'trading212.export_row'")
    )
    assert rows == 0
    posted = await session.scalar(
        text(
            "select count(*) from finance.transactions"
            " where external_id = 'transaction:only-in-export'"
        )
    )
    assert posted == 0


async def test_upload_parses_before_taking_the_lock(api, session, monkeypatch) -> None:
    created, _ = await _synced(api, session)
    order: list[str] = []
    real_parse = connections_api.parse_trading212_upload
    real_lock = connections_api.transfers.lock_transfers

    async def parse(body):
        order.append("parse")
        return await real_parse(body)

    async def lock(ctx):
        order.append("lock")
        return await real_lock(ctx)

    monkeypatch.setattr(connections_api, "parse_trading212_upload", parse)
    monkeypatch.setattr(connections_api.transfers, "lock_transfers", lock)

    response = await _upload(api, created["id"], export_csv(row()))

    assert response.status_code == 202, response.text
    assert order[:2] == ["parse", "lock"]


# Review items --------------------------------------------------------------------------------


async def _disagreeing(api, session) -> tuple[dict[str, Any], dict[str, Any]]:
    created, _ = await _synced(api, session)
    response = await _upload(api, created["id"], export_csv(row(total="260.00")))
    assert response.status_code == 202, response.text
    listed = await api.get(f"/api/v1/connections/{created['id']}/reviews", headers=OWNER)
    assert listed.status_code == 200, listed.text
    [review] = listed.json()
    return created, review


async def test_reviews_list_and_resolve(api, session, monkeypatch) -> None:
    created, review = await _disagreeing(api, session)
    assert (review["event_key"], review["field"]) == (f"transaction:{REF}", "amount")
    assert (review["api_value"], review["statement_value"]) == ("250", "260")
    order: list[str] = []
    real_lock = connections_api.lock_connection
    real_normalize = connections_api.trading212_normalize

    async def lock(ctx, connection_id):
        order.append("lock")
        return await real_lock(ctx, connection_id)

    async def normalize(ctx, ids):
        order.append("normalize")
        return await real_normalize(ctx, ids)

    monkeypatch.setattr(connections_api, "lock_connection", lock)
    monkeypatch.setattr(connections_api, "trading212_normalize", normalize)

    response = await api.post(
        f"/api/v1/connections/{created['id']}/reviews/{review['id']}/resolve",
        json={"take": "statement"},
        headers=OWNER,
    )

    assert response.status_code == 204, response.text
    assert order == ["lock", "normalize"]
    # Within the same request: the export's amount posts now.
    assert (await _postings(session, f"transaction:{REF}"))[0][2] == Decimal("260.00")
    [resolved] = (
        await api.get(f"/api/v1/connections/{created['id']}/reviews", headers=OWNER)
    ).json()
    assert (resolved["resolution"], resolved["resolved_by"]) == ("statement", "user")


async def test_a_review_of_another_connection_404(api, session, run) -> None:
    created, review = await _disagreeing(api, session)
    other = await run(
        lambda ctx: create_connection(
            ctx, connector="trading212", display_name="B", config={"account_kind": "isa"}
        )
    )

    response = await api.post(
        f"/api/v1/connections/{other.id}/reviews/{review['id']}/resolve",
        json={"take": "statement"},
        headers=OWNER,
    )

    assert response.status_code == 404
    [still] = (await api.get(f"/api/v1/connections/{created['id']}/reviews", headers=OWNER)).json()
    assert still["resolution"] is None
    assert (await _postings(session, f"transaction:{REF}"))[0][2] == Decimal("250.00")


async def test_a_headerless_file_422_names_no_cell(api, session, tmp_path) -> None:
    created, _ = await _synced(api, session)
    body = b"Deposit,2026-03-01 09:15:00+00:00,777.77,ref-h,777.77,GBP\r\n"

    response = await _upload(api, created["id"], body)

    assert response.status_code == 422
    assert response.json()["detail"] == {
        "error": "export_invalid",
        "message": "the export lacks its Action column",
    }
    for value in ("777.77", "ref-h", "Deposit"):
        assert value not in response.text
    assert _files(tmp_path) == []


async def test_a_failed_resolve_leaves_the_review_open_422(api, session, monkeypatch) -> None:
    created, review = await _disagreeing(api, session)

    async def fail(ctx, ids):
        raise Trading212DataError("trading212.transaction lacks amount")

    monkeypatch.setattr(connections_api, "trading212_normalize", fail)

    response = await api.post(
        f"/api/v1/connections/{created['id']}/reviews/{review['id']}/resolve",
        json={"take": "statement"},
        headers=OWNER,
    )

    assert response.status_code == 422
    [still] = (await api.get(f"/api/v1/connections/{created['id']}/reviews", headers=OWNER)).json()
    assert still["resolution"] is None
