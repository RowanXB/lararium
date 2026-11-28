"""/api/v1/connections: connecting a Bitcoin wallet and previewing its first address."""

import base64
import json
import logging
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from procrastinate.testing import InMemoryConnector
from pydantic import SecretStr
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from lararium.api.app import create_app
from lararium.config import Settings
from lararium.core.actors import Actor
from lararium.core.commands import command_context
from lararium.core.connections import (
    SYNC_REQUESTED_KEY,
    create_connection,
    get_connection,
    load_credential,
    set_cursor,
    set_warnings,
)
from lararium.deps import get_session
from lararium.modules.finance.bitcoin.derive import build_wallet
from lararium.modules.finance.bitcoin.keys import WalletInput, parse_input
from lararium.modules.finance.bitcoin.sync import DESCRIPTOR_PURPOSE, input_json, load_input
from lararium.worker.app import procrastinate_app

from ..finance.bitcoin.fixtures import CHANGE_0, RECEIVING_0

KEY = bytes(range(32))
OWNER = {"Tailscale-User-Login": "me@example.com"}
CREATE = "/api/v1/connections/bitcoin"
PREVIEW = "/api/v1/connections/bitcoin/preview"
# BIP84's account key (m/84'/0'/0' of "abandon ... about"), as Electrum shows it, and as an
# xpub in a descriptor: one wallet either way.
ZPUB = (
    "zpub6rFR7y4Q2AijBEqTUquhVz398htDFrtymD9xYYfG1m4wAcvPhXNfE3EfH1r1ADqtfSdVCToUG868RvUUkgDKf31mGDt"
    "KsAYz2oz2AGutZYs"
)
ZPUB_AS_XPUB = (
    "xpub6CatWdiZiodmUeTDp8LT5or8nmbKNcuyvz7WyksVFkKB4RHwCD3XyuvPEbvqAQY3rAPshWcMLoP2fMFMKHPJ4ZeZXY"
    "VUhLv1VMrjPC7PW6V"
)
DESCRIPTOR = f"wpkh({ZPUB_AS_XPUB}/<0;1>/*)"
ELECTRUM = {
    "display_name": "Electrum",
    "form": "electrum",
    "electrum_type": "single",
    "keys": [ZPUB],
}
# BIP39's published test mnemonic (vector 3 of the reference implementation).
SEED = "legal winner thank year wave sausage worth useful legal winner thank yellow"
SEED_MESSAGE = (
    "This looks like a seed phrase. Never enter it anywhere: "
    "Lararium needs the master public key from Wallet → Information."
)
GAP_INVALID = {"error": "gap_invalid", "message": "A gap limit is not valid."}


@pytest.fixture
def queue() -> Any:
    with procrastinate_app.replace_connector(InMemoryConnector()) as app:
        yield app.connector


@pytest.fixture
async def api(session: AsyncSession, tmp_path: Path, queue) -> AsyncIterator[httpx.AsyncClient]:
    settings = Settings(
        database_url="postgresql+psycopg://unused@localhost/unused",
        auth_mode="tailscale",
        owner_login="me@example.com",
        master_key=SecretStr(base64.b64encode(KEY).decode()),
        document_dir=tmp_path,
        esplora_url="https://user:pw@esplora.example:3002/api?token=x",
    )
    app = create_app(settings)

    async def _test_session() -> AsyncIterator[AsyncSession]:
        yield session

    app.dependency_overrides[get_session] = _test_session
    # Nothing here may reach a server: any request fails the test.
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


async def _create(api: httpx.AsyncClient, **overrides: Any) -> dict[str, Any]:
    response = await api.post(CREATE, json=ELECTRUM | overrides, headers=OWNER)
    assert response.status_code == 201, response.text
    return response.json()


async def _count(session: AsyncSession, table: str) -> int:
    return await session.scalar(text(f"select count(*) from {table}")) or 0


async def _stored_text(session: AsyncSession) -> str:
    """Everything the audit log, the events and the raw records hold, as text."""
    stored = await session.scalar(
        text(
            "select coalesce((select string_agg(args::text || changes::text, ' ')"
            " from core.audit_log), '')"
            " || coalesce((select string_agg(data::text, ' ') from core.events), '')"
            " || coalesce((select string_agg(r::text, ' ') from core.raw_records r), '')"
        )
    )
    return stored or ""


