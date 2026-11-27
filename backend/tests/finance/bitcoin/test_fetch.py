"""The Bitcoin fetch: the gap-limited scan, histories only where an address changed, the
recheck of shallow transactions, raw records, typed rows and the wallet's identifiers."""

import json
import uuid
from datetime import UTC, date, datetime
from typing import Any, cast

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from lararium.core.actors import Actor
from lararium.core.commands import CommandContext
from lararium.core.connections import ConnectionRecord, create_connection, get_connection
from lararium.core.connectors import SyncContext
from lararium.modules.finance.accounts import get_account
from lararium.modules.finance.bitcoin import fetch as fetch_module
from lararium.modules.finance.bitcoin.accounts import ACCOUNT_KEY, wallet_account
from lararium.modules.finance.bitcoin.derive import Wallet, build_wallet
from lararium.modules.finance.bitcoin.fetch import (
    MAX_TIP_JUMP,
    TIP_KEY,
    VERSION,
    FetchResult,
    TooManyAddresses,
    fetch,
    mark_normalized,
)
from lararium.modules.finance.bitcoin.keys import WalletInput, parse_input
from lararium.modules.finance.bitcoin.parse import MalformedResponse
from lararium.modules.finance.identifiers import assign_identifier, list_identifiers
from lararium.modules.finance.readonly_http import HttpRetryableError

from .fake_esplora import BASE, STRANGER, TIP, FakeEsplora, block_hash, block_time, make_tx, txid

KEY = bytes(range(32))
CONNECTOR = Actor("connector", "bitcoin")
# BIP84's account key (m/84'/0'/0' of "abandon ... about"), as an Electrum single-key wallet.
ZPUB = (
    "zpub6rFR7y4Q2AijBEqTUquhVz398htDFrtymD9xYYfG1m4wAcvPhXNfE3EfH1r1ADqtfSdVCToUG868RvUUkgDKf31mGDt"
    "KsAYz2oz2AGutZYs"
)
SPEC = parse_input(WalletInput("electrum", "single", (ZPUB,)))
_ADDRESSES: dict[tuple[int, int], str] = {}


def address(branch: int, index: int) -> str:
    """The BIP84 wallet's address (derivation is slow, so each is derived once)."""
    if (branch, index) not in _ADDRESSES:
        _ADDRESSES[(branch, index)] = build_wallet(SPEC).address("d0", branch, index)[0]  # pyright: ignore[reportArgumentType]
    return _ADDRESSES[(branch, index)]


def receiving(index: int) -> str:
    return address(0, index)


def change(index: int) -> str:
    return address(1, index)


class _NoSession:
    """The sync's own session, which the fetch must never use: what it stores commits through
    `session_factory`, so a failed sync keeps it."""

    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"the fetch used the sync's session ({name})")


@pytest.fixture
def chain() -> FakeEsplora:
    return FakeEsplora()


async def _connection(run) -> ConnectionRecord:
    async def setup(ctx: CommandContext) -> ConnectionRecord:
        return await create_connection(
            ctx, connector="bitcoin", display_name="Bitcoin wallet", config={}
        )

    return await run(setup)


async def _fetch(
    session: AsyncSession,
    connection: ConnectionRecord,
    chain: FakeEsplora,
    wallet: Wallet,
    *,
    mark: bool = True,
) -> FetchResult:
    """One fetch; with `mark`, the rest of a sync that succeeds: the normalizer marks what
    it was given, in the sync's own session."""
    async with httpx.AsyncClient(transport=httpx.MockTransport(chain.handler)) as http:
        ctx = SyncContext(
            esplora_url=BASE,
            command=CommandContext(cast(AsyncSession, _NoSession()), CONNECTOR, uuid.uuid4()),
            connection=await get_connection(session, connection.id),
            sync_id=uuid.uuid4(),
            master_key=KEY,
            http=http,
            notifier=None,  # pyright: ignore[reportArgumentType]  # the fetch sends nothing
            session_factory=async_sessionmaker(
                bind=session.bind, join_transaction_mode="create_savepoint"
            ),
        )
        result = await fetch(ctx, chain.client(http), wallet)
    if mark:
        await mark_normalized(session, connection.id, result.changed_txids | result.left_chain)
    return result


async def _rows(session: AsyncSession, sql: str, **params: Any) -> list[Any]:
    return list((await session.execute(text(sql), params)).all())


