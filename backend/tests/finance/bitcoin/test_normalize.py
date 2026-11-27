"""The Bitcoin normalizer: one ledger transaction per movement (receipts, payments, the fee,
a shared transaction's net), confirmed only, voided when a transaction leaves the chain and
posted again when it confirms, one Bitcoin instrument shared with Binance, and the replay."""

import json
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import httpx
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from lararium.core.commands import CommandContext, command_context
from lararium.core.connections import (
    ConnectionRecord,
    create_connection,
    get_connection,
    update_config,
)
from lararium.core.connectors import SyncContext
from lararium.modules.finance import models
from lararium.modules.finance.accounts import get_account
from lararium.modules.finance.bitcoin.accounts import ACCOUNT_KEY
from lararium.modules.finance.bitcoin.derive import Branch, Source, Wallet, build_wallet
from lararium.modules.finance.bitcoin.fetch import FetchResult, fetch
from lararium.modules.finance.bitcoin.instruments import SEVERAL_FLAG
from lararium.modules.finance.bitcoin.normalize import (
    FEE_FLAG,
    LEFT_CHAIN_FLAG,
    NORMALIZER_VERSION,
    SHARED_FLAG,
    NormalizeReport,
    normalize,
)
from lararium.modules.finance.connector_run import holdings
from lararium.modules.finance.identifiers import get_identifier
from lararium.modules.finance.instruments import (
    InstrumentIdentifier,
    create_instrument,
    get_instrument,
)
from lararium.modules.finance.ledger import void_transaction
from lararium.modules.finance.system_accounts import system_account_ids

from .fake_esplora import BASE, STRANGER, TIP, FakeEsplora, block_time, make_tx, output, txid
from .test_fetch import CONNECTOR, KEY, SPEC, change, receiving

# Addresses of nobody in these tests: the fake server takes any text for an address.
OTHERS = tuple(f"bc1qother{n:02d}{'x' * 30}" for n in range(8))
FAR = datetime(2100, 1, 1, tzinfo=UTC)


def btc(sats: int) -> Decimal:
    return Decimal(sats) / 10**8


def at(height: int) -> datetime:
    return datetime.fromtimestamp(block_time(height), UTC)


@dataclass
class CachedWallet:
    """A wallet whose addresses are derived once per test session (derivation is slow)."""

    inner: Wallet
    cache: dict[tuple[str, Branch, int], tuple[str, str]] = field(
        default_factory=dict[tuple[str, Branch, int], tuple[str, str]]
    )

    @property
    def sources(self) -> tuple[Source, ...]:
        return self.inner.sources

    @property
    def descriptors(self) -> tuple[str, ...]:
        return self.inner.descriptors

    @property
    def fingerprint(self) -> str:
        return self.inner.fingerprint

    def address(self, source: str, branch: Branch, index: int) -> tuple[str, str]:
        key = (source, branch, index)
        if key not in self.cache:
            self.cache[key] = self.inner.address(source, branch, index)
        return self.cache[key]

    def listed(self) -> tuple[str, ...]:
        return self.inner.listed()


WALLET = CachedWallet(build_wallet(SPEC))


@dataclass(frozen=True)
class Posted:
    id: uuid.UUID
    status: str
    voided_by: str | None
    occurred_at: datetime
    description: str
    chain: str | None
    tx_hash: str | None
    chain_address: str | None
    counterparty_identifier_id: uuid.UUID | None
    counterparty_name: str | None
    never_transfer: bool
    # (account, instrument, quantity, is_fee), in position order.
    postings: tuple[tuple[uuid.UUID, uuid.UUID, Decimal, bool], ...]


