"""The Bitcoin connector: the credential, the six-hour cadence, the order of fetch, normalize
and the balance check, the flags it publishes, and its registration."""

import uuid
from collections.abc import Callable
from datetime import timedelta
from typing import Any

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from lararium.core.commands import CommandContext, command_context
from lararium.core.connections import (
    ConnectionRecord,
    create_connection,
    delete_credential,
    get_connection,
    record_sync_success,
    store_credential,
)
from lararium.core.connectors import SyncContext
from lararium.core.errors import CredentialRejectedError
from lararium.core.json_types import JSONValue
from lararium.modules import registry
from lararium.modules.finance.account_types import Api, account_type
from lararium.modules.finance.bitcoin.keys import WalletInput, parse_input
from lararium.modules.finance.bitcoin.normalize import FEE_FLAG, LEFT_CHAIN_FLAG
from lararium.modules.finance.bitcoin.sync import (
    CONFLICT_FLAG,
    DESCRIPTOR_PURPOSE,
    FLAGGED_PREFIX,
    NO_USED_FLAG,
    SYNC_EVERY,
    BitcoinConnector,
    PriceStep,
    input_json,
    load_input,
)
from lararium.modules.finance.identifiers import assign_identifier

from .fake_esplora import BASE, STRANGER, TIP, FakeEsplora, make_tx
from .test_fetch import CONNECTOR, KEY, SPEC, change, receiving
from .test_normalize import OTHERS, Synced, at, btc

NOW = at(TIP) + timedelta(hours=1)


class Notifier:
    def __init__(self) -> None:
        self.messages: list[str] = []

    async def send(self, text: str) -> None:
        self.messages.append(text)


async def _no_prices(ctx: SyncContext, account_id: uuid.UUID) -> list[str]:
    return []


class Wallet(Synced):
    """A Bitcoin connection with its credential stored, synced by the connector itself, its
    cursor recorded as the worker records it."""

    def __init__(self, started: Synced) -> None:
        super().__init__(started.session, started.connection, started.chain)
        self.notifier = Notifier()
        self.now = NOW
        # Prices are test_prices.py's: here no Binance is asked.
        self.prices: PriceStep = _no_prices
        self.handler: Callable[[httpx.Request], Any] = started.chain.handler

    @classmethod
    async def create(
        cls,
        session: AsyncSession,
        chain: FakeEsplora,
        spec: WalletInput = SPEC,
        config: dict[str, JSONValue] | None = None,
    ) -> "Wallet":
        async with command_context(session, CONNECTOR, "test", {}) as cmd:
            connection = await create_connection(
                cmd, connector="bitcoin", display_name="Bitcoin wallet", config=config or {}
            )
            await store_credential(
                cmd, KEY, connection.id, DESCRIPTOR_PURPOSE, input_json(spec), None
            )
        return cls(Synced(session, connection, chain))

    def connector(self) -> BitcoinConnector:
        return BitcoinConnector(
            now=lambda: self.now, sleep=self.chain.sleep, clock=self.chain.clock, prices=self.prices
        )

    async def run(self, *, requested: bool = False) -> JSONValue:
        async with httpx.AsyncClient(transport=httpx.MockTransport(self.handler)) as http:
            ctx = SyncContext(
                esplora_url=BASE,
                command=CommandContext(self.session, CONNECTOR, uuid.uuid4()),
                connection=await get_connection(self.session, self.connection.id),
                sync_id=uuid.uuid4(),
                master_key=KEY,
                http=http,
                notifier=self.notifier,
                session_factory=async_sessionmaker(
                    bind=self.session.bind, join_transaction_mode="create_savepoint"
                ),
                requested=requested,
            )
            cursor = await self.connector().sync(ctx)
            await record_sync_success(ctx.command, self.connection.id, cursor)
        await self.session.flush()
        return cursor

    async def record(self) -> ConnectionRecord:
        return await get_connection(self.session, self.connection.id)

    async def warnings(self) -> list[str]:
        return (await self.record()).warnings


def _flag(reason: str) -> str:
    return FLAGGED_PREFIX + reason