async def _account_id(session: AsyncSession, connection: ConnectionRecord) -> uuid.UUID:
    found = (await get_connection(session, connection.id)).config[ACCOUNT_KEY]
    assert isinstance(found, str)
    return uuid.UUID(found)


def _receive(n: int, to: str, value: int = 50_000, height: int | None = TIP - 100) -> dict:
    return make_tx(n, [(to, value), (STRANGER, 7_000)], height=height)


async def test_scan_stops_after_gap(session, run, chain: FakeEsplora) -> None:
    for n, index in enumerate((0, 1, 25)):
        chain.add(_receive(n + 1, receiving(index)))
    connection = await _connection(run)

    result = await _fetch(session, connection, chain, build_wallet(SPEC))

    expected = [receiving(i) for i in range(22)] + [change(i) for i in range(10)]
    assert chain.asked_stats() == expected
    assert set(result.scanned) == set(expected)
    assert result.tip == TIP
    rows = await _rows(
        session,
        "select source, branch, address_index, address, script_type"
        " from finance.bitcoin_addresses where connection_id = :c",
        c=connection.id,
    )
    assert {(r.source, r.branch, r.address_index, r.address) for r in rows} == {
        ("d0", 0, i, receiving(i)) for i in range(22)
    } | {("d0", 1, i, change(i)) for i in range(10)}
    assert {r.script_type for r in rows} == {"p2wpkh"}
    account_id = await _account_id(session, connection)
    registered = await list_identifiers(session, account_id=account_id)
    assert {(i.kind, i.value) for i in registered} == {("chain_address", a) for a in expected}
    assert result.conflicts == []


async def test_raising_gap_finds_later_addresses(session, run, chain: FakeEsplora) -> None:
    chain.add(_receive(1, receiving(0)))
    # One transaction pays index 1 and index 25: until 25 is found, its output is a stranger's.
    shared = chain.add(make_tx(2, [(receiving(1), 10_000), (receiving(25), 20_000)]))
    connection = await _connection(run)
    await _fetch(session, connection, chain, build_wallet(SPEC))
    owns = "select vout, own from finance.bitcoin_outputs where txid = :t order by vout"
    assert [r.own for r in await _rows(session, owns, t=shared["txid"])] == [True, False]

    chain.requests.clear()
    result = await _fetch(session, connection, chain, build_wallet(SPEC, {"d0": {0: 30}}))

    assert receiving(25) in result.scanned
    assert chain.asked_stats()[:32] == [receiving(i) for i in range(32)]
    assert [r.own for r in await _rows(session, owns, t=shared["txid"])] == [True, True]
    assert shared["txid"] in result.changed_txids


async def test_history_only_for_changed_addresses(session, run, chain: FakeEsplora) -> None:
    chain.add(_receive(1, receiving(0)))
    # Address 1 has more than a page of deep history.
    for n in range(30):
        chain.add(_receive(100 + n, receiving(1), value=1_000 + n, height=TIP - 500 + n))
    connection = await _connection(run)
    first = await _fetch(session, connection, chain, build_wallet(SPEC))
    assert len(first.changed_txids) == 31
    paged = [p for p in chain.asked_txs() if "/txs/chain/" in p]
    assert len(paged) == 1 and receiving(1) in paged[0]

    chain.requests.clear()
    again = await _fetch(session, connection, chain, build_wallet(SPEC))
    assert chain.asked_txs() == []
    assert chain.asked_status() == []
    assert again.changed_txids == set()
    assert again.left_chain == set()

    chain.tip += 1
    new = chain.add(_receive(200, receiving(1), height=chain.tip))
    chain.requests.clear()
    third = await _fetch(session, connection, chain, build_wallet(SPEC))

    assert chain.asked_txs() == [f"/address/{receiving(1)}/txs"]
    assert third.changed_txids == {new["txid"]}


