"""On-chain movements paired by their transaction hash (Bitcoin wallet design §5.8): two own
records on different accounts, in one instrument, with the same chain, hash and address and
opposite, equal amounts are one transfer's two sides (pairs.HASH), whatever the booking
window; unless the address is registered to an account other than the receiving one's (the
registry wins). Records are posted as the Bitcoin wallet's and Binance's normalizers post
them: the movement's chain fields, its contra from transfer_contra, and the counterparty
address it names, if any. Hashes and addresses are invented."""

from collections.abc import Awaitable, Callable
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from lararium.core.connections import create_connection
from lararium.modules.finance.accounts import AccountRecord, create_account
from lararium.modules.finance.identifiers import assign_identifier, get_or_create_identifier
from lararium.modules.finance.instruments import InstrumentRecord
from lararium.modules.finance.ledger import (
    ChainFields,
    CounterpartyFields,
    PostingInput,
    TransactionInput,
    TransactionRecord,
    upsert_connector_transaction,
)
from lararium.modules.finance.pricing import record_fx_rate, record_price
from lararium.modules.finance.system_accounts import system_account_ids
from lararium.modules.finance.transfers import (
    TransferRecord,
    list_transfers,
    load_transfer_settings,
    rematch_identifiers,
    transfer_contra,
    unassign_identifier_and_rematch,
)
from lararium.modules.finance.transfers.pairs import (
    HASH,
    Pairable,
    hash_tier,
    pair_relation,
)

from .conftest import (
    Connected,
    Heal,
    Run,
    SystemIds,
    assert_consistent,
    balances_on,
    contra_account,
    net_worth_on,
    placeholders,
)

WHEN = datetime(2026, 11, 2, 12, tzinfo=UTC)
H = "a1" * 32  # one chain transaction's hash
OTHER_H = "b2" * 32
A = "bc1qhashtestreceiveaaaaaaaaaaaaaaaaaaaaaa"  # the wallet's receiving addresses
B = "bc1qhashtestreceivebbbbbbbbbbbbbbbbbbbbbb"
D = "bc1qhashtestdepositdddddddddddddddddddddd"  # Binance's deposit address
S = "bc1qhashtestsendersssssssssssssssssssssss"  # an input address of the wallet's


# A connected account of the connector's: (name, connector, subtype).
type MakeConnected = Callable[[str, str, str], Awaitable[Connected]]


@pytest.fixture
def connected(run: Run) -> MakeConnected:
    async def _make(name: str, connector: str, subtype: str) -> Connected:
        connection = await run(create_connection, connector=connector, display_name=name, config={})
        account = await run(
            create_account,
            name=name,
            ledger_type="asset",
            subtype=subtype,
            balance_mode="transactions",
            source_connection_id=connection.id,
        )
        return Connected(connection.id, account)

    return _make


@pytest.fixture
async def binance(connected: MakeConnected) -> Connected:
    return await connected("Binance", "binance", "exchange")


@pytest.fixture
async def wallet(connected: MakeConnected) -> Connected:
    return await connected("Wallet", "bitcoin", "wallet")


class Chain:
    """Posts movements as the normalizers do, on the connection of the account given."""

    def __init__(self, session: AsyncSession, run: Run, btc: InstrumentRecord) -> None:
        self.session, self.run, self.btc = session, run, btc

    async def movement(
        self,
        on: Connected,
        quantity: str,
        *,
        key: str | None = None,
        at: datetime = WHEN,
        tx_hash: str = H,
        address: str | None = A,
        names: UUID | None = None,
    ) -> TransactionRecord:
        """A withdrawal, deposit, receipt or payment: a transfer's possible leg."""
        key = key or f"tx_{uuid4().hex[:8]}"
        contra = await transfer_contra(
            self.session,
            source=("connection", f"{on.connection_id}:{key}"),
            own_account_id=on.account.id,
            counterparty_identifier_id=names,
            other_postings=0,
        )
        amount = Decimal(quantity)
        record, _ = await self.run(
            upsert_connector_transaction,
            TransactionInput(
                occurred_at=at,
                description="Bitcoin movement",
                origin="connector",
                connection_id=on.connection_id,
                external_id=key,
                postings=[
                    PostingInput(on.account.id, self.btc.id, amount),
                    PostingInput(contra, self.btc.id, -amount),
                ],
                counterparty=CounterpartyFields(identifier_id=names),
                chain=ChainFields("bitcoin", tx_hash, address),
            ),
        )
        return record

    async def fee(
        self, on: Connected, quantity: str, *, at: datetime = WHEN, chain: ChainFields | None
    ) -> TransactionRecord:
        """A network or withdrawal fee: its own transaction, never a transfer's leg."""
        unassigned = (await system_account_ids(self.session))["unassigned"]
        amount = Decimal(quantity)
        record, _ = await self.run(
            upsert_connector_transaction,
            TransactionInput(
                occurred_at=at,
                description="Fee",
                origin="connector",
                connection_id=on.connection_id,
                external_id=f"fee_{uuid4().hex[:8]}",
                postings=[
                    PostingInput(on.account.id, self.btc.id, amount, is_fee=True),
                    PostingInput(unassigned, self.btc.id, -amount),
                ],
                counterparty=CounterpartyFields(never_transfer=True),
                chain=chain,
            ),
        )
        return record