class Synced:
    """A Bitcoin connection synced against the fake chain: each `sync` fetches and then
    normalizes what the fetch found, in the sync's own session, as the connector will."""

    def __init__(
        self, session: AsyncSession, connection: ConnectionRecord, chain: FakeEsplora
    ) -> None:
        self.session = session
        self.connection = connection
        self.chain = chain

    @classmethod
    async def start(cls, session: AsyncSession, chain: FakeEsplora) -> "Synced":
        async with command_context(session, CONNECTOR, "test", {}) as cmd:
            connection = await create_connection(
                cmd, connector="bitcoin", display_name="Bitcoin wallet", config={}
            )
        return cls(session, connection, chain)

    async def context(self, http: httpx.AsyncClient) -> SyncContext:
        return SyncContext(
            esplora_url=BASE,
            command=CommandContext(self.session, CONNECTOR, uuid.uuid4()),
            connection=await get_connection(self.session, self.connection.id),
            sync_id=uuid.uuid4(),
            master_key=KEY,
            http=http,
            notifier=None,  # pyright: ignore[reportArgumentType]  # nothing is sent
            session_factory=async_sessionmaker(
                bind=self.session.bind, join_transaction_mode="create_savepoint"
            ),
        )

    async def sync(self, wallet: Wallet = WALLET) -> tuple[FetchResult, NormalizeReport]:
        async with httpx.AsyncClient(transport=httpx.MockTransport(self.chain.handler)) as http:
            ctx = await self.context(http)
            found = await fetch(ctx, self.chain.client(http), wallet)
            report = await normalize(ctx, found.changed_txids | found.left_chain)
        await self.session.flush()
        return found, report

    async def normalize(self, txids: set[str] | None) -> NormalizeReport:
        async with httpx.AsyncClient() as http:
            report = await normalize(await self.context(http), txids)
        await self.session.flush()
        return report

    async def account_id(self) -> uuid.UUID:
        found = (await get_connection(self.session, self.connection.id)).config[ACCOUNT_KEY]
        assert isinstance(found, str)
        return uuid.UUID(found)

    async def balance(self) -> Decimal:
        held = await holdings(self.session, await self.account_id(), FAR)
        assert len(held) <= 1
        return sum(held.values(), Decimal(0))

    async def ledger(self) -> dict[str, Posted]:
        t, p = models.Transaction, models.Posting
        rows = (
            await self.session.execute(
                select(t).where(t.connection_id == self.connection.id).order_by(t.external_id)
            )
        ).scalars()
        found: dict[str, Posted] = {}
        for row in rows:
            postings = await self.session.execute(
                select(p.account_id, p.instrument_id, p.quantity, p.is_fee)
                .where(p.transaction_id == row.id)
                .order_by(p.position)
            )
            assert row.external_id is not None
            found[row.external_id] = Posted(
                id=row.id,
                status=row.status,
                voided_by=row.voided_by,
                occurred_at=row.occurred_at,
                description=row.description,
                chain=row.chain,
                tx_hash=row.tx_hash,
                chain_address=row.chain_address,
                counterparty_identifier_id=row.counterparty_identifier_id,
                counterparty_name=row.counterparty_name,
                never_transfer=row.never_transfer,
                postings=tuple(
                    (r.account_id, r.instrument_id, Decimal(r.quantity), r.is_fee) for r in postings
                ),
            )
        return found

    async def live(self) -> dict[str, Posted]:
        return {k: v for k, v in (await self.ledger()).items() if v.status != "void"}

    async def row(self, sql: str, **params: Any) -> Any:
        return (await self.session.execute(text(sql), params | {"c": self.connection.id})).one()


async def _unassigned(session: AsyncSession) -> uuid.UUID:
    return (await system_account_ids(session))["unassigned"]