async def test_recheck_detects_a_dropped_transaction(session, run, chain: FakeEsplora) -> None:
    unconfirmed = chain.add(_receive(1, receiving(0), height=TIP - 1))  # 2 confirmations
    dropped = chain.add(_receive(2, receiving(1), height=TIP - 1))
    deep = chain.add(_receive(3, receiving(2), height=TIP - 5))  # 6 confirmations
    moved = chain.add(_receive(4, receiving(3), height=TIP))
    connection = await _connection(run)
    await _fetch(session, connection, chain, build_wallet(SPEC))

    chain.move(unconfirmed["txid"], None)
    chain.drop(dropped["txid"])
    chain.move(moved["txid"], TIP)  # the same height, another block
    chain.requests.clear()
    result = await _fetch(session, connection, chain, build_wallet(SPEC))

    assert result.left_chain == {unconfirmed["txid"], dropped["txid"]}
    assert deep["txid"] not in chain.asked_status()
    assert set(chain.asked_status()) == {unconfirmed["txid"], dropped["txid"], moved["txid"]}
    assert moved["txid"] in result.changed_txids
    rows = {
        r.txid: r
        for r in await _rows(
            session,
            "select txid, confirmed, block_height, block_hash, seen_at_height"
            " from finance.bitcoin_transactions where connection_id = :c",
            c=connection.id,
        )
    }
    assert rows[moved["txid"]].block_hash == block_hash(TIP, 1)
    assert rows[unconfirmed["txid"]].confirmed is False
    assert rows[unconfirmed["txid"]].block_height is None
    assert rows[dropped["txid"]].seen_at_height is None

    # Once gone, a transaction is not asked about again; one in the mempool still is.
    chain.requests.clear()
    later = await _fetch(session, connection, chain, build_wallet(SPEC))
    assert set(chain.asked_status()) == {unconfirmed["txid"], moved["txid"]}
    assert later.left_chain == set()


async def test_every_response_is_a_raw_record(session, run, chain: FakeEsplora) -> None:
    chain.add(_receive(1, receiving(0), height=TIP - 1))
    chain.add(make_tx(2, [(change(0), 5_000)], spends=[(receiving(0), 5_141)], height=TIP))
    connection = await _connection(run)
    wallet = build_wallet(SPEC)
    first = await _fetch(session, connection, chain, wallet)
    await _fetch(session, connection, chain, wallet)  # rechecks the shallow transactions

    records = (
        "select record_type, request, content, connector_version"
        " from core.raw_records where connection_id = :c"
    )
    stored = await _rows(session, records, c=connection.id)
    assert {r.record_type for r in stored} == {"tip", "address", "txs", "tx_status"}
    assert {r.connector_version for r in stored} == {VERSION}
    for row in stored:
        request = json.dumps(row.request)
        assert not any(a in request for a in first.scanned)
        assert "http" not in request and "esplora" not in request
    stats = [r for r in stored if r.record_type == "address"]
    assert {
        (r.request["endpoint"], r.request["source"], r.request["branch"], r.request["index"])
        for r in stats
    } >= {("/address/:address", "d0", 0, 0), ("/address/:address", "d0", 1, 0)}
    # A status names no transaction, so its record carries the one it was asked for.
    statuses = [r.content for r in stored if r.record_type == "tx_status"]
    assert {c["txid"] for c in statuses} == {txid(1), txid(2)}
    assert all(set(c) == {"txid", "status"} for c in statuses)

    sync_count = await session.scalar(
        text(
            "select count(distinct sync_id) from finance.report_sightings where connection_id = :c"
        ),
        {"c": connection.id},
    )
    assert sync_count == 2

    await _fetch(session, connection, chain, wallet)
    assert len(await _rows(session, records, c=connection.id)) == len(stored)