@pytest.fixture
def chain(session: AsyncSession, run: Run, btc: InstrumentRecord, ids: SystemIds) -> Chain:
    return Chain(session, run, btc)


async def _address(run: Run, account: AccountRecord | None, value: str) -> UUID:
    """The address's registry row, registered to the account (none: left unassigned)."""
    if account is None:
        return await run(get_or_create_identifier, "chain_address", value)
    return (await run(assign_identifier, account.id, "chain_address", value)).id


def _pairs(transfers: list[TransferRecord]) -> set[tuple[UUID | None, UUID | None, str]]:
    return {(t.out_transaction_id, t.in_transaction_id, t.status) for t in transfers}


async def _relation(session: AsyncSession):
    return await pair_relation(
        session, await system_account_ids(session), await load_transfer_settings(session)
    )


async def test_binance_withdrawal_pairs_with_wallet_receipt(
    session: AsyncSession,
    run: Run,
    chain: Chain,
    binance: Connected,
    wallet: Connected,
    btc: InstrumentRecord,
    usd: InstrumentRecord,
    gbp: InstrumentRecord,
    ids: SystemIds,
    heal: Heal,
) -> None:
    """The withdrawal names the wallet (its address is the wallet's), so it waits for the
    wallet's receipt, which arrives with the block three days later; the receipt names
    nobody, and only the hash pairs it. Binance's fee stays its own transaction."""
    first = WHEN.date() - timedelta(days=1)
    await run(
        record_price, instrument_id=btc.id, quote_instrument_id=usd.id, price=Decimal("60000"),
        as_of_date=first,
    )  # fmt: skip
    await run(
        record_fx_rate, base_instrument_id=usd.id, quote_instrument_id=gbp.id,
        rate=Decimal("0.8"), as_of_date=first,
    )  # fmt: skip
    funded = await chain.movement(
        binance, "0.05", at=WHEN - timedelta(days=1), tx_hash=OTHER_H, address=D
    )
    a = await _address(run, wallet.account, A)
    await heal()
    before = await net_worth_on(session, first)

    withdrawal = await chain.movement(binance, "-0.01", names=a)
    fee = await chain.fee(binance, "-0.0001", chain=None)
    await heal()
    [waiting] = await list_transfers(session)
    assert (waiting.status, waiting.out_transaction_id) == ("awaiting", withdrawal.id)

    receipt = await chain.movement(wallet, "0.01", at=WHEN + timedelta(days=3))
    await heal()
    [transfer] = await list_transfers(session)
    assert (transfer.from_account_id, transfer.to_account_id) == (
        binance.account.id,
        wallet.account.id,
    )
    assert (transfer.status, transfer.out_transaction_id, transfer.in_transaction_id) == (
        "matched",
        withdrawal.id,
        receipt.id,
    )
    relation = await _relation(session)
    assert relation.tiers[withdrawal.id][receipt.id] == HASH
    for record, account in ((withdrawal, binance), (receipt, wallet)):
        assert await contra_account(session, record.id, account.account.id) == ids.in_transit
    assert await contra_account(session, fee.id, binance.account.id) == ids.unassigned
    assert await contra_account(session, funded.id, binance.account.id) == ids.unassigned
    assert (await balances_on(session, WHEN.date() + timedelta(days=4))).get(ids.in_transit, 0) == 0
    fee_worth = Decimal("0.0001") * Decimal("60000") * Decimal("0.8")
    for day in (WHEN.date() + timedelta(days=n) for n in range(5)):
        assert await net_worth_on(session, day) == before - fee_worth, day
    await assert_consistent(session)