async def test_receipt_posts_each_own_output(session) -> None:
    chain = FakeEsplora()
    tx = chain.add(
        make_tx(
            1,
            [(receiving(0), 30_000), (receiving(1), 20_000)] + [(o, 1_000) for o in OTHERS[:5]],
            spends=[(STRANGER, 40_000), (OTHERS[6], 15_141)],
        )
    )
    wallet = await Synced.start(session, chain)

    _, report = await wallet.sync()

    account_id = await wallet.account_id()
    unassigned = await _unassigned(session)
    ledger = await wallet.ledger()
    t = tx["txid"]
    assert set(ledger) == {f"{t}:0", f"{t}:1"}
    for vout, sats in ((0, 30_000), (1, 20_000)):
        posted = ledger[f"{t}:{vout}"]
        assert posted.description == "Received bitcoin"
        assert posted.occurred_at == at(TIP - 100)
        assert (posted.chain, posted.tx_hash, posted.chain_address) == (
            "bitcoin",
            t,
            receiving(vout),
        )
        assert posted.counterparty_identifier_id is None
        assert [(a, q, f) for a, _, q, f in posted.postings] == [
            (account_id, btc(sats), False),
            (unassigned, -btc(sats), False),
        ]
    assert (report.created, report.updated, report.unchanged) == (2, 0, 0)
    assert await wallet.balance() == btc(50_000)
    links = await session.execute(
        text("select vout, transaction_id from finance.bitcoin_outputs where txid = :t"),
        {"t": t},
    )
    assert {r.vout: r.transaction_id for r in links} == {
        0: ledger[f"{t}:0"].id,
        1: ledger[f"{t}:1"].id,
        **{n: None for n in range(2, 7)},
    }


async def test_spend_posts_payments_and_fee_not_change(session) -> None:
    chain = FakeEsplora()
    chain.add(make_tx(1, [(receiving(0), 100_000)], height=TIP - 200))
    spend = chain.add(
        make_tx(
            2,
            [(OTHERS[0], 60_000), (change(0), 39_000)],
            spends=[(receiving(0), 100_000)],
            fee=1_000,
        )
    )
    wallet = await Synced.start(session, chain)

    await wallet.sync()

    account_id = await wallet.account_id()
    unassigned = await _unassigned(session)
    t = spend["txid"]
    ledger = await wallet.ledger()
    assert set(ledger) == {f"{txid(1)}:0", f"{t}:0", f"{t}:fee"}
    payment = ledger[f"{t}:0"]
    assert payment.description == "Sent bitcoin"
    assert (payment.chain, payment.tx_hash, payment.chain_address) == ("bitcoin", t, OTHERS[0])
    assert payment.counterparty_identifier_id is not None
    paid = await get_identifier(session, payment.counterparty_identifier_id)
    assert (paid.kind, paid.value, paid.account_id) == ("chain_address", OTHERS[0], None)
    assert payment.counterparty_name == "Bitcoin address"
    assert [(a, q, f) for a, _, q, f in payment.postings] == [
        (account_id, -btc(60_000), False),
        (unassigned, btc(60_000), False),
    ]
    fee = ledger[f"{t}:fee"]
    assert fee.description == "Bitcoin network fee"
    assert (fee.chain, fee.tx_hash, fee.chain_address) == ("bitcoin", t, None)
    assert fee.never_transfer
    assert [(a, q, f) for a, _, q, f in fee.postings] == [
        (account_id, -btc(1_000), True),
        (unassigned, btc(1_000), False),
    ]
    assert await wallet.balance() == btc(100_000) - btc(61_000)
    fee_link = await wallet.row(
        "select fee_transaction_id, net_transaction_id from finance.bitcoin_transactions"
        " where connection_id = :c and txid = :t",
        t=t,
    )
    assert (fee_link.fee_transaction_id, fee_link.net_transaction_id) == (fee.id, None)


async def test_spend_to_own_addresses_is_fee_only(session) -> None:
    chain = FakeEsplora()
    spend = chain.add(
        make_tx(
            1,
            [(receiving(1), 50_000), (change(0), 49_500)],
            spends=[(receiving(0), 100_000)],
            fee=500,
        )
    )
    wallet = await Synced.start(session, chain)

    await wallet.sync()

    ledger = await wallet.ledger()
    assert set(ledger) == {f"{spend['txid']}:fee"}
    assert await wallet.balance() == -btc(500)


