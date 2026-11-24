"""/api/v1/connections: the eToro connection routes, the statement upload and the review
items (design §5.1, §5.3, §5.7, §5.14)."""

import base64
from collections.abc import AsyncIterator
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from procrastinate.testing import InMemoryConnector
from pydantic import SecretStr
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from lararium.api.app import create_app
from lararium.config import Settings
from lararium.core.actors import Actor
from lararium.core.commands import command_context
from lararium.core.connections import (
    create_connection,
    get_connection,
    load_credential,
    set_status,
)
from lararium.deps import get_session, get_session_factory
from lararium.modules.finance import connections_api
from lararium.modules.finance.etoro.client import GATE_MESSAGE
from lararium.modules.finance.etoro.normalize import EtoroDataError
from lararium.modules.finance.transfers import heal_transfers
from lararium.worker.app import procrastinate_app

from ..finance.etoro.fixtures import GCID, READ_SCOPES, REAL_CID, SYMBOL, dumps, me_json
from ..finance.etoro.statement_fixtures import build_statement, summary
from ..finance.etoro.test_normalize_api import Ledger, _postings
from ..finance.etoro.test_normalize_statement import (
    BASE,
    POSITION,
    TRADES,
    TRADING,
    open_row,
    trade_statement,
    transfer_in,
)

KEY = bytes(range(32))
OWNER = {"Tailscale-User-Login": "me@example.com"}
PUBLIC_KEY = "etoro-PUBLIC-KEY-TEST-do-not-log"
USER_KEY = "etoro-USER-KEY-TEST-do-not-log"
NEW_PUBLIC_KEY = "etoro-PUBLIC-KEY-TEST-second"
NEW_USER_KEY = "etoro-USER-KEY-TEST-second"
BODY = {"display_name": "eToro", "public_api_key": PUBLIC_KEY, "user_key": USER_KEY}
URL = "/api/v1/connections/etoro"
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


class FakeEtoroMe:
    """eToro's /me answer; every request is kept."""

    def __init__(self) -> None:
        self.status = 200
        self.body = dumps(me_json())
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return httpx.Response(self.status, text=self.body)


@pytest.fixture
def etoro() -> FakeEtoroMe:
    return FakeEtoroMe()


@pytest.fixture
def sessions(session: AsyncSession) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(bind=session.bind, join_transaction_mode="create_savepoint")


@pytest.fixture
def queue() -> Any:
    with procrastinate_app.replace_connector(InMemoryConnector()) as app:
        yield app.connector


@pytest.fixture
async def api(session, sessions, etoro, queue, tmp_path: Path) -> AsyncIterator[httpx.AsyncClient]:
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
        async with httpx.AsyncClient(transport=httpx.MockTransport(etoro)) as http:
            yield http

    app.dependency_overrides[get_session] = _test_session
    app.dependency_overrides[get_session_factory] = lambda: sessions
    app.dependency_overrides[connections_api.get_http] = _test_http
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


async def _create(api: httpx.AsyncClient, **overrides: Any) -> dict[str, Any]:
    response = await api.post(URL, json=BODY | overrides, headers=OWNER)
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


def _queued(queue) -> list[str]:
    return [job["task_name"] for job in queue.jobs.values()]


def _files(root: Path) -> list[Path]:
    return [path for path in root.rglob("*") if path.is_file()]


# Creating and updating ---------------------------------------------------------------------


async def test_create_hides_keys(api, session, queue, etoro) -> None:
    created = await _create(api)

    assert created["connector"] == "etoro"
    assert created["status"] == "healthy"
    for secret in (PUBLIC_KEY, USER_KEY):
        assert secret not in str(created)
        assert secret not in await _logged(session)
    args = await session.scalar(
        text("select args from core.audit_log where command = 'create_etoro_connection'")
    )
    assert args == {"display_name": "eToro"}
    public = await load_credential(session, KEY, created["id"], "public_api_key")
    user = await load_credential(session, KEY, created["id"], "user_key")
    assert public is not None and public.reveal() == PUBLIC_KEY
    assert user is not None and user.reveal() == USER_KEY
    record = await get_connection(session, created["id"])
    # Where the sync's user check looks.
    assert record.cursor == {"gcid": str(GCID), "real_cid": str(REAL_CID)}
    assert _queued(queue) == ["sync_connection"]
    # /me only, once, before anything was written.
    assert [(r.method, r.url.path) for r in etoro.requests] == [("GET", "/api/v1/me")]