@pytest.mark.parametrize("amounts", [("0.01", "0.02"), ("0.01", "0.01")])
async def test_batch_pairs_only_the_matching_output(
    session: AsyncSession,
    run: Run,
    chain: Chain,
    binance: Connected,
    wallet: Connected,
    heal: Heal,
    amounts: tuple[str, str],
) -> None:
    """Binance batches two withdrawals into one chain transaction H, paying the wallet at A
    and at B, and a stranger: each withdrawal pairs with the receipt at its own address,
    even when the two amounts are the same."""
    to_a, to_b = amounts
    a, b = await _address(run, wallet.account, A), await _address(run, wallet.account, B)
    out_a = await chain.movement(binance, f"-{to_a}", names=a, address=A)
    out_b = await chain.movement(binance, f"-{to_b}", names=b, address=B)
    later = WHEN + timedelta(hours=2)
    in_b = await chain.movement(wallet, to_b, key=f"{H}:0", at=later, address=B)
    in_a = await chain.movement(wallet, to_a, key=f"{H}:2", at=later, address=A)
    await heal()
    assert _pairs(await list_transfers(session)) == {
        (out_a.id, in_a.id, "matched"),
        (out_b.id, in_b.id, "matched"),
    }
    await assert_consistent(session)


async def test_same_address_and_amount_twice_is_review(
    session: AsyncSession,
    run: Run,
    chain: Chain,
    binance: Connected,
    wallet: Connected,
    ids: SystemIds,
    heal: Heal,
) -> None:
    """The withdrawal's chain transaction pays the wallet's address A twice the same amount:
    two candidates, which the rule can't choose between."""
    a = await _address(run, wallet.account, A)
    withdrawal = await chain.movement(binance, "-0.01", names=a)
    first = await chain.movement(wallet, "0.01", key=f"{H}:0", at=WHEN + timedelta(hours=1))
    second = await chain.movement(wallet, "0.01", key=f"{H}:1", at=WHEN + timedelta(hours=1))
    await heal()
    [transfer] = await list_transfers(session)
    assert (transfer.status, transfer.review_reason) == ("awaiting", "several_candidates")
    assert (transfer.out_transaction_id, transfer.in_transaction_id) == (withdrawal.id, None)
    relation = await _relation(session)
    assert relation.candidates(withdrawal.id) == sorted([first.id, second.id])
    for receipt in (first, second):
        assert await contra_account(session, receipt.id, wallet.account.id) == ids.unassigned
    await assert_consistent(session)


async def test_wallet_payment_pairs_with_binance_deposit(
    session: AsyncSession,
    run: Run,
    chain: Chain,
    binance: Connected,
    wallet: Connected,
    ids: SystemIds,
    heal: Heal,
) -> None:
    """The wallet pays Binance's deposit address D (registered to nobody); Binance's deposit
    of the same hash, address and amount names its source, an address of the wallet's. The
    wallet's network fee stays the wallet's own transaction."""
    d = await _address(run, None, D)
    s = await _address(run, wallet.account, S)
    payment = await chain.movement(wallet, "-0.02", address=D, names=d)
    fee = await chain.fee(wallet, "-0.00002", chain=ChainFields("bitcoin", H))
    deposit = await chain.movement(
        binance, "0.02", at=WHEN + timedelta(minutes=40), address=D, names=s
    )
    await heal()
    assert _pairs(await list_transfers(session)) == {(payment.id, deposit.id, "matched")}
    assert await contra_account(session, fee.id, wallet.account.id) == ids.unassigned
    await assert_consistent(session)


async def test_two_own_wallets_pair(
    session: AsyncSession, run: Run, chain: Chain, connected: MakeConnected, heal: Heal
) -> None:
    """One wallet pays an address the other scanned (registered to it): its receipt pairs."""
    spending = await connected("Spending", "bitcoin", "wallet")
    savings = await connected("Savings", "bitcoin", "wallet")
    a = await _address(run, savings.account, A)
    receipt = await chain.movement(savings, "0.3", at=WHEN + timedelta(minutes=10))
    await heal()
    payment = await chain.movement(spending, "-0.3", names=a)
    await heal()
    assert _pairs(await list_transfers(session)) == {(payment.id, receipt.id, "matched")}
    await assert_consistent(session)