def _queued(queue) -> list[str]:
    return [job["task_name"] for job in queue.jobs.values()]


async def test_create_and_sync_is_queued(api, session, queue) -> None:
    created = await _create(api)

    assert (created["connector"], created["status"]) == ("bitcoin", "healthy")
    assert created["bitcoin"] == {
        "form": "electrum",
        "electrum_type": "single",
        "scanned": {"receiving": 0, "change": 0},
        "used": {"receiving": 0, "change": 0},
        "last_check": None,
        "unconfirmed": 0,
        "server": "esplora.example:3002",
        "gaps": {},
    }
    spec = parse_input(WalletInput("electrum", "single", (ZPUB,)))
    record = await get_connection(session, created["id"])
    assert record.config == {
        "form": "electrum",
        "electrum_type": "single",
        "fingerprint": build_wallet(spec).fingerprint,
    }
    stored = await load_credential(session, KEY, created["id"], DESCRIPTOR_PURPOSE)
    assert stored is not None and stored.reveal() == input_json(spec)
    assert _queued(queue) == ["sync_connection"]
    args = await session.scalar(
        text("select args from core.audit_log where command = 'create_bitcoin_connection'")
    )
    assert args == {"display_name": "Electrum", "form": "electrum", "electrum_type": "single"}
    assert ZPUB not in await _stored_text(session)


async def test_create_stores_valid_gap_limits(api, session) -> None:
    created = await _create(api, gaps={"d0": {"0": 50, "1": 1000}})

    record = await get_connection(session, created["id"])
    assert record.config["gaps"] == {"d0": {"0": 50, "1": 1000}}
    assert created["bitcoin"]["gaps"] == {"d0": {"0": 50, "1": 1000}}


@pytest.mark.parametrize(
    ("body", "gaps"),
    [
        (ELECTRUM, {"d1": {"0": 30}}),
        (ELECTRUM, {"d0": {"2": 30}}),
        (ELECTRUM, {"d0": {"0": 0}}),
        (ELECTRUM, {"d0": {"0": 1001}}),
        (ELECTRUM, {"d0": {"0": "30"}}),
        (ELECTRUM, {"d0": {"0": True}}),
        (ELECTRUM, {"d0": {"0": 2.5}}),
        (ELECTRUM, {"d0": {}}),
        # A single-branch descriptor has no change branch.
        (
            {
                "display_name": "D",
                "form": "descriptor",
                "descriptors": [f"wpkh({ZPUB_AS_XPUB}/0/*)"],
            },
            {"d0": {"1": 30}},
        ),
        (
            {"display_name": "L", "form": "addresses", "addresses": [RECEIVING_0]},
            {"list": {"0": 5}},
        ),
    ],
)
async def test_an_invalid_gap_limit_is_422(api, session, queue, body, gaps) -> None:
    response = await api.post(CREATE, json=body | {"gaps": gaps}, headers=OWNER)

    assert response.status_code == 422, response.text
    assert response.json()["detail"] == GAP_INVALID
    assert await _count(session, "core.connections") == 0
    assert _queued(queue) == []


async def test_duplicate_wallet_is_409(api, session, queue) -> None:
    await _create(api)
    queue.jobs.clear()

    # The same wallet as a descriptor of the xpub form of the key: one fingerprint.
    response = await api.post(
        CREATE,
        json={"display_name": "Sparrow", "form": "descriptor", "descriptors": [DESCRIPTOR]},
        headers=OWNER,
    )

    assert response.status_code == 409
    assert response.json()["detail"] == {
        "error": "already_connected",
        "message": "this wallet is already connected",
    }
    assert ZPUB_AS_XPUB not in response.text
    assert await _count(session, "core.connections") == 1
    assert _queued(queue) == []