async def test_typed_rows_hold_every_field(session, run, chain: FakeEsplora) -> None:
    tx = chain.add(
        make_tx(
            7,
            [(receiving(0), 30_000), (STRANGER, 60_000), (None, 0)],
            spends=[(change(0), 40_000), (STRANGER, 50_141)],
            height=TIP - 50,
        )
    )
    connection = await _connection(run)
    result = await _fetch(session, connection, chain, build_wallet(SPEC))
    assert result.changed_txids == {tx["txid"]}

    [row] = await _rows(
        session,
        "select * from finance.bitcoin_transactions where connection_id = :c",
        c=connection.id,
    )
    status = tx["status"]
    assert (row.txid, row.version, row.locktime, row.size, row.weight, row.sigops, row.fee) == (
        tx["txid"],
        2,
        0,
        tx["size"],
        tx["weight"],
        tx["sigops"],
        tx["fee"],
    )
    assert (row.confirmed, row.block_height, row.block_hash) == (
        True,
        status["block_height"],
        status["block_hash"],
    )
    assert row.block_time == datetime.fromtimestamp(block_time(TIP - 50), UTC)
    assert row.seen_at_height == TIP
    assert row.first_seen_at is not None and row.last_seen_at is not None

    inputs = await _rows(
        session,
        "select * from finance.bitcoin_inputs where connection_id = :c order by input_index",
        c=connection.id,
    )
    assert len(inputs) == 2
    for got, want in zip(inputs, tx["vin"], strict=True):
        prevout = want["prevout"]
        assert (
            got.prev_txid,
            got.prev_vout,
            got.is_coinbase,
            got.scriptsig,
            got.scriptsig_asm,
            got.inner_redeemscript_asm,
            got.inner_witnessscript_asm,
            got.sequence,
            got.witness,
            got.prevout_scriptpubkey,
            got.prevout_scriptpubkey_asm,
            got.prevout_scriptpubkey_type,
            got.prevout_scriptpubkey_address,
            got.prevout_value,
        ) == (
            want["txid"],
            want["vout"],
            False,
            "",
            "",
            None,
            None,
            want["sequence"],
            want["witness"],
            prevout["scriptpubkey"],
            prevout["scriptpubkey_asm"],
            prevout["scriptpubkey_type"],
            prevout["scriptpubkey_address"],
            prevout["value"],
        )
    assert [i.own for i in inputs] == [True, False]

    outputs = await _rows(
        session,
        "select * from finance.bitcoin_outputs where connection_id = :c order by vout",
        c=connection.id,
    )
    assert [
        (
            o.vout,
            o.scriptpubkey,
            o.scriptpubkey_asm,
            o.scriptpubkey_type,
            o.scriptpubkey_address,
            o.value,
        )
        for o in outputs
    ] == [
        (
            n,
            want["scriptpubkey"],
            want["scriptpubkey_asm"],
            want["scriptpubkey_type"],
            want.get("scriptpubkey_address"),
            want["value"],
        )
        for n, want in enumerate(tx["vout"])
    ]
    assert [o.own for o in outputs] == [True, False, False]

    [report] = await _rows(
        session,
        "select * from finance.bitcoin_address_reports where connection_id = :c and address = :a",
        c=connection.id,
        a=receiving(0),
    )
    assert (
        report.chain_tx_count,
        report.chain_funded_txo_count,
        report.chain_funded_txo_sum,
        report.chain_spent_txo_count,
        report.chain_spent_txo_sum,
        report.mempool_tx_count,
    ) == (1, 1, 30_000, 0, 0, 0)


async def test_address_on_another_account_is_a_conflict(
    session, run, make_account, chain: FakeEsplora
) -> None:
    chain.add(_receive(1, receiving(0)))
    manual = await make_account(name="Old wallet", subtype="wallet", balance_mode="transactions")
    await run(assign_identifier, manual.id, "chain_address", receiving(0))
    connection = await _connection(run)

    result = await _fetch(session, connection, chain, build_wallet(SPEC))

    assert result.conflicts == [receiving(0)]
    assert {i.value for i in await list_identifiers(session, account_id=manual.id)} == {
        receiving(0)
    }
    wallet_ids = await list_identifiers(session, account_id=await _account_id(session, connection))
    assert receiving(0) not in {i.value for i in wallet_ids}
    assert receiving(1) in {i.value for i in wallet_ids}
    assert receiving(0) not in repr(result)


async def test_failed_fetch_keeps_what_it_stored(session, run, chain: FakeEsplora) -> None:
    for n, index in enumerate((0, 1, 17)):
        chain.add(_receive(n + 1, receiving(index)))
    connection = await _connection(run)
    chain.fail_after_stats = 15

    with pytest.raises(HttpRetryableError):
        await _fetch(session, connection, chain, build_wallet(SPEC))

    stored = await _rows(
        session,
        "select address_index from finance.bitcoin_addresses where connection_id = :c",
        c=connection.id,
    )
    assert sorted(r.address_index for r in stored) == list(range(15))
    counts = dict(
        (r.record_type, r.n)
        for r in await _rows(
            session,
            "select record_type, count(*) as n from core.raw_records"
            " where connection_id = :c group by record_type",
            c=connection.id,
        )
    )
    assert counts == {"tip": 1, "address": 15, "txs": 2}
    txids = await _rows(
        session,
        "select txid from finance.bitcoin_transactions where connection_id = :c",
        c=connection.id,
    )
    assert {r.txid for r in txids} == {txid(1), txid(2)}

    chain.fail_after_stats = None
    chain.requests.clear()
    result = await _fetch(session, connection, chain, build_wallet(SPEC))

    assert chain.asked_stats() == [receiving(i) for i in range(38)] + [change(i) for i in range(10)]
    assert chain.asked_txs() == [f"/address/{receiving(17)}/txs"]
    # What the failed sync stored was never normalized: it is still to post.
    assert result.changed_txids == {txid(1), txid(2), txid(3)}