async def test_shared_transaction_posts_net_and_flags(session) -> None:
    chain = FakeEsplora()
    shared = chain.add(
        make_tx(
            1,
            [(receiving(1), 30_000), (OTHERS[0], 79_000)],
            spends=[(receiving(0), 40_000), (STRANGER, 70_000)],
            fee=1_000,
        )
    )
    wallet = await Synced.start(session, chain)

    _, report = await wallet.sync()

    t = shared["txid"]
    ledger = await wallet.ledger()
    assert set(ledger) == {f"{t}:net"}
    net = ledger[f"{t}:net"]
    assert net.description == "Shared transaction"
    assert (net.chain, net.tx_hash, net.chain_address) == ("bitcoin", t, None)
    assert net.never_transfer
    assert [(a, q) for a, _, q, _ in net.postings] == [
        (await wallet.account_id(), -btc(10_000)),
        (await _unassigned(session), btc(10_000)),
    ]
    assert SHARED_FLAG in report.flagged
    assert not any(t in flag for flag in report.flagged)
    link = await wallet.row(
        "select net_transaction_id from finance.bitcoin_transactions"
        " where connection_id = :c and txid = :t",
        t=t,
    )
    assert link.net_transaction_id == net.id

    again = await wallet.normalize({t})
    assert SHARED_FLAG not in again.flagged
    assert again.unchanged == 1


async def test_address_less_output_with_value_is_a_payment(session) -> None:
    chain = FakeEsplora()
    built = make_tx(
        1, [(None, 5_000), (change(0), 94_000)], spends=[(receiving(0), 100_000)], fee=1_000
    )
    bare = output(None, 5_000)
    bare.update(scriptpubkey="51", scriptpubkey_asm="OP_PUSHNUM_1", scriptpubkey_type="unknown")
    built["vout"][0] = bare
    chain.add(built)
    wallet = await Synced.start(session, chain)

    await wallet.sync()

    t = built["txid"]
    ledger = await wallet.ledger()
    assert set(ledger) == {f"{t}:0", f"{t}:fee"}
    payment = ledger[f"{t}:0"]
    assert (payment.chain_address, payment.counterparty_identifier_id) == (None, None)
    assert payment.counterparty_name is None
    assert payment.postings[0][2] == -btc(5_000)
    assert await wallet.balance() == -btc(6_000)


async def test_op_return_posts_nothing(session) -> None:
    chain = FakeEsplora()
    built = chain.add(
        make_tx(1, [(None, 0), (change(0), 99_000)], spends=[(receiving(0), 100_000)], fee=1_000)
    )
    wallet = await Synced.start(session, chain)

    await wallet.sync()

    assert set(await wallet.ledger()) == {f"{built['txid']}:fee"}


async def test_unconfirmed_is_not_posted_until_confirmed(session) -> None:
    chain = FakeEsplora()
    waiting = chain.add(make_tx(1, [(receiving(0), 25_000)], height=None))
    wallet = await Synced.start(session, chain)

    found, report = await wallet.sync()

    t = waiting["txid"]
    assert found.changed_txids == {t}
    assert await wallet.ledger() == {}
    assert report.created == 0
    row = await wallet.row(
        "select pending, normalized_confirmed from finance.bitcoin_transactions"
        " where connection_id = :c and txid = :t",
        t=t,
    )
    assert (row.pending, row.normalized_confirmed) == (False, False)

    chain.move(t, TIP - 1)
    found, _ = await wallet.sync()

    assert found.changed_txids == {t}
    ledger = await wallet.live()
    assert set(ledger) == {f"{t}:0"}
    assert ledger[f"{t}:0"].occurred_at == at(TIP - 1)