async def test_adding_a_disabled_wallet_again_brings_it_back(api, session, queue) -> None:
    """A disabled wallet has no key to replace: adding it again is how it comes back, as the
    same connection (its account and ledger kept), healthy, with a sync queued."""
    created = await _create(api)
    disabled = await api.post(f"/api/v1/connections/{created['id']}/disable", headers=OWNER)
    assert disabled.status_code == 200
    queue.jobs.clear()

    response = await api.post(
        CREATE,
        json={
            "display_name": "Sparrow",
            "form": "descriptor",
            "descriptors": [DESCRIPTOR],
            "gaps": {"d0": {"0": 30}},
        },
        headers=OWNER,
    )

    assert response.status_code == 201
    back = response.json()
    assert back["id"] == created["id"]
    assert back["status"] == "healthy"
    assert back["bitcoin"]["gaps"] == {"d0": {"0": 30}}
    # The stored input stands: the wallet was given as an Electrum key, and is still.
    assert back["bitcoin"]["form"] == "electrum"
    assert back["display_name"] == "Electrum"
    assert await _count(session, "core.connections") == 1
    assert _queued(queue) == ["sync_connection"]
    # Healthy now, so a second add is the duplicate again.
    again = await api.post(CREATE, json={"display_name": "Again", **ELECTRUM}, headers=OWNER)
    assert again.status_code == 409


async def test_a_restore_keeps_the_stored_descriptor_order(api, session) -> None:
    """Two descriptors in another order are the same wallet (one fingerprint), but their
    order names the sources (d0, d1) under the stored addresses: a restore keeps the order
    the wallet was connected with."""
    first, second = f"wpkh({ZPUB_AS_XPUB}/<0;1>/*)", f"pkh({ZPUB_AS_XPUB}/<0;1>/*)"
    created = await _create(
        api, display_name="Two", form="descriptor", descriptors=[first, second], keys=[]
    )
    await api.post(f"/api/v1/connections/{created['id']}/disable", headers=OWNER)

    response = await api.post(
        CREATE,
        json={"display_name": "Two", "form": "descriptor", "descriptors": [second, first]},
        headers=OWNER,
    )

    assert response.status_code == 201
    assert response.json()["id"] == created["id"]
    stored = await load_credential(session, KEY, created["id"], DESCRIPTOR_PURPOSE)
    assert stored is not None
    assert load_input(stored.reveal()).descriptors == (first, second)


@pytest.mark.parametrize("path", [CREATE, PREVIEW])
@pytest.mark.parametrize(
    "fields",
    [
        {"form": "electrum", "electrum_type": "single", "keys": [SEED]},
        {"form": "descriptor", "descriptors": [SEED]},
        {"form": "addresses", "addresses": [SEED]},
    ],
)
async def test_seed_phrase_422_does_not_echo(api, session, caplog, queue, path, fields) -> None:
    caplog.set_level(logging.DEBUG)

    response = await api.post(path, json={"display_name": "W"} | fields, headers=OWNER)

    assert response.status_code == 422
    assert response.json()["detail"] == {"error": "private_material", "message": SEED_MESSAGE}
    stored = await _stored_text(session)
    for word in set(SEED.split()):
        assert word not in response.text
        assert word not in caplog.text
        assert word not in stored
    assert await _count(session, "core.connections") == 0
    assert await _count(session, "core.credentials") == 0
    assert _queued(queue) == []


@pytest.mark.parametrize(
    ("fields", "error", "message"),
    [
        (
            {"form": "electrum", "electrum_type": "single", "keys": ["tpub" + "Z" * 107]},
            "unsupported_network",
            "Only Bitcoin mainnet wallets are supported.",
        ),
        (
            {"form": "electrum", "electrum_type": "multisig", "keys": [ZPUB]},
            "wallet_invalid",
            "A multisig wallet takes at least two master public keys.",
        ),
        (
            {"form": "descriptor", "descriptors": [f"wpkh({ZPUB_AS_XPUB}/0/*)#00000000"]},
            "wallet_invalid",
            "The descriptor could not be read.",
        ),
    ],
)
@pytest.mark.parametrize("path", [CREATE, PREVIEW])
async def test_a_refused_wallet_is_422_with_the_fixed_message(
    api, session, path, fields, error, message
) -> None:
    response = await api.post(path, json={"display_name": "W"} | fields, headers=OWNER)

    assert response.status_code == 422
    assert response.json()["detail"] == {"error": error, "message": message}
    assert ZPUB not in response.text and ZPUB_AS_XPUB not in response.text
    assert await _count(session, "core.connections") == 0