async def test_registry_wins_over_hash(
    session: AsyncSession,
    run: Run,
    chain: Chain,
    binance: Connected,
    wallet: Connected,
    ids: SystemIds,
    heal: Heal,
) -> None:
    """A is registered to a manual account, so the withdrawal to it is a transfer there, with
    a placeholder; the wallet's receipt at A doesn't take it. Once A moves to the wallet, the
    hash pairs them."""
    cold = await run(
        create_account,
        name="Cold storage",
        ledger_type="asset",
        subtype="wallet",
        balance_mode="transactions",
    )
    a = await _address(run, cold, A)
    withdrawal = await chain.movement(binance, "-0.01", names=a)
    receipt = await chain.movement(wallet, "0.01", at=WHEN + timedelta(hours=1))
    await heal()
    [transfer] = await list_transfers(session)
    assert (transfer.status, transfer.to_account_id) == ("inferred", cold.id)
    assert transfer.placeholder_transaction_id is not None
    assert len(await placeholders(session)) == 1
    assert await contra_account(session, receipt.id, wallet.account.id) == ids.unassigned
    assert receipt.id not in (await _relation(session)).tiers.get(withdrawal.id, {})

    await run(unassign_identifier_and_rematch, a)
    await run(assign_identifier, wallet.account.id, "chain_address", A)
    await run(rematch_identifiers, [a])
    assert _pairs(await list_transfers(session)) == {(withdrawal.id, receipt.id, "matched")}
    assert [p for p in await placeholders(session) if p.status != "void"] == []
    await assert_consistent(session)


async def test_hash_ignores_the_window(
    session: AsyncSession,
    run: Run,
    chain: Chain,
    binance: Connected,
    wallet: Connected,
    heal: Heal,
) -> None:
    """A receipt 30 days after the withdrawal, far past the booking window, still pairs."""
    a = await _address(run, wallet.account, A)
    withdrawal = await chain.movement(binance, "-0.01", names=a)
    receipt = await chain.movement(wallet, "0.01", at=WHEN + timedelta(days=30))
    await heal()
    assert _pairs(await list_transfers(session)) == {(withdrawal.id, receipt.id, "matched")}
    await assert_consistent(session)


_WALLET, _BINANCE = uuid4(), uuid4()
_BTC, _OTHER_COIN = uuid4(), uuid4()
_SENT = Pairable(
    id=uuid4(),
    source=("connection", "binance:1"),
    account_id=_BINANCE,
    instrument_id=_BTC,
    quantity=Decimal("-0.01"),
    occurred_at=WHEN,
    booking_date=date(2026, 11, 2),
    holder=_WALLET,
    bank_transfer=False,
    chain="bitcoin",
    tx_hash=H,
    chain_address=A,
)
_RECEIVED = replace(
    _SENT,
    id=uuid4(),
    source=("connection", "wallet:1"),
    account_id=_WALLET,
    quantity=Decimal("0.01"),
    holder=None,
)


def test_hash_tier_takes_one_transaction_address_and_amount() -> None:
    assert hash_tier(_SENT, _RECEIVED) == hash_tier(_RECEIVED, _SENT) == HASH
    # The registry agrees: the address is the receiving account's.
    assert hash_tier(_SENT, replace(_RECEIVED, address_holder=_WALLET)) == HASH


@pytest.mark.parametrize(
    "received",
    [
        replace(_RECEIVED, tx_hash=OTHER_H),
        replace(_RECEIVED, chain_address=B),
        replace(_RECEIVED, chain="testchain"),
        replace(_RECEIVED, chain=None, tx_hash=None, chain_address=None),
        replace(_RECEIVED, chain_address=None),
        replace(_RECEIVED, quantity=Decimal("0.02")),
        replace(_RECEIVED, quantity=Decimal("-0.01")),
        replace(_RECEIVED, instrument_id=_OTHER_COIN),
        replace(_RECEIVED, account_id=_BINANCE),
        # The registry disagrees: the address is another account's.
        replace(_RECEIVED, address_holder=uuid4()),
        replace(_RECEIVED, address_holder=_BINANCE),
    ],
    ids=[
        "another hash",
        "another address",
        "another chain",
        "no chain",
        "no address",
        "another amount",
        "the same sign",
        "another instrument",
        "the same account",
        "address registered elsewhere",
        "address registered to the sender",
    ],
)
def test_hash_tier_refuses(received: Pairable) -> None:
    sent = replace(_SENT, address_holder=received.address_holder)
    assert hash_tier(sent, received) is None
    assert hash_tier(received, sent) is None


def test_hash_tier_refuses_zero() -> None:
    zero = replace(_SENT, quantity=Decimal(0))
    assert hash_tier(zero, replace(_RECEIVED, quantity=Decimal(0))) is None