async def test_dropped_transaction_is_voided_and_reposted(session) -> None:
    chain = FakeEsplora()
    chain.add(make_tx(1, [(receiving(0), 100_000)], height=TIP - 200))
    spend = chain.add(
        make_tx(
            2,
            [(OTHERS[0], 60_000), (change(0), 39_000)],
            spends=[(receiving(0), 100_000)],
            height=TIP - 1,
            fee=1_000,
        )
    )
    t = spend["txid"]
    wallet = await Synced.start(session, chain)
    await wallet.sync()
    assert {f"{t}:0", f"{t}:fee"} <= set(await wallet.live())

    chain.drop(t)
    found, report = await wallet.sync()

    assert found.left_chain == {t}
    ledger = await wallet.ledger()
    for key in (f"{t}:0", f"{t}:fee"):
        assert (ledger[key].status, ledger[key].voided_by) == ("void", "connector")
    assert LEFT_CHAIN_FLAG in report.flagged
    row = await wallet.row(
        "select left_chain_at, fee_transaction_id, pending from finance.bitcoin_transactions"
        " where connection_id = :c and txid = :t",
        t=t,
    )
    assert row.left_chain_at is not None
    assert (row.fee_transaction_id, row.pending) == (None, False)
    assert await wallet.balance() == btc(100_000)

    # It confirms again, in another block.
    chain.add(spend)
    chain.move(t, TIP, fork=2)
    found, report = await wallet.sync()

    assert t in found.changed_txids
    assert LEFT_CHAIN_FLAG not in report.flagged
    again = await wallet.ledger()
    for key in (f"{t}:0", f"{t}:fee"):
        assert again[key].id == ledger[key].id
        assert again[key].status == "booked"
        assert again[key].occurred_at == at(TIP)
    assert await wallet.balance() == btc(39_000)


async def test_found_change_voids_the_payment(session) -> None:
    """A raised gap limit finds that an output paid one of the wallet's own addresses: the
    payment it posted becomes change, which posts nothing."""
    chain = FakeEsplora()
    spend = chain.add(
        make_tx(1, [(receiving(25), 60_000)], spends=[(receiving(0), 61_000)], fee=1_000)
    )
    t = spend["txid"]
    wallet = await Synced.start(session, chain)
    await wallet.sync()
    assert set(await wallet.live()) == {f"{t}:0", f"{t}:fee"}

    await wallet.sync(CachedWallet(build_wallet(SPEC, {"d0": {0: 30}})))

    ledger = await wallet.ledger()
    assert (ledger[f"{t}:0"].status, ledger[f"{t}:0"].voided_by) == ("void", "connector")
    assert set(await wallet.live()) == {f"{t}:fee"}
    assert await wallet.balance() == -btc(1_000)


async def test_resync_is_unchanged(session) -> None:
    chain = FakeEsplora()
    chain.add(make_tx(1, [(receiving(0), 100_000), (receiving(1), 5_000)], height=TIP - 200))
    chain.add(
        make_tx(
            2,
            [(OTHERS[0], 60_000), (change(0), 39_000)],
            spends=[(receiving(0), 100_000)],
            fee=1_000,
        )
    )
    chain.add(
        make_tx(3, [(receiving(2), 9_000)], spends=[(receiving(1), 5_000), (STRANGER, 5_000)])
    )
    wallet = await Synced.start(session, chain)
    found, first = await wallet.sync()
    before = await wallet.ledger()
    n = first.created
    assert n == len(before) == 5

    again = await wallet.normalize(found.changed_txids)

    assert (again.created, again.updated, again.unchanged) == (0, 0, n)
    assert await wallet.ledger() == before
    _, quiet = await wallet.sync()
    assert (quiet.created, quiet.updated, quiet.unchanged) == (0, 0, 0)


async def test_account_opens_on_first_block_day(session) -> None:
    chain = FakeEsplora()
    chain.add(make_tx(1, [(receiving(0), 1_000)], height=TIP - 20))
    chain.add(make_tx(2, [(receiving(1), 1_000)], height=TIP - 400))
    chain.add(make_tx(3, [(receiving(2), 1_000)], height=None))
    wallet = await Synced.start(session, chain)

    await wallet.sync()

    account = await get_account(session, await wallet.account_id())
    assert account.opened_on == at(TIP - 400).date()
    assert (account.ledger_type, account.subtype, account.balance_mode) == (
        "asset",
        "wallet",
        "transactions",
    )


