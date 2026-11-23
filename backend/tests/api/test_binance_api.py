"""/api/v1/connections: the Binance connection routes, and the `chain_address` identifier."""

import base64
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from procrastinate.testing import InMemoryConnector
from pydantic import SecretStr
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from lararium.api.app import create_app
from lararium.config import Settings
from lararium.core.connections import (
    get_connection,
    load_credential,
    record_sync_failure,
    set_status,
    update_config,
)
from lararium.deps import get_session, get_session_factory
from lararium.modules import finance
from lararium.modules.finance import connections_api
from lararium.modules.finance.accounts import create_account
from lararium.modules.finance.binance.accounts import wallet_account
from lararium.modules.finance.binance.sync import BinanceConnector
from lararium.modules.finance.instruments import create_instrument
from lararium.modules.finance.system_accounts import get_or_create_system_account
from lararium.worker.app import procrastinate_app

from ..finance.binance.fixtures import NOW_MS, FakeBinance
from ..finance.binance.syncing import (
    API_KEY,
    API_SECRET,
    UID,
    Clock,
    binance_connection,
    run_binance_sync,
)

KEY = bytes(range(32))
OWNER = {"Tailscale-User-Login": "me@example.com"}
OTHER_SECRET = "as-TEST-second-8910"
FINANCE = "/api/v1/finance"
UID_ID = "00000000-0000-4000-8000-000000000000"
HMAC = {"display_name": "My Binance", "key_type": "hmac", "api_key": API_KEY}
BODY = HMAC | {"api_secret": API_SECRET}


@pytest.fixture
def sessions(session: AsyncSession) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(bind=session.bind, join_transaction_mode="create_savepoint")


@pytest.fixture
def queue() -> Any:
    with procrastinate_app.replace_connector(InMemoryConnector()) as app:
        yield app.connector


@pytest.fixture
def fake() -> FakeBinance:
    return FakeBinance()


@pytest.fixture
async def api(
    session, sessions, tmp_path: Path, queue, fake: FakeBinance
) -> AsyncIterator[httpx.AsyncClient]:
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

    app.dependency_overrides[get_session] = _test_session
    app.dependency_overrides[get_session_factory] = lambda: sessions
    app.dependency_overrides[connections_api.get_http] = lambda: fake.http()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


async def _create(api: httpx.AsyncClient, **overrides: Any) -> dict[str, Any]:
    response = await api.post("/api/v1/connections/binance", json=BODY | overrides, headers=OWNER)
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


async def _count(session: AsyncSession, table: str) -> int:
    return await session.scalar(text(f"select count(*) from {table}")) or 0


def _queued(queue) -> list[str]:
    return [job["task_name"] for job in queue.jobs.values()]


def _pem(passphrase: bytes | None = None) -> str:
    encryption = (
        serialization.BestAvailableEncryption(passphrase)
        if passphrase
        else serialization.NoEncryption()
    )
    return (
        Ed25519PrivateKey.generate()
        .private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, encryption)
        .decode()
    )


async def test_create_runs_the_gate_first(api, session, fake, queue) -> None:
    fake.permissions["enableWithdrawals"] = True

    response = await api.post("/api/v1/connections/binance", json=BODY, headers=OWNER)

    assert response.status_code == 422
    assert response.json()["detail"]["message"] == (
        "this key can do more than read: enableWithdrawals"
    )
    # The clock is read without the key; the gate is the only request that carries it.
    assert [seen.label for seen in fake.requests] == ["time", "api-restrictions"]
    assert await _count(session, "core.connections") == 0
    assert await _count(session, "core.credentials") == 0
    assert _queued(queue) == []


async def test_create_makes_a_healthy_connection_and_queues_a_sync(
    api, session, fake, queue
) -> None:
    created = await _create(api)

    assert (created["connector"], created["status"]) == ("binance", "healthy")
    assert created["binance"] == {
        "wallets": [],
        "first_sync_done": False,
        "differences": [],
        "flags": [],
    }
    assert [seen.label for seen in fake.requests] == ["time", "api-restrictions", "spot-account"]
    record = await get_connection(session, created["id"])
    assert record.config == {"key_type": "hmac", "uid": str(UID)}
    stored = await load_credential(session, KEY, created["id"], "api_secret")
    assert stored is not None and stored.reveal() == API_SECRET
    assert _queued(queue) == ["sync_connection"]