async def test_sync_end_to_end(session) -> None:
    chain = FakeEsplora()
    receipt = chain.add(make_tx(1, [(receiving(0), 100_000)], height=TIP - 200))
    spend = chain.add(
        make_tx(
            2,
            [(STRANGER, 60_000), (change(0), 39_859)],
            spends=[(receiving(0), 100_000)],
            height=TIP - 100,
        )
    )
    wallet = await Wallet.create(session, chain)

    cursor = await wallet.run()

    r, s = receipt["txid"], spend["txid"]
    assert set(await wallet.live()) == {f"{r}:0", f"{s}:0", f"{s}:fee"}
    assert await wallet.balance() == btc(39_859)
    assert await wallet.warnings() == []
    assert wallet.notifier.messages == []
    assert isinstance(cursor, dict)
    assert cursor["synced_at"] == NOW.isoformat()
    assert cursor["reconciliation"] == {}
    record = await wallet.record()
    assert record.cursor == cursor


async def test_sync_waits_six_hours_unless_requested(session) -> None:
    chain = FakeEsplora()
    chain.add(make_tx(1, [(receiving(0), 100_000)]))
    wallet = await Wallet.create(session, chain)
    first = await wallet.run()
    asked = len(chain.requests)
    chain.add(make_tx(2, [(receiving(1), 5_000)]))

    wallet.now = NOW + timedelta(hours=1)
    assert await wallet.run() == first
    assert len(chain.requests) == asked
    assert len(await wallet.live()) == 1

    later = await wallet.run(requested=True)

    assert len(chain.requests) > asked
    assert len(await wallet.live()) == 2
    assert isinstance(later, dict) and later["synced_at"] == wallet.now.isoformat()

    asked = len(chain.requests)
    wallet.now += SYNC_EVERY
    await wallet.run()
    assert len(chain.requests) > asked


async def test_first_sync_with_nothing_used_flags(session) -> None:
    wallet = await Wallet.create(session, FakeEsplora())

    await wallet.run()

    assert await wallet.warnings() == [_flag(NO_USED_FLAG)]
    assert wallet.notifier.messages == [f"Bitcoin wallet: {_flag(NO_USED_FLAG)}"]

    # Still nothing: the warning stands and is not told again; a receipt clears it.
    await wallet.run(requested=True)
    assert await wallet.warnings() == [_flag(NO_USED_FLAG)]
    assert len(wallet.notifier.messages) == 1
    wallet.chain.add(make_tx(1, [(receiving(3), 5_000)]))
    await wallet.run(requested=True)
    assert await wallet.warnings() == []


async def test_an_address_held_by_another_account_is_flagged_without_naming_it(
    session, run, make_account
) -> None:
    chain = FakeEsplora()
    chain.add(make_tx(1, [(receiving(0), 30_000)]))
    chain.add(make_tx(2, [(receiving(1), 20_000)]))
    manual = await make_account(name="Old wallet", subtype="wallet", balance_mode="transactions")
    await run(assign_identifier, manual.id, "chain_address", receiving(0))
    await run(assign_identifier, manual.id, "chain_address", receiving(1))
    wallet = await Wallet.create(session, chain)

    await wallet.run()

    assert await wallet.warnings() == [_flag(CONFLICT_FLAG)]
    told = " ".join(wallet.notifier.messages) + str((await wallet.record()).cursor)
    assert receiving(0) not in told and receiving(1) not in told


async def test_unknown_fields_are_flagged(session) -> None:
    chain = FakeEsplora()
    tx = chain.add(make_tx(1, [(receiving(0), 30_000)]))
    tx["novel"] = 1
    tx["vout"][0]["extra"] = "x"
    wallet = await Wallet.create(session, chain)

    await wallet.run()

    assert await wallet.warnings() == [
        _flag("unknown field novel"),
        _flag("unknown field vout[].extra"),
    ]


async def test_normalizer_flags_are_published_and_kept(session) -> None:
    chain = FakeEsplora()
    chain.add(make_tx(1, [(receiving(0), 100_000)], height=TIP - 200))
    gone = chain.add(make_tx(2, [(receiving(1), 5_000)], height=TIP - 2))
    # The inputs less the outputs are 1,000; the server says the fee is 900.
    chain.add(
        make_tx(
            3,
            [(OTHERS[0], 60_000), (change(0), 39_000)],
            spends=[(receiving(0), 100_000)],
            fee=900,
        )
    )
    wallet = await Wallet.create(session, chain)
    await wallet.run()
    assert _flag(FEE_FLAG) in await wallet.warnings()

    chain.move(gone["txid"], None)
    await wallet.run(requested=True)

    # The fee is flagged again each sync while it waits; the reorg once, and it stays.
    warnings = await wallet.warnings()
    assert _flag(FEE_FLAG) in warnings and _flag(LEFT_CHAIN_FLAG) in warnings
    await wallet.run(requested=True)
    warnings = await wallet.warnings()
    assert _flag(FEE_FLAG) in warnings and _flag(LEFT_CHAIN_FLAG) in warnings