async def test_coinbase_output_is_a_receipt(session) -> None:
    chain = FakeEsplora()
    mined = make_tx(1, [(receiving(0), 312_500_000)], fee=0)
    mined["vin"] = [
        {
            "txid": "00" * 32,
            "vout": 4_294_967_295,
            "is_coinbase": True,
            "scriptsig": "03" + "ab" * 3,
            "scriptsig_asm": "OP_PUSHBYTES_3 " + "ab" * 3,
            "sequence": 4_294_967_295,
            "prevout": None,
        }
    ]
    chain.add(mined)
    wallet = await Synced.start(session, chain)

    await wallet.sync()

    ledger = await wallet.ledger()
    assert set(ledger) == {f"{mined['txid']}:0"}
    assert ledger[f"{mined['txid']}:0"].description == "Received bitcoin"
    assert await wallet.balance() == btc(312_500_000)


async def test_out_of_order_block_times_keep_their_own(session) -> None:
    chain = FakeEsplora()
    earlier = chain.add(make_tx(1, [(receiving(0), 1_000)], height=TIP - 11))
    later = chain.add(make_tx(2, [(receiving(1), 2_000)], height=TIP - 10))
    # The higher block's time is the lower one's less an hour: block times need not increase.
    later["status"]["block_time"] = earlier["status"]["block_time"] - 3_600
    wallet = await Synced.start(session, chain)

    await wallet.sync()

    ledger = await wallet.ledger()
    assert ledger[f"{earlier['txid']}:0"].occurred_at == at(TIP - 11)
    assert ledger[f"{later['txid']}:0"].occurred_at == datetime.fromtimestamp(
        later["status"]["block_time"], UTC
    )


async def test_bitcoin_instrument_is_shared_with_binance(session, run) -> None:
    binance = await run(
        create_instrument,
        kind="crypto",
        symbol="BTC",
        name="Bitcoin",
        decimals=8,
        quote_instrument_id=None,
        identifiers=[InstrumentIdentifier("binance_asset", "BTC")],
    )
    chain = FakeEsplora()
    chain.add(make_tx(1, [(receiving(0), 1_000)]))
    wallet = await Synced.start(session, chain)

    await wallet.sync()

    (posted,) = (await wallet.ledger()).values()
    assert {instrument for _, instrument, _, _ in posted.postings} == {binance.id}
    found = await get_instrument(session, binance.id)
    assert {(i.scheme, i.value) for i in found.identifiers} == {
        ("binance_asset", "BTC"),
        ("chain_native", "bitcoin"),
        ("coingecko", "bitcoin"),
    }


async def test_one_untagged_btc_is_taken_several_are_not(session, run) -> None:
    async def untagged() -> uuid.UUID:
        made = await run(
            create_instrument,
            kind="crypto",
            symbol="btc",
            name="Bitcoin",
            decimals=8,
            quote_instrument_id=None,
            identifiers=[],
        )
        return made.id

    first = await untagged()
    second = await untagged()
    chain = FakeEsplora()
    chain.add(make_tx(1, [(receiving(0), 1_000)]))
    wallet = await Synced.start(session, chain)

    _, report = await wallet.sync()

    (posted,) = (await wallet.ledger()).values()
    (instrument,) = {i for _, i, _, _ in posted.postings}
    assert instrument not in (first, second)
    assert SEVERAL_FLAG in report.flagged
    created = await get_instrument(session, instrument)
    assert (created.kind, created.symbol, created.name, created.decimals) == (
        "crypto",
        "BTC",
        "Bitcoin",
        8,
    )
    assert {i.scheme for i in created.identifiers} == {"chain_native", "binance_asset", "coingecko"}

    # A second wallet finds the instrument by chain_native and flags nothing.
    other = await Synced.start(session, chain)
    _, report = await other.sync()
    (posted,) = (await other.ledger()).values()
    assert {i for _, i, _, _ in posted.postings} == {instrument}
    assert SEVERAL_FLAG not in report.flagged