async def test_create_stores_no_secret_in_audit_or_response(api, session) -> None:
    passphrase = "pp-TEST-do-not-log-1112"
    pem = _pem(passphrase.encode())
    hmac_response = await _create(api)
    ed_response = await api.post(
        "/api/v1/connections/binance",
        json={
            "display_name": "Ed",
            "key_type": "ed25519",
            "api_key": "ak-TEST-other-9999",
            "private_key": pem,
            "private_key_passphrase": passphrase,
        },
        headers=OWNER,
    )

    # The same account twice is a conflict; the secrets must stay out of that answer too.
    assert ed_response.status_code == 409
    logged = await _logged(session)
    secrets = (API_KEY, API_SECRET, passphrase, "ak-TEST-other-9999", pem, pem.splitlines()[1])
    for secret in secrets:
        assert secret not in logged
        assert secret not in str(hmac_response)
        assert secret not in ed_response.text
    args = await session.scalar(
        text("select args from core.audit_log where command = 'create_binance_connection'")
    )
    assert args == {"display_name": "My Binance", "key_type": "hmac"}


async def test_create_with_an_ed25519_key(api, session, fake) -> None:
    pem = _pem(b"pp-TEST")

    created = await _create(
        api,
        key_type="ed25519",
        api_secret=None,
        private_key=pem,
        private_key_passphrase="pp-TEST",
    )

    record = await get_connection(session, created["id"])
    assert record.config["key_type"] == "ed25519"
    stored = await load_credential(session, KEY, created["id"], "private_key")
    assert stored is not None and stored.reveal() == pem
    assert await load_credential(session, KEY, created["id"], "api_secret") is None
    assert "pp-TEST" not in await _logged(session)


@pytest.mark.parametrize(
    "overrides",
    [
        {"api_secret": None},
        {"api_secret": ""},
        {"private_key": "x"},
        {"private_key_passphrase": "pp"},
        {"key_type": "ed25519", "api_secret": None},
        {"key_type": "ed25519", "private_key": "x"},
        {"key_type": "ed25519", "api_secret": "x", "private_key": "x"},
        {"api_key": ""},
    ],
)
async def test_create_refuses_a_body_that_does_not_fit_its_key_type(
    api, session, fake, overrides
) -> None:
    response = await api.post("/api/v1/connections/binance", json=BODY | overrides, headers=OWNER)

    assert response.status_code == 422
    assert fake.requests == []
    assert await _count(session, "core.connections") == 0
    logged_back = response.text
    for secret in (API_KEY, API_SECRET):
        assert secret not in logged_back


@pytest.mark.parametrize("missing", ["display_name", "key_type", "api_key"])
async def test_a_malformed_body_does_not_echo_the_key(api, fake, missing) -> None:
    body = {**BODY, "private_key": "pk-TEST-echo-3141"}
    del body[missing]

    for response in (
        await api.post("/api/v1/connections/binance", json=body, headers=OWNER),
        await api.put(f"/api/v1/connections/{UID_ID}/binance", json=body, headers=OWNER),
    ):
        assert response.status_code == 422
        for secret in (API_KEY, API_SECRET, "pk-TEST-echo-3141"):
            assert secret not in response.text
    assert fake.requests == []


async def test_create_refuses_a_private_key_that_is_not_ed25519_pem(api, session, fake) -> None:
    junk = "-----BEGIN PRIVATE KEY-----\nTUVTU0FHRQ==\n-----END PRIVATE KEY-----"

    response = await api.post(
        "/api/v1/connections/binance",
        json={
            "display_name": "Ed",
            "key_type": "ed25519",
            "api_key": API_KEY,
            "private_key": junk,
        },
        headers=OWNER,
    )

    assert response.status_code == 422
    assert response.json()["detail"]["message"] == "the Ed25519 private key can't be read"
    assert "TUVTU0FHRQ" not in response.text and API_KEY not in response.text
    assert fake.requests == []
    assert await _count(session, "core.connections") == 0


async def test_create_refuses_a_key_binance_rejects(api, session, fake) -> None:
    fake.fail("api-restrictions", status=401, code=-2015)

    response = await api.post("/api/v1/connections/binance", json=BODY, headers=OWNER)

    assert response.status_code == 422
    assert response.json()["detail"]["message"] == "Binance rejected the key (code -2015)"
    assert await _count(session, "core.connections") == 0