async def test_wallet_account_is_made_once(session, run, chain: FakeEsplora) -> None:
    chain.add(_receive(1, receiving(0), height=TIP - 10))
    chain.add(_receive(2, receiving(1), height=TIP - 300))
    connection = await _connection(run)
    await _fetch(session, connection, chain, build_wallet(SPEC))

    account_id = await _account_id(session, connection)
    account = await get_account(session, account_id)
    assert (account.name, account.ledger_type, account.subtype, account.balance_mode) == (
        "Bitcoin wallet",
        "asset",
        "wallet",
        "transactions",
    )
    assert account.source_connection_id == connection.id
    first_day = datetime.fromtimestamp(block_time(TIP - 300), UTC).date()
    assert account.opened_on == first_day

    async with httpx.AsyncClient() as http:
        ctx = SyncContext(
            esplora_url=BASE,
            command=CommandContext(cast(AsyncSession, _NoSession()), CONNECTOR, uuid.uuid4()),
            connection=await get_connection(session, connection.id),
            sync_id=uuid.uuid4(),
            master_key=KEY,
            http=http,
            notifier=None,  # pyright: ignore[reportArgumentType]
            session_factory=async_sessionmaker(
                bind=session.bind, join_transaction_mode="create_savepoint"
            ),
        )
        assert await wallet_account(ctx, date(2001, 1, 1)) == account_id
    assert (await get_account(session, account_id)).opened_on == first_day


async def test_address_list_asks_each_address_once(session, run, chain: FakeEsplora) -> None:
    listed = (receiving(3), change(2), receiving(0))
    chain.add(_receive(1, change(2)))
    wallet = build_wallet(parse_input(WalletInput("addresses", addresses=listed)))
    connection = await _connection(run)

    result = await _fetch(session, connection, chain, wallet)

    assert chain.asked_stats() == list(listed)
    assert result.changed_txids == {txid(1)}
    rows = await _rows(
        session,
        "select source, branch, address_index, address from finance.bitcoin_addresses"
        " where connection_id = :c order by address_index",
        c=connection.id,
    )
    assert [(r.source, r.branch, r.address_index, r.address) for r in rows] == [
        ("list", 0, n, a) for n, a in enumerate(listed)
    ]


async def test_unnormalized_transactions_stay_pending(session, run, chain: FakeEsplora) -> None:
    chain.add(_receive(1, receiving(0)))
    connection = await _connection(run)
    wallet = build_wallet(SPEC)
    first = await _fetch(session, connection, chain, wallet, mark=False)
    assert first.changed_txids == {txid(1)}

    # The sync failed after the fetch (in normalize, say): nothing was marked.
    again = await _fetch(session, connection, chain, wallet)
    assert chain.asked_txs() == [f"/address/{receiving(0)}/txs"]  # asked once, by the first
    assert again.changed_txids == {txid(1)}

    assert (await _fetch(session, connection, chain, wallet)).changed_txids == set()


@pytest.mark.parametrize("was_confirmed", [False, True])
async def test_replacement_is_found(session, run, chain: FakeEsplora, was_confirmed: bool) -> None:
    """A fee bump (or a double spend) leaves every address's count as it was: the recheck's
    404 for the replaced transaction is what makes the scan ask the histories again."""
    chain.add(_receive(1, receiving(0), value=50_141, height=TIP - 100))
    replaced = chain.add(
        make_tx(
            2,
            [(STRANGER, 30_000), (change(0), 20_000)],
            spends=[(receiving(0), 50_141)],
            height=TIP if was_confirmed else None,
        )
    )
    connection = await _connection(run)
    wallet = build_wallet(SPEC)
    await _fetch(session, connection, chain, wallet)

    chain.drop(replaced["txid"])
    bumped = chain.add(
        make_tx(
            3,
            [(STRANGER, 30_000), (change(0), 19_641)],
            spends=[(receiving(0), 50_141)],
            height=TIP + 1,
            fee=500,
        )
    )
    chain.tip = TIP + 1
    chain.requests.clear()
    result = await _fetch(session, connection, chain, wallet)

    assert bumped["txid"] in result.changed_txids
    assert result.left_chain == ({replaced["txid"]} if was_confirmed else set())
    stored = await _rows(
        session,
        "select txid from finance.bitcoin_transactions where connection_id = :c and confirmed",
        c=connection.id,
    )
    assert bumped["txid"] in {r.txid for r in stored}