async def test_replay_on_version_change(session) -> None:
    chain = FakeEsplora()
    chain.add(make_tx(1, [(receiving(0), 100_000)], height=TIP - 200))
    chain.add(
        make_tx(
            2,
            [(OTHERS[0], 60_000), (change(0), 39_000)],
            spends=[(receiving(0), 100_000)],
            fee=1_000,
        )
    )
    waiting = chain.add(make_tx(3, [(receiving(1), 7_000)], height=None))
    wallet = await Synced.start(session, chain)
    await wallet.sync()
    config = (await get_connection(session, wallet.connection.id)).config
    assert config["normalizer_version"] == NORMALIZER_VERSION
    before = await wallet.ledger()
    assert len(before) == 3

    async with command_context(session, CONNECTOR, "test", {}) as cmd:
        await update_config(cmd, wallet.connection.id, {"normalizer_version": "bitcoin-norm-0"})
    # The sync finds nothing new, but the stored version differs: every stored transaction
    # is normalized again (the one still waiting posts nothing).
    found, report = await wallet.sync()

    assert found.changed_txids == set()
    assert (report.created, report.updated, report.unchanged) == (0, 0, 3)
    assert await wallet.ledger() == before
    config = (await get_connection(session, wallet.connection.id)).config
    assert config["normalizer_version"] == NORMALIZER_VERSION
    waited = await wallet.row(
        "select pending from finance.bitcoin_transactions where connection_id = :c and txid = :t",
        t=waiting["txid"],
    )
    assert waited.pending is False

    # `None` replays whatever the version says.
    replayed = await wallet.normalize(None)
    assert (replayed.created, replayed.updated, replayed.unchanged) == (0, 0, 3)


async def test_confirmed_without_block_time_stays_pending(session) -> None:
    chain = FakeEsplora()
    tx = chain.add(make_tx(1, [(receiving(0), 1_000)]))
    t = tx["txid"]
    wallet = await Synced.start(session, chain)
    await wallet.sync()
    posted = await wallet.ledger()
    await session.execute(
        text(
            "update finance.bitcoin_transactions set block_time = null, pending = true"
            " where connection_id = :c and txid = :t"
        ),
        {"c": wallet.connection.id, "t": t},
    )

    report = await wallet.normalize({t})

    assert (report.created, report.updated, report.unchanged) == (0, 0, 0)
    assert await wallet.ledger() == posted
    row = await wallet.row(
        "select pending from finance.bitcoin_transactions where connection_id = :c and txid = :t",
        t=t,
    )
    assert row.pending is True


class TimelessStatus(FakeEsplora):
    """A server whose `/tx/:txid/status` omits `block_time`, as Esplora documents it."""

    def handler(self, request: httpx.Request) -> httpx.Response:
        response = super().handler(request)
        if "/tx/" in request.url.path and response.status_code == 200:
            body = json.loads(response.text)
            body.pop("block_time", None)
            return httpx.Response(200, text=json.dumps(body))
        return response


async def test_confirmed_by_a_timeless_status_is_dated_by_its_history(session) -> None:
    chain = TimelessStatus()
    waiting = chain.add(make_tx(1, [(receiving(0), 25_000)], height=None))
    t = waiting["txid"]
    wallet = await Synced.start(session, chain)
    await wallet.sync()

    chain.move(t, TIP - 1)
    chain.requests.clear()
    found, _ = await wallet.sync()

    assert f"/address/{receiving(0)}/txs" in chain.asked_txs()
    assert t in found.changed_txids
    ledger = await wallet.live()
    assert set(ledger) == {f"{t}:0"}
    assert ledger[f"{t}:0"].occurred_at == at(TIP - 1)
    row = await wallet.row(
        "select block_time, pending from finance.bitcoin_transactions"
        " where connection_id = :c and txid = :t",
        t=t,
    )
    assert (row.block_time, row.pending) == (at(TIP - 1), False)