@pytest.mark.parametrize("label", ["time", "api-restrictions", "spot-account"])
async def test_create_says_so_when_binance_fails(api, session, fake, label) -> None:
    fake.fail(label, status=503)

    response = await api.post("/api/v1/connections/binance", json=BODY, headers=OWNER)

    assert response.status_code == 502
    assert "nothing was saved" in response.json()["detail"]["message"]
    assert await _count(session, "core.connections") == 0


async def test_create_says_so_when_binance_is_banned(api, session, fake) -> None:
    fake.fail("time", status=418)

    response = await api.post("/api/v1/connections/binance", json=BODY, headers=OWNER)

    assert response.status_code == 502
    assert await _count(session, "core.connections") == 0


async def test_second_connection_for_the_same_uid_is_409(api, session, queue) -> None:
    await _create(api)

    response = await api.post(
        "/api/v1/connections/binance", json=BODY | {"display_name": "Again"}, headers=OWNER
    )

    assert response.status_code == 409
    assert response.json()["detail"]["message"] == "this Binance account is already connected"
    assert await _count(session, "core.connections") == 1
    assert _queued(queue) == ["sync_connection"]


async def test_update_restores_health(api, session, run, fake, queue) -> None:
    created = await _create(api)
    queue.jobs.clear()
    await run(set_status, created["id"], "needs_reauth", error="Binance rejected the key")

    response = await api.put(
        f"/api/v1/connections/{created['id']}/binance",
        json=HMAC | {"api_key": "ak-TEST-new-5555", "api_secret": OTHER_SECRET},
        headers=OWNER,
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert (body["status"], body["last_error"]) == ("healthy", None)
    assert [seen.label for seen in fake.requests][-3:] == [
        "time",
        "api-restrictions",
        "spot-account",
    ]
    key = await load_credential(session, KEY, created["id"], "api_key")
    secret = await load_credential(session, KEY, created["id"], "api_secret")
    assert key is not None and key.reveal() == "ak-TEST-new-5555"
    assert secret is not None and secret.reveal() == OTHER_SECRET
    assert _queued(queue) == ["sync_connection"]
    logged = await _logged(session)
    for value in ("ak-TEST-new-5555", OTHER_SECRET):
        assert value not in logged and value not in response.text


async def test_the_sync_an_update_queues_runs_within_the_four_hours(
    api, session, run, fake, queue, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = Clock(NOW_MS)
    installed = BinanceConnector(clock=clock, sleep=clock.sleep)
    monkeypatch.setitem(finance.MODULE.connectors, "binance", installed)  # pyright: ignore[reportArgumentType]
    created = await _create(api)
    # A sync has just succeeded: the next one is not due for four hours.
    await session.execute(
        text("update core.connections set cursor = cast(:c as jsonb) where id = :id"),
        {"c": f'{{"fetched_at": {NOW_MS}}}', "id": created["id"]},
    )
    await run(set_status, created["id"], "needs_reauth", error="Binance rejected the key")
    response = await api.put(
        f"/api/v1/connections/{created['id']}/binance", json=BODY, headers=OWNER
    )
    assert response.status_code == 200, response.text
    fake.requests.clear()

    after = await run_binance_sync(session, created["id"], fake.handler)

    assert after.status == "healthy", after.last_error
    assert "spot-account" in [seen.label for seen in fake.requests]


async def test_update_can_switch_the_key_type(api, session, run) -> None:
    created = await _create(api)
    pem = _pem()

    response = await api.put(
        f"/api/v1/connections/{created['id']}/binance",
        json={"key_type": "ed25519", "api_key": "ak-TEST-ed-6666", "private_key": pem},
        headers=OWNER,
    )

    assert response.status_code == 200, response.text
    assert (await get_connection(session, created["id"])).config["key_type"] == "ed25519"
    assert await load_credential(session, KEY, created["id"], "api_secret") is None
    stored = await load_credential(session, KEY, created["id"], "private_key")
    assert stored is not None and stored.reveal() == pem


async def test_update_refuses_another_account(api, session, run, fake, queue) -> None:
    created = await _create(api)
    queue.jobs.clear()
    await run(set_status, created["id"], "needs_reauth", error="Binance rejected the key")
    # The key answers for the fake's account; the connection is of another.
    await run(update_config, created["id"], {"uid": "999999"})

    response = await api.put(
        f"/api/v1/connections/{created['id']}/binance",
        json=HMAC | {"api_key": "ak-TEST-new-5555", "api_secret": OTHER_SECRET},
        headers=OWNER,
    )

    assert response.status_code == 422
    assert "another Binance account" in response.json()["detail"]["message"]
    key = await load_credential(session, KEY, created["id"], "api_key")
    assert key is not None and key.reveal() == API_KEY
    record = await get_connection(session, created["id"])
    assert record.status == "needs_reauth"
    assert _queued(queue) == []


async def test_update_refuses_a_key_that_can_do_more_than_read(
    api, session, run, fake, queue
) -> None:
    created = await _create(api)
    queue.jobs.clear()
    await run(set_status, created["id"], "needs_reauth", error="Binance rejected the key")
    fake.requests.clear()
    fake.permissions["enableSpotAndMarginTrading"] = True

    response = await api.put(
        f"/api/v1/connections/{created['id']}/binance",
        json=HMAC | {"api_key": "ak-TEST-new-5555", "api_secret": OTHER_SECRET},
        headers=OWNER,
    )

    assert response.status_code == 422
    assert "can do more than read" in response.json()["detail"]["message"]
    assert [seen.label for seen in fake.requests] == ["time", "api-restrictions"]
    key = await load_credential(session, KEY, created["id"], "api_key")
    secret = await load_credential(session, KEY, created["id"], "api_secret")
    assert key is not None and key.reveal() == API_KEY
    assert secret is not None and secret.reveal() == API_SECRET
    assert (await get_connection(session, created["id"])).status == "needs_reauth"
    assert _queued(queue) == []


async def test_update_of_a_failing_connection_restores_health_and_the_failure_count(
    api, session, run
) -> None:
    created = await _create(api)
    for _ in range(3):
        await run(record_sync_failure, created["id"], "Binance request failed")
    await run(set_status, created["id"], "failing", error="Binance request failed")
    assert (await get_connection(session, created["id"])).consecutive_failures == 3

    response = await api.put(
        f"/api/v1/connections/{created['id']}/binance", json=BODY, headers=OWNER
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert (body["status"], body["last_error"], body["consecutive_failures"]) == (
        "healthy",
        None,
        0,
    )
    assert (await get_connection(session, created["id"])).consecutive_failures == 0


@pytest.mark.parametrize("label", ["time", "api-restrictions", "spot-account"])
async def test_a_rate_limited_check_answers_at_once(api, session, fake, label) -> None:
    fake.fail(label, status=429, retry_after=60)

    response = await api.post("/api/v1/connections/binance", json=BODY, headers=OWNER)

    assert response.status_code == 502
    assert response.json()["detail"]["error"] == "binance_unavailable"
    assert len(fake.calls(label)) == 1
    assert await _count(session, "core.connections") == 0


async def test_update_refuses_other_connectors_and_disabled_connections(api, session, run) -> None:
    created = await _create(api)
    other = await run(
        connections_api.create_connection,
        connector="monzo",
        display_name="M",
        config={},
    )
    not_binance = await api.put(f"/api/v1/connections/{other.id}/binance", json=BODY, headers=OWNER)
    await run(set_status, created["id"], "disabled")
    disabled = await api.put(
        f"/api/v1/connections/{created['id']}/binance", json=BODY, headers=OWNER
    )

    assert not_binance.status_code == 422
    assert disabled.status_code == 422


async def test_list_shows_differences_and_flags(api, session, run, sessions) -> None:
    connection = await binance_connection(run)
    spot = await run(wallet_account, connection, "spot")
    await run(wallet_account, connection, "funding")
    aaa = await run(
        create_instrument,
        kind="crypto",
        symbol="AAA",
        name="Alpha",
        decimals=8,
        quote_instrument_id=None,
        identifiers=[],
    )
    closed = await run(
        create_instrument,
        kind="crypto",
        symbol="BBB",
        name="Beta",
        decimals=8,
        quote_instrument_id=None,
        identifiers=[],
    )
    for instrument, closed_at in ((aaa.id, None), (closed.id, datetime(2026, 11, 20, tzinfo=UTC))):
        await session.execute(
            text(
                "insert into finance.binance_reconciliation_differences"
                " (account_id, instrument_id, from_at, to_at, reported_change, posted_sum,"
                " opened_at, closed_at)"
                " values (:a, :i, :f, :t, 5, 0, :o, :c)"
            ),
            {
                "a": spot,
                "i": instrument,
                "f": datetime(2026, 11, 1, tzinfo=UTC),
                "t": datetime(2026, 11, 2, tzinfo=UTC),
                "o": datetime(2026, 11, 3, tzinfo=UTC),
                "c": closed_at,
            },
        )
    await run(
        update_config,
        connection.id,
        {
            "flags": ["flag one", "flag two"],
            "first_sync_done": True,
            "pending_flags": ["internal"],
            "not_present": {"x": 1},
            "cursors": {},
        },
    )

    [listed] = (await api.get("/api/v1/connections", headers=OWNER)).json()

    state = listed["binance"]
    assert state == {
        "wallets": ["Funding", "Spot"],
        "first_sync_done": True,
        "differences": [
            {
                "account_id": str(spot),
                "symbol": "AAA",
                "from_at": "2026-11-01T00:00:00Z",
                "to_at": "2026-11-02T00:00:00Z",
            }
        ],
        "flags": ["flag one", "flag two"],
    }


async def test_list_has_no_binance_state_for_other_connectors(api, run) -> None:
    await run(connections_api.create_connection, connector="monzo", display_name="M", config={})

    [listed] = (await api.get("/api/v1/connections", headers=OWNER)).json()

    assert listed["binance"] is None


async def test_register_chain_address(api, run) -> None:
    for key in ("unassigned", "in_transit", "opening_balances"):
        await run(get_or_create_system_account, key)
    account = await run(
        create_account,
        name="Cold wallet",
        ledger_type="asset",
        subtype="other",
        balance_mode="hybrid",
    )
    url = f"{FINANCE}/accounts/{account.id}/identifiers"
    mixed = "0x" + "AbCdEf" * 6 + "1234"

    created = await api.post(url, json={"kind": "chain_address", "value": mixed}, headers=OWNER)

    assert created.status_code == 201, created.text
    body = created.json()
    assert (body["kind"], body["value"], body["account_id"]) == (
        "chain_address",
        mixed.lower(),
        str(account.id),
    )
    assert body["sort_code"] is None and body["account_number"] is None

    bad = "not an address!"
    refused = await api.post(url, json={"kind": "chain_address", "value": bad}, headers=OWNER)

    assert refused.status_code == 422
    assert refused.json()["detail"]["message"]
    assert bad not in refused.text


SECRET_VALUE = "sv-TEST-echo-2718"


async def _ibkr_connection(api) -> str:
    """Ids of an IBKR connection (for its update route)."""
    created = await api.post(
        "/api/v1/connections/ibkr",
        json={
            "display_name": "I",
            "token": "tok-TEST-1",
            "token_expires_on": "2099-01-01",
            "query_id": "123456",
        },
        headers=OWNER,
    )
    return created.json()["id"]


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("post", "/api/v1/connections", {"display_name": "M", "client_secret": SECRET_VALUE}),
        (
            "post",
            "/api/v1/connections/ibkr",
            {"display_name": "I", "token": SECRET_VALUE, "token_expires_on": "2099-01-01"},
        ),
        ("put", "/api/v1/connections/{id}/ibkr", {"token": SECRET_VALUE}),
        (
            "post",
            "/api/v1/connections/binance",
            {"display_name": "B", "key_type": "hmac", "api_secret": SECRET_VALUE},
        ),
        (
            "put",
            "/api/v1/connections/{id}/binance",
            {"key_type": "hmac", "api_secret": SECRET_VALUE},
        ),
    ],
)
async def test_a_validation_error_never_echoes_the_body(api, method, path, body) -> None:
    connection_id = await _ibkr_connection(api)

    response = await api.request(
        method.upper(), path.replace("{id}", connection_id), json=body, headers=OWNER
    )

    assert response.status_code == 422
    assert SECRET_VALUE not in response.text
    errors = response.json()["detail"]
    assert errors and all({"loc", "msg", "type"} <= set(error) for error in errors)
    assert all("input" not in error and "ctx" not in error for error in errors)


def test_every_route_of_the_connections_routers_hides_the_body() -> None:
    routes = [*connections_api.router.routes, *connections_api.callback_router.routes]
    assert routes
    assert all(isinstance(route, connections_api.BodylessErrorRoute) for route in routes)