@pytest.mark.parametrize(
    ("fields", "message"),
    [
        (
            {"form": "electrum", "electrum_type": "multisig", "keys": [ZPUB] * 21, "threshold": 2},
            "A wallet takes at most 20 master public keys.",
        ),
        (
            {"form": "descriptor", "descriptors": [DESCRIPTOR] * 3},
            "A wallet takes at most 2 output descriptors.",
        ),
        (
            {"form": "addresses", "addresses": [RECEIVING_0] * 10_001},
            "An address list takes at most 10,000 addresses.",
        ),
        (
            {"form": "electrum", "electrum_type": "single", "keys": [ZPUB * 10]},
            "A key, descriptor or address is too long.",
        ),
        (
            {"form": "addresses", "addresses": [RECEIVING_0 * 24]},
            "A key, descriptor or address is too long.",
        ),
        (
            {"form": "descriptor", "descriptors": [DESCRIPTOR * 90]},
            "A key, descriptor or address is too long.",
        ),
    ],
)
@pytest.mark.parametrize("path", [CREATE, PREVIEW])
async def test_an_input_too_large_is_422_without_echo(api, session, path, fields, message) -> None:
    response = await api.post(path, json={"display_name": "W"} | fields, headers=OWNER)

    assert response.status_code == 422
    assert response.json()["detail"] == {"error": "wallet_invalid", "message": message}
    for secret in (ZPUB, ZPUB_AS_XPUB, RECEIVING_0):
        assert secret not in response.text
    assert await _count(session, "core.connections") == 0


async def test_a_malformed_body_does_not_echo_the_key(api) -> None:
    response = await api.post(
        CREATE, json=ELECTRUM | {"threshold": "two", "display_name": ""}, headers=OWNER
    )

    assert response.status_code == 422
    assert ZPUB not in response.text


async def test_preview_derives_bip84_vector(api, session) -> None:
    response = await api.post(
        PREVIEW, json={"form": "electrum", "electrum_type": "single", "keys": [ZPUB]}, headers=OWNER
    )

    assert response.status_code == 200, response.text
    assert response.json() == {
        "first_receiving_address": RECEIVING_0,
        "script_type": "p2wpkh",
    }
    for table in ("core.connections", "core.credentials", "core.audit_log", "core.events"):
        assert await _count(session, table) == 0


async def test_preview_of_an_address_list_is_its_first_address(api) -> None:
    response = await api.post(
        PREVIEW, json={"form": "addresses", "addresses": [CHANGE_0, RECEIVING_0]}, headers=OWNER
    )

    assert response.status_code == 200, response.text
    assert response.json() == {"first_receiving_address": CHANGE_0, "script_type": "p2wpkh"}


async def test_state_never_carries_the_key(api, session) -> None:
    created = await _create(api)
    connection_id = created["id"]
    rows = [
        ("d0", 0, 0, RECEIVING_0, True),
        ("d0", 0, 1, "bc1qnjg0jd8228aq7egyzacy8cys3knf9xvrerkf9g", False),
        ("d0", 1, 0, CHANGE_0, True),
    ]
    for source, branch, index, address, used in rows:
        await session.execute(
            text(
                "insert into finance.bitcoin_addresses"
                " (connection_id, source, branch, address_index, address, script_type,"
                "  first_used_at, last_used_at)"
                " values (:c, :s, :b, :i, :a, 'p2wpkh', :u, :u)"
            ),
            {
                "c": connection_id,
                "s": source,
                "b": branch,
                "i": index,
                "a": address,
                "u": "2026-01-01T00:00:00Z" if used else None,
            },
        )
    # Waiting in the mempool, confirmed, and one the server no longer knows.
    for txid, confirmed, seen in (
        ("aa" * 32, False, 900),
        ("bb" * 32, True, 900),
        ("cc" * 32, False, None),
    ):
        await session.execute(
            text(
                "insert into finance.bitcoin_transactions"
                " (connection_id, txid, confirmed, seen_at_height)"
                " values (:c, :t, :confirmed, :seen)"
            ),
            {"c": connection_id, "t": txid, "confirmed": confirmed, "seen": seen},
        )
    warning = "Electrum BTC differs from the Bitcoin blockchain on 2026-01-02 by 0.00001"
    async with command_context(session, Actor("system", "t"), "test", {}) as ctx:
        await set_cursor(ctx, connection_id, {"reconciliation": {"checked": [warning]}})
        await set_warnings(ctx, connection_id, [warning])

    listed = await api.get("/api/v1/connections", headers=OWNER)

    assert listed.status_code == 200
    (out,) = [c for c in listed.json() if c["id"] == connection_id]
    assert out["bitcoin"] == {
        "form": "electrum",
        "electrum_type": "single",
        "scanned": {"receiving": 2, "change": 1},
        "used": {"receiving": 1, "change": 1},
        "last_check": warning,
        "unconfirmed": 1,
        "server": "esplora.example:3002",
        "gaps": {},
    }
    for secret in (ZPUB, ZPUB_AS_XPUB, RECEIVING_0, CHANGE_0, "pw", "token"):
        assert secret not in json.dumps(out["bitcoin"])
    for secret in (ZPUB, ZPUB_AS_XPUB, RECEIVING_0, CHANGE_0):
        assert secret not in listed.text

    async with command_context(session, Actor("system", "t"), "test", {}) as ctx:
        await set_cursor(ctx, connection_id, {"reconciliation": {}})
    again = await api.get("/api/v1/connections", headers=OWNER)
    (out,) = [c for c in again.json() if c["id"] == connection_id]
    assert out["bitcoin"]["last_check"] == "agrees"