@pytest.mark.parametrize(
    "extra", ["etoro-public:trade.real:write", "money:transfer", "etoro-public:trade.real"]
)
async def test_create_refuses_write_key_422(api, session, etoro, queue, extra) -> None:
    etoro.body = dumps(me_json([*READ_SCOPES, extra]))

    response = await api.post(URL, json=BODY, headers=OWNER)

    assert response.status_code == 422
    assert response.json()["detail"]["message"] == GATE_MESSAGE
    assert extra not in response.text
    assert PUBLIC_KEY not in response.text and USER_KEY not in response.text
    assert await session.scalar(text("select count(*) from core.connections")) == 0
    assert await session.scalar(text("select count(*) from core.credentials")) == 0
    assert _queued(queue) == []


async def test_create_refuses_a_key_with_no_scopes_422(api, session, etoro) -> None:
    etoro.body = dumps(me_json([]))

    response = await api.post(URL, json=BODY, headers=OWNER)

    assert response.status_code == 422
    assert "Read permission" in response.json()["detail"]["message"]
    assert await session.scalar(text("select count(*) from core.connections")) == 0


@pytest.mark.parametrize("status", [401, 403])
async def test_create_rejected_key_422(api, session, etoro, queue, status) -> None:
    etoro.status = status
    etoro.body = f'{{"error": "{PUBLIC_KEY}"}}'

    response = await api.post(URL, json=BODY, headers=OWNER)

    assert response.status_code == 422
    assert response.json()["detail"]["message"] == "eToro rejected this key"
    assert PUBLIC_KEY not in response.text and USER_KEY not in response.text
    assert await session.scalar(text("select count(*) from core.connections")) == 0
    assert _queued(queue) == []


@pytest.mark.parametrize(
    ("status", "body"), [(403, "error code: 1010"), (500, "boom"), (404, "nope")]
)
async def test_create_other_errors_422_with_a_fixed_message(api, etoro, status, body) -> None:
    etoro.status = status
    etoro.body = f"{body} {PUBLIC_KEY}"

    response = await api.post(URL, json=BODY, headers=OWNER)

    assert response.status_code == 422
    message = response.json()["detail"]["message"]
    assert message.startswith("eToro could not check this key")
    assert PUBLIC_KEY not in response.text


async def test_create_without_user_ids_422(api, etoro) -> None:
    me = me_json()
    del me["realCid"]
    etoro.body = dumps(me)

    response = await api.post(URL, json=BODY, headers=OWNER)

    assert response.status_code == 422
    assert "realCid" in response.json()["detail"]["message"]


async def test_same_user_twice_409(api, queue) -> None:
    await _create(api)
    queue.jobs.clear()

    response = await api.post(URL, json=BODY | {"display_name": "eToro two"}, headers=OWNER)

    assert response.status_code == 409
    assert "already connected" in response.json()["detail"]["message"]
    assert PUBLIC_KEY not in response.text
    assert _queued(queue) == []