async def test_shared_flag_only_when_first_posted(session) -> None:
    chain = FakeEsplora()
    shared = chain.add(
        make_tx(
            1,
            [(receiving(1), 30_000), (OTHERS[0], 79_000)],
            spends=[(receiving(0), 40_000), (STRANGER, 70_000)],
            height=TIP - 1,
            fee=1_000,
        )
    )
    t = shared["txid"]
    wallet = await Synced.start(session, chain)
    _, first = await wallet.sync()
    assert SHARED_FLAG in first.flagged

    chain.move(t, None)
    await wallet.sync()
    chain.move(t, TIP, fork=2)
    _, again = await wallet.sync()

    assert again.updated == 1
    assert (await wallet.live())[f"{t}:net"].occurred_at == at(TIP)
    assert SHARED_FLAG not in again.flagged


async def test_owner_voided_movement_is_not_linked(session, run) -> None:
    chain = FakeEsplora()
    spend = chain.add(
        make_tx(
            1,
            [(OTHERS[0], 60_000), (change(0), 39_000)],
            spends=[(receiving(0), 100_000)],
            fee=1_000,
        )
    )
    t = spend["txid"]
    wallet = await Synced.start(session, chain)
    await wallet.sync()
    ledger = await wallet.ledger()
    for key in (f"{t}:0", f"{t}:fee"):
        await run(void_transaction, ledger[key].id)

    await wallet.normalize({t})

    assert {k: v.voided_by for k, v in (await wallet.ledger()).items()} == {
        f"{t}:0": "user",
        f"{t}:fee": "user",
    }
    links = await session.execute(
        text("select vout, transaction_id from finance.bitcoin_outputs where txid = :t"),
        {"t": t},
    )
    assert {r.vout: r.transaction_id for r in links} == {0: None, 1: None}
    row = await wallet.row(
        "select fee_transaction_id from finance.bitcoin_transactions"
        " where connection_id = :c and txid = :t",
        t=t,
    )
    assert row.fee_transaction_id is None


async def test_fee_that_does_not_add_up_is_flagged_and_waits(session) -> None:
    chain = FakeEsplora()
    good = chain.add(make_tx(1, [(receiving(0), 100_000)], height=TIP - 200))
    # The inputs less the outputs are 1,000; the server says the fee is 900.
    bad = chain.add(
        make_tx(
            2,
            [(OTHERS[0], 60_000), (change(0), 39_000)],
            spends=[(receiving(0), 100_000)],
            fee=900,
        )
    )
    wallet = await Synced.start(session, chain)

    async def pending(t: str) -> bool:
        row = await wallet.row(
            "select pending from finance.bitcoin_transactions"
            " where connection_id = :c and txid = :t",
            t=t,
        )
        return row.pending

    _, report = await wallet.sync()

    assert set(await wallet.live()) == {f"{good['txid']}:0"}
    assert FEE_FLAG in report.flagged
    assert not any(bad["txid"] in flag for flag in report.flagged)
    assert (await pending(good["txid"]), await pending(bad["txid"])) == (False, True)

    again = await wallet.normalize({good["txid"], bad["txid"]})

    assert FEE_FLAG in again.flagged
    assert (again.created, again.unchanged) == (0, 1)
    assert set(await wallet.live()) == {f"{good['txid']}:0"}
    assert await pending(bad["txid"]) is True

    # A transaction already posted whose stored fee stops adding up: its movements go.
    spend = chain.add(
        make_tx(3, [(OTHERS[1], 5_000)], spends=[(receiving(0), 6_000)], height=TIP - 50, fee=1_000)
    )
    s = spend["txid"]
    await wallet.sync()
    assert {f"{s}:0", f"{s}:fee"} <= set(await wallet.live())
    # As the fetch stores a changed row: pending.
    await session.execute(
        text("update finance.bitcoin_transactions set fee = 1, pending = true where txid = :t"),
        {"t": s},
    )

    later = await wallet.normalize({s})

    assert FEE_FLAG in later.flagged
    ledger = await wallet.ledger()
    for key in (f"{s}:0", f"{s}:fee"):
        assert (ledger[key].status, ledger[key].voided_by) == ("void", "connector")
    assert await pending(s) is True
    fee_link = await wallet.row(
        "select fee_transaction_id from finance.bitcoin_transactions"
        " where connection_id = :c and txid = :t",
        t=s,
    )
    assert fee_link.fee_transaction_id is None