async def test_list_has_no_bitcoin_state_for_other_connectors(api, session) -> None:
    async with command_context(session, Actor("system", "t"), "test", {}) as ctx:
        await create_connection(ctx, connector="monzo", display_name="M", config={})

    listed = await api.get("/api/v1/connections", headers=OWNER)

    assert [c["bitcoin"] for c in listed.json()] == [None]


def _gaps_path(connection_id: str) -> str:
    return f"/api/v1/connections/{connection_id}/bitcoin/gaps"


async def test_a_raised_gap_is_stored_and_a_sync_is_requested(api, session, queue) -> None:
    created = await _create(api)
    queue.jobs.clear()

    response = await api.patch(
        _gaps_path(created["id"]), json={"gaps": {"d0": {"0": 60}}}, headers=OWNER
    )

    assert response.status_code == 200, response.text
    assert response.json()["bitcoin"]["gaps"] == {"d0": {"0": 60}}
    record = await get_connection(session, created["id"])
    assert record.config["gaps"] == {"d0": {"0": 60}}
    assert SYNC_REQUESTED_KEY in record.config
    assert _queued(queue) == ["sync_connection"]
    args = await session.scalar(
        text("select args from core.audit_log where command = 'update_bitcoin_gaps'")
    )
    assert args == {"connection_id": created["id"]}

    # An empty map restores the defaults.
    restored = await api.patch(_gaps_path(created["id"]), json={"gaps": {}}, headers=OWNER)
    assert restored.status_code == 200, restored.text
    assert "gaps" not in (await get_connection(session, created["id"])).config


@pytest.mark.parametrize("gaps", [{"d0": {"0": 0}}, {"d1": {"0": 30}}, {"d0": {"1": "x"}}])
async def test_a_bad_gap_update_is_422_and_stores_nothing(api, session, queue, gaps) -> None:
    created = await _create(api, gaps={"d0": {"0": 50}})
    queue.jobs.clear()
    before = await get_connection(session, created["id"])

    response = await api.patch(_gaps_path(created["id"]), json={"gaps": gaps}, headers=OWNER)

    assert response.status_code == 422
    assert response.json()["detail"] == GAP_INVALID
    assert (await get_connection(session, created["id"])).config == before.config
    assert _queued(queue) == []
    assert await _count(session, "core.audit_log where command = 'update_bitcoin_gaps'") == 0


async def test_a_gap_update_of_another_connector_is_404(api, session, queue) -> None:
    async with command_context(session, Actor("system", "t"), "test", {}) as ctx:
        other = await create_connection(ctx, connector="monzo", display_name="M", config={})

    for connection_id in (str(other.id), "00000000-0000-4000-8000-00000000dead"):
        response = await api.patch(
            _gaps_path(connection_id), json={"gaps": {"d0": {"0": 60}}}, headers=OWNER
        )
        assert response.status_code == 404, response.text
        assert response.json()["detail"]["error"] == "connection_not_found"
    assert (await get_connection(session, other.id)).config == {}
    assert _queued(queue) == []