async def test_update_from_needs_reauth(api, session, run, queue, etoro) -> None:
    created = await _create(api)
    await run(set_status, created["id"], "needs_reauth", error="eToro rejected the key")
    queue.jobs.clear()
    etoro.requests.clear()

    response = await api.put(
        f"/api/v1/connections/{created['id']}/etoro",
        json={"public_api_key": NEW_PUBLIC_KEY, "user_key": NEW_USER_KEY},
        headers=OWNER,
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert (body["status"], body["last_error"]) == ("healthy", None)
    assert NEW_PUBLIC_KEY not in response.text and NEW_USER_KEY not in response.text
    public = await load_credential(session, KEY, created["id"], "public_api_key")
    user = await load_credential(session, KEY, created["id"], "user_key")
    assert public is not None and public.reveal() == NEW_PUBLIC_KEY
    assert user is not None and user.reveal() == NEW_USER_KEY
    logged = await _logged(session)
    assert NEW_PUBLIC_KEY not in logged and NEW_USER_KEY not in logged
    args = await session.scalar(
        text("select args from core.audit_log where command = 'update_etoro_connection'")
    )
    assert args == {"connection_id": created["id"]}
    # The user check stays where the sync looks; the sync's gate checks the new keys.
    record = await get_connection(session, created["id"])
    assert record.cursor == {"gcid": str(GCID), "real_cid": str(REAL_CID)}
    assert etoro.requests == []
    assert _queued(queue) == ["sync_connection"]


async def test_update_refuses_other_connectors_and_disabled(api, run) -> None:
    created = await _create(api)
    await run(set_status, created["id"], "disabled")
    keys = {"public_api_key": NEW_PUBLIC_KEY, "user_key": NEW_USER_KEY}
    response = await api.put(f"/api/v1/connections/{created['id']}/etoro", json=keys, headers=OWNER)
    assert response.status_code == 422
    assert response.json()["detail"]["message"] == "a disabled connection can't be updated"

    other = await api.post(
        "/api/v1/connections/ibkr",
        json={
            "display_name": "I",
            "token": "tok-TEST",
            "token_expires_on": "2099-01-01",
            "query_id": "1",
        },
        headers=OWNER,
    )
    assert other.status_code == 201, other.text
    response = await api.put(
        f"/api/v1/connections/{other.json()['id']}/etoro", json=keys, headers=OWNER
    )
    assert response.status_code == 422
    assert response.json()["detail"]["message"] == "this is not an eToro connection"


async def test_too_long_keys_are_not_echoed(api) -> None:
    public, user = "p" * 501, "u" * 501
    response = await api.post(
        URL, json=BODY | {"public_api_key": public, "user_key": user}, headers=OWNER
    )
    assert response.status_code == 422
    assert public not in response.text and user not in response.text

    updated = await api.put(
        f"/api/v1/connections/{uuid4()}/etoro",
        json={"public_api_key": public, "user_key": user},
        headers=OWNER,
    )
    assert updated.status_code == 422
    assert public not in updated.text and user not in updated.text


# The statement upload ----------------------------------------------------------------------


async def _synced(api, session) -> tuple[dict[str, Any], Ledger]:
    """A connection whose first sync has stored the user, balances and one trade."""
    created = await _create(api)
    ledger = Ledger(session, await get_connection(session, created["id"]))
    await ledger.normalize(*BASE, *TRADES)
    return created, ledger


async def _upload(
    api, connection_id: str, content: bytes, name: str = "statement.xlsx"
) -> httpx.Response:
    return await api.post(
        f"/api/v1/connections/{connection_id}/etoro-statement",
        files={"file": (name, content, XLSX)},
        headers=OWNER,
    )


async def test_upload_before_first_sync_409(api, session, tmp_path) -> None:
    created = await _create(api)

    response = await _upload(api, created["id"], trade_statement())

    assert response.status_code == 409
    assert "first sync" in response.json()["detail"]["message"]
    assert await session.scalar(text("select count(*) from core.documents")) == 0
    assert _files(tmp_path) == []


async def test_upload_ingests_and_heals(api, session, tmp_path) -> None:
    created, _ = await _synced(api, session)
    body = trade_statement()

    response = await _upload(api, created["id"], body)

    assert response.status_code == 202, response.text
    assert response.json()["raw_records"] > 0
    stored = await session.scalar(
        text(
            "select count(*) from core.raw_records where connection_id = :c"
            " and record_type like 'etoro.statement.%'"
        ),
        {"c": created["id"]},
    )
    assert stored == response.json()["raw_records"]
    # The statement's transfer posted, and the upload's own heal matched it: nothing is left
    # for another heal.
    described = await session.scalars(
        text("select description from finance.transactions where connection_id = :c"),
        {"c": created["id"]},
    )
    assert "Transfer from eToro Money" in list(described)
    async with command_context(session, Actor("system", "t"), "t", {}) as ctx:
        assert await heal_transfers(ctx) == 0
    # The document, row and bytes.
    document = (
        await session.execute(text("select sha256, source, original_filename from core.documents"))
    ).one()
    assert (document.source, document.original_filename) == ("finance.etoro", "statement.xlsx")
    [blob] = _files(tmp_path)
    assert blob.name == document.sha256 and blob.read_bytes() == body
    audit = await session.scalar(
        text("select args from core.audit_log where command = 'upload_etoro_statement'")
    )
    assert audit["filename"] == "statement.xlsx" and "sha256" in audit
    assert PUBLIC_KEY not in await _logged(session)


async def test_upload_heals_in_the_upload(api, session, monkeypatch) -> None:
    created, _ = await _synced(api, session)
    healed: list[tuple[UUID, int]] = []
    real = connections_api.transfers.heal_transfers

    async def spy(ctx) -> int:
        matched = await real(ctx)
        healed.append((ctx.correlation_id, matched))
        return matched

    monkeypatch.setattr(connections_api.transfers, "heal_transfers", spy)

    response = await _upload(api, created["id"], trade_statement())

    assert response.status_code == 202, response.text
    # One heal, in the upload's own command, and it matched the statement's transfer leg.
    upload = await session.scalar(
        text("select correlation_id from core.audit_log where command = 'upload_etoro_statement'")
    )
    [(correlation_id, matched)] = healed
    assert correlation_id == upload
    assert matched >= 1


async def test_upload_other_user_422(api, session, tmp_path) -> None:
    created, _ = await _synced(api, session)
    other = build_statement(summary(username="someone-else-42"), [], [])

    response = await _upload(api, created["id"], other)

    assert response.status_code == 422
    message = response.json()["detail"]["message"]
    assert message == "this statement is for another eToro user"
    assert "someone-else-42" not in response.text
    assert await session.scalar(text("select count(*) from core.documents")) == 0
    assert _files(tmp_path) == []


async def test_upload_not_an_xlsx_422(api, session, tmp_path) -> None:
    created, _ = await _synced(api, session)

    response = await _upload(api, created["id"], b"not a workbook at all")

    assert response.status_code == 422
    assert response.json()["detail"]["message"] == "the file is not an xlsx workbook"
    assert _files(tmp_path) == []


async def test_upload_too_large_413(api, session, monkeypatch, tmp_path) -> None:
    created, _ = await _synced(api, session)
    monkeypatch.setattr(connections_api, "MAX_ETORO_UPLOAD_BYTES", 1024)

    response = await _upload(api, created["id"], b"P" * 2048)

    assert response.status_code == 413
    assert response.json()["detail"]["error"] == "file_too_large"
    assert _files(tmp_path) == []


async def test_upload_a_statement_that_cannot_post_422(api, session, tmp_path) -> None:
    created, _ = await _synced(api, session)
    # The open row has no units: it cannot be posted.
    body = build_statement(
        summary(username=me_json()["username"]),
        [transfer_in(), open_row(units="")],
        [],
    )

    response = await _upload(api, created["id"], body)

    assert response.status_code == 422, response.text
    message = response.json()["detail"]["message"]
    assert "lacks its units" in message and str(POSITION) not in message
    assert await session.scalar(text("select count(*) from core.documents")) == 0
    assert _files(tmp_path) == []


def test_upload_limit_is_16_mib() -> None:
    assert connections_api.MAX_ETORO_UPLOAD_BYTES == 16 * 1024 * 1024


async def test_upload_to_non_etoro_connection_422(api) -> None:
    other = await api.post(
        "/api/v1/connections/ibkr",
        json={
            "display_name": "I",
            "token": "tok-TEST",
            "token_expires_on": "2099-01-01",
            "query_id": "1",
        },
        headers=OWNER,
    )
    assert other.status_code == 201, other.text

    response = await _upload(api, other.json()["id"], trade_statement())

    assert response.status_code == 422
    assert response.json()["detail"]["message"] == "this is not an eToro connection"


async def test_a_failed_upload_leaves_no_file(api, session, monkeypatch, tmp_path) -> None:
    created, _ = await _synced(api, session)

    async def fail(ctx) -> int:
        raise RuntimeError("heal failed")

    # The last database work, after the statement's rows and its normalizing.
    monkeypatch.setattr(connections_api.transfers, "heal_transfers", fail)

    with pytest.raises(RuntimeError):
        await _upload(api, created["id"], trade_statement())

    assert await session.scalar(text("select count(*) from core.documents")) == 0
    assert _files(tmp_path) == []
    statement_rows = await session.scalar(
        text("select count(*) from core.raw_records where record_type like 'etoro.statement.%'")
    )
    assert statement_rows == 0
    # The statement's own events (its transfer row) are posted under `activity:` keys.
    statement_transactions = await session.scalar(
        text("select count(*) from finance.transactions where external_id like 'activity:%'")
    )
    assert statement_transactions == 0


async def test_upload_to_disabled_connection_422(api, session, run, tmp_path) -> None:
    created, _ = await _synced(api, session)
    await run(set_status, created["id"], "disabled")

    response = await _upload(api, created["id"], trade_statement())

    assert response.status_code == 422
    assert response.json()["detail"]["message"] == "a disabled connection can't take a statement"
    assert await session.scalar(text("select count(*) from core.documents")) == 0
    assert _files(tmp_path) == []
    assert (await get_connection(session, created["id"])).status == "disabled"


async def test_upload_parses_before_taking_the_matching_lock(api, session, monkeypatch) -> None:
    created, _ = await _synced(api, session)
    order: list[str] = []
    real_parse = connections_api.parse_etoro_upload
    real_lock = connections_api.transfers.lock_transfers

    async def parse(body):
        order.append("parse")
        return await real_parse(body)

    async def lock(ctx):
        order.append("lock")
        return await real_lock(ctx)

    monkeypatch.setattr(connections_api, "parse_etoro_upload", parse)
    monkeypatch.setattr(connections_api.transfers, "lock_transfers", lock)

    response = await _upload(api, created["id"], trade_statement())

    assert response.status_code == 202, response.text
    assert order[:2] == ["parse", "lock"]


# Review items ------------------------------------------------------------------------------


async def _disagreeing(api, session) -> tuple[dict[str, Any], dict[str, Any]]:
    """A connection whose statement says 5 units where the API says 4: one open review."""
    created, _ = await _synced(api, session)
    response = await _upload(api, created["id"], trade_statement(closed_units="5"))
    assert response.status_code == 202, response.text
    listed = await api.get(f"/api/v1/connections/{created['id']}/reviews", headers=OWNER)
    assert listed.status_code == 200, listed.text
    [review] = listed.json()
    return created, review


async def test_reviews_list_and_resolve(api, session) -> None:
    created, review = await _disagreeing(api, session)
    assert (review["event_key"], review["field"]) == (f"open:{POSITION}", "units")
    assert (review["api_value"], review["statement_value"]) == ("4", "5")
    assert review["resolution"] is None
    # The API's value posts until the owner decides.
    assert (await _postings(session, f"open:{POSITION}"))[0][2] == Decimal(4)

    response = await api.post(
        f"/api/v1/connections/{created['id']}/reviews/{review['id']}/resolve",
        json={"take": "statement"},
        headers=OWNER,
    )

    assert response.status_code == 204, response.text
    # Within the same request: the statement's units post now.
    assert (await _postings(session, f"open:{POSITION}"))[:2] == [
        (TRADING, SYMBOL, Decimal(5), Decimal(100), "USD", None, None, False),
        (TRADING, "USD", Decimal("-500.00"), None, None, None, None, False),
    ]
    [resolved] = (
        await api.get(f"/api/v1/connections/{created['id']}/reviews", headers=OWNER)
    ).json()
    assert (resolved["id"], resolved["resolution"], resolved["resolved_by"]) == (
        review["id"],
        "statement",
        "user",
    )
    audit = await session.scalar(
        text("select args from core.audit_log where command = 'resolve_source_review'")
    )
    assert audit == {
        "connection_id": created["id"],
        "review_id": review["id"],
        "take": "statement",
    }

    again = await api.post(
        f"/api/v1/connections/{created['id']}/reviews/{review['id']}/resolve",
        json={"take": "api"},
        headers=OWNER,
    )
    assert again.status_code == 409
    assert again.json()["detail"]["message"]


async def test_resolve_takes_the_connection_lock_first(api, session, monkeypatch) -> None:
    created, review = await _disagreeing(api, session)
    order: list[str] = []
    real_lock = connections_api.lock_connection
    real_resolve = connections_api.resolve_review
    real_normalize = connections_api.etoro_normalize
    real_heal = connections_api.transfers.heal_transfers

    async def lock(ctx, connection_id):
        order.append("lock")
        return await real_lock(ctx, connection_id)

    async def resolve(ctx, review_id, take):
        order.append("resolve")
        return await real_resolve(ctx, review_id, take)

    async def normalize(ctx, ids):
        order.append("normalize")
        return await real_normalize(ctx, ids)

    async def heal(ctx):
        order.append("heal")
        return await real_heal(ctx)

    monkeypatch.setattr(connections_api, "lock_connection", lock)
    monkeypatch.setattr(connections_api, "resolve_review", resolve)
    monkeypatch.setattr(connections_api, "etoro_normalize", normalize)
    monkeypatch.setattr(connections_api.transfers, "heal_transfers", heal)

    response = await api.post(
        f"/api/v1/connections/{created['id']}/reviews/{review['id']}/resolve",
        json={"take": "api"},
        headers=OWNER,
    )

    assert response.status_code == 204, response.text
    assert order == ["lock", "resolve", "normalize", "heal"]


async def test_a_review_of_another_connection_404(api, session, run) -> None:
    created, review = await _disagreeing(api, session)
    other = await run(
        lambda ctx: create_connection(ctx, connector="etoro", display_name="other", config={})
    )

    response = await api.post(
        f"/api/v1/connections/{other.id}/reviews/{review['id']}/resolve",
        json={"take": "statement"},
        headers=OWNER,
    )

    assert response.status_code == 404
    assert response.json()["detail"]["message"]
    listed = (await api.get(f"/api/v1/connections/{created['id']}/reviews", headers=OWNER)).json()
    assert listed[0]["resolution"] is None
    assert (await api.get(f"/api/v1/connections/{other.id}/reviews", headers=OWNER)).json() == []


async def _ibkr(api) -> str:
    other = await api.post(
        "/api/v1/connections/ibkr",
        json={
            "display_name": "I",
            "token": "tok-TEST",
            "token_expires_on": "2099-01-01",
            "query_id": "1",
        },
        headers=OWNER,
    )
    assert other.status_code == 201, other.text
    return other.json()["id"]


async def test_review_routes_refuse_other_connectors_422(api, session, monkeypatch) -> None:
    created, review = await _disagreeing(api, session)
    ibkr = await _ibkr(api)

    async def never(*args, **kwargs):
        raise AssertionError("nothing runs for another connector")

    monkeypatch.setattr(connections_api, "lock_connection", never)
    monkeypatch.setattr(connections_api, "resolve_review", never)

    listed = await api.get(f"/api/v1/connections/{ibkr}/reviews", headers=OWNER)
    resolved = await api.post(
        f"/api/v1/connections/{ibkr}/reviews/{review['id']}/resolve",
        json={"take": "statement"},
        headers=OWNER,
    )

    for response in (listed, resolved):
        assert response.status_code == 422
        assert response.json()["detail"] == {
            "error": "no_reviews",
            "message": "only eToro and Trading 212 connections have review items",
        }
    still = (await api.get(f"/api/v1/connections/{created['id']}/reviews", headers=OWNER)).json()
    assert still[0]["resolution"] is None


async def test_a_failed_resolve_leaves_the_review_open_422(api, session, monkeypatch) -> None:
    created, review = await _disagreeing(api, session)

    async def fail(ctx, ids):
        raise EtoroDataError("etoro.trade_history lacks units")

    monkeypatch.setattr(connections_api, "etoro_normalize", fail)

    response = await api.post(
        f"/api/v1/connections/{created['id']}/reviews/{review['id']}/resolve",
        json={"take": "statement"},
        headers=OWNER,
    )

    assert response.status_code == 422
    assert response.json()["detail"]["message"] == "etoro.trade_history lacks units"
    [still] = (await api.get(f"/api/v1/connections/{created['id']}/reviews", headers=OWNER)).json()
    assert (still["resolution"], still["resolved_at"]) == (None, None)
    decisions = await session.scalar(text("select count(*) from finance.source_decisions"))
    assert decisions == 0
    assert (await _postings(session, f"open:{POSITION}"))[0][2] == Decimal(4)


async def test_reviews_of_unknown_connection_404(api) -> None:
    response = await api.get(f"/api/v1/connections/{uuid4()}/reviews", headers=OWNER)
    assert response.status_code == 404


async def test_resolve_take_must_be_known(api, session) -> None:
    created, review = await _disagreeing(api, session)
    response = await api.post(
        f"/api/v1/connections/{created['id']}/reviews/{review['id']}/resolve",
        json={"take": "both"},
        headers=OWNER,
    )
    assert response.status_code == 422


async def test_unknown_review_404(api, session) -> None:
    created, _ = await _synced(api, session)
    response = await api.post(
        f"/api/v1/connections/{created['id']}/reviews/{UUID(int=1)}/resolve",
        json={"take": "api"},
        headers=OWNER,
    )
    assert response.status_code == 404