async def test_a_receipt_confirming_between_the_recheck_and_the_scan_posts(session) -> None:
    """A receipt seen in the mempool, rechecked still there, confirms before its address's
    stats are read: chain + mempool is the count the last history held, but the confirmed
    count is not, so the history is asked again and the receipt posts in that sync."""
    chain = FakeEsplora()
    receipt = chain.add(make_tx(1, [(receiving(0), 30_000)], height=None))
    wallet = await Wallet.create(session, chain)
    await wallet.run()
    assert await wallet.live() == {}

    serve = chain.handler

    def confirm_after_recheck(request: httpx.Request) -> httpx.Response:
        response = serve(request)
        if request.url.path.endswith(f"/tx/{receipt['txid']}/status"):
            chain.move(receipt["txid"], TIP - 1)
        return response

    wallet.handler = confirm_after_recheck
    await wallet.run(requested=True)

    assert chain.asked_status() == [receipt["txid"]]
    assert set(await wallet.live()) == {f"{receipt['txid']}:0"}
    assert await wallet.balance() == btc(30_000)
    assert await wallet.warnings() == []


async def test_a_block_stamped_after_now_is_checked_at_its_time(session) -> None:
    chain = FakeEsplora()
    chain.add(make_tx(1, [(receiving(0), 100_000)], height=TIP))
    wallet = await Wallet.create(session, chain)
    # The block's time is ahead of the clock, as a miner's may be by up to two hours.
    wallet.now = at(TIP) - timedelta(hours=1)

    await wallet.run()

    assert await wallet.balance() == btc(100_000)
    assert await wallet.warnings() == []


async def test_gap_limits_come_from_the_config(session) -> None:
    chain = FakeEsplora()
    chain.add(make_tx(1, [(receiving(5), 30_000)]))
    gaps: dict[str, JSONValue] = {"d0": {"0": 5, "1": 2}}
    wallet = await Wallet.create(session, chain, config={"gaps": gaps})

    await wallet.run()

    # Five unused receiving addresses end the scan before index 5: nothing is found.
    assert len(chain.asked_stats()) == 5 + 2
    assert await wallet.live() == {}


async def test_an_address_list_wallet_syncs(session) -> None:
    chain = FakeEsplora()
    chain.add(make_tx(1, [(receiving(7), 30_000)]))
    spec = parse_input(WalletInput("addresses", addresses=(receiving(7),)))
    wallet = await Wallet.create(session, chain, spec)

    await wallet.run()

    assert await wallet.balance() == btc(30_000)
    assert chain.asked_stats() == [receiving(7)]


@pytest.mark.parametrize("stored", [None, "not json", '{"form": "electrum"}', '{"form": 1}'])
async def test_a_missing_or_unreadable_credential_is_rejected(session, stored) -> None:
    chain = FakeEsplora()
    wallet = await Wallet.create(session, chain)
    async with command_context(session, CONNECTOR, "test", {}) as cmd:
        if stored is None:
            await delete_credential(cmd, wallet.connection.id, DESCRIPTOR_PURPOSE)
        else:
            await store_credential(cmd, KEY, wallet.connection.id, DESCRIPTOR_PURPOSE, stored, None)

    with pytest.raises(CredentialRejectedError) as raised:
        await wallet.run()

    assert str(raised.value) == "The Bitcoin wallet's stored keys or addresses could not be read"
    assert chain.requests == []


def test_input_json_round_trips() -> None:
    multisig = WalletInput("electrum", "multisig", ("a", "b"), 2)
    for spec in (SPEC, multisig, WalletInput("addresses", addresses=("x", "y"))):
        assert load_input(input_json(spec)) == spec


def test_wallet_type_is_listed() -> None:
    found = account_type("bitcoin_wallet")
    assert found is not None
    assert found.api == Api("bitcoin", {})
    assert found.name == "Bitcoin wallet"
    assert found.api.connector == "bitcoin"


def test_registry_has_bitcoin() -> None:
    connector = registry.connector("bitcoin")
    assert isinstance(connector, BitcoinConnector)
    assert connector.capabilities == frozenset({"balances", "transactions"})


async def test_check_access_needs_no_secret() -> None:
    ctx: Any = None
    assert await BitcoinConnector().check_access(ctx) is True