async def test_a_history_stored_through_another_address_is_still_walked(
    session, run, chain: FakeEsplora
) -> None:
    """B receives 25 payments, then T pays B from A. A sync that stores A's history (so T)
    and fails before B's leaves T stored: B's next walk must not stop at T, since B's own last
    history predates it, and must reach the oldest new payment on the second page."""
    a, b = receiving(0), receiving(1)
    chain.add(_receive(1, a, value=90_141, height=TIP - 600))
    for n in range(30):
        chain.add(_receive(100 + n, b, value=1_000 + n, height=TIP - 500 + n))
    connection = await _connection(run)
    wallet = build_wallet(SPEC)
    await _fetch(session, connection, chain, wallet)

    payments = [chain.add(_receive(200 + n, b, height=TIP + 1 + n)) for n in range(25)]
    spend = chain.add(make_tx(300, [(b, 90_000)], spends=[(a, 90_141)], height=TIP + 26))
    chain.tip = TIP + 30
    chain.requests.clear()
    chain.fail_after_stats = 1  # A's stats and history, then B's stats fails
    with pytest.raises(HttpRetryableError):
        await _fetch(session, connection, chain, wallet)
    assert spend["txid"] in {
        r.txid
        for r in await _rows(
            session,
            "select txid from finance.bitcoin_transactions where connection_id = :c",
            c=connection.id,
        )
    }

    chain.fail_after_stats = None
    chain.tip = TIP + 60  # T is now deep, but not by B's last history
    result = await _fetch(session, connection, chain, wallet)

    assert payments[0]["txid"] in result.changed_txids
    assert {p["txid"] for p in payments} | {spend["txid"]} <= result.changed_txids


def _all_used(chain: FakeEsplora) -> None:
    """The server says every address has one transaction (and lists none)."""
    real = chain.stats

    def stats(address: str) -> dict[str, Any]:
        found = real(address)
        found["chain_stats"]["tx_count"] = max(found["chain_stats"]["tx_count"], 1)
        return found

    chain.stats = stats  # type: ignore[method-assign]


async def test_endless_scan_is_capped(session, run, chain: FakeEsplora, monkeypatch) -> None:
    monkeypatch.setattr(fetch_module, "MAX_INDEX", 5)
    _all_used(chain)
    connection = await _connection(run)

    with pytest.raises(TooManyAddresses) as caught:
        await _fetch(session, connection, chain, build_wallet(SPEC))

    assert str(caught.value) == "The wallet has more addresses than Lararium scans."
    assert chain.asked_stats() == [receiving(i) for i in range(5)]


async def test_history_longer_than_its_count_is_refused(session, run, chain: FakeEsplora) -> None:
    for n in range(3):
        chain.add(_receive(1 + n, receiving(0)))
    real = chain.stats

    def understated(address: str) -> dict[str, Any]:
        found = real(address)
        found["chain_stats"]["tx_count"] = min(found["chain_stats"]["tx_count"], 2)
        return found

    chain.stats = understated  # type: ignore[method-assign]
    connection = await _connection(run)

    with pytest.raises(MalformedResponse):
        await _fetch(session, connection, chain, build_wallet(SPEC))

    stored = await _rows(
        session,
        "select txid from finance.bitcoin_transactions where connection_id = :c",
        c=connection.id,
    )
    assert stored == []


async def test_tip_below_a_stored_block_is_refused(session, run, chain: FakeEsplora) -> None:
    chain.add(_receive(1, receiving(0), height=TIP))
    connection = await _connection(run)
    await _fetch(session, connection, chain, build_wallet(SPEC))

    chain.tip = TIP - 1
    with pytest.raises(MalformedResponse):
        await _fetch(session, connection, chain, build_wallet(SPEC))


async def test_tip_too_far_on_is_refused(session, run, chain: FakeEsplora) -> None:
    chain.add(_receive(1, receiving(0)))
    connection = await _connection(run)
    await _fetch(session, connection, chain, build_wallet(SPEC))
    assert (await get_connection(session, connection.id)).config[TIP_KEY] == TIP

    chain.tip = TIP + MAX_TIP_JUMP + 1
    with pytest.raises(MalformedResponse):
        await _fetch(session, connection, chain, build_wallet(SPEC))
    assert (await get_connection(session, connection.id)).config[TIP_KEY] == TIP

    chain.tip = TIP + MAX_TIP_JUMP
    assert (await _fetch(session, connection, chain, build_wallet(SPEC))).tip == chain.tip
