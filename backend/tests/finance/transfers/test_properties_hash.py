"""A property test of hash evidence (pairs.HASH, Bitcoin wallet design §5.8) on generated
on-chain movements: records on three connected accounts colliding at random in their hash,
address and amount, the addresses registered to an account or to none, a sending record
naming the address it paid or not. Each arrives as a connector's record (transfer_contra,
then upsert_connector_transaction), in a drawn order, each arrival followed by heal as the
worker runs it.

- H1: the matched transfers are exactly the pairs of a sending and a receiving record on
  different accounts with the same hash and address and opposite, equal amounts, the address
  registered to nobody or to the receiving account, each the other's only such candidate.
  Every record is booked on a day of its own, days apart, so the booking window never
  decides, and no two candidates are alike enough to be paired in a fixed order
  (pairs._twins).
- H2: no record is in two transfers, and find_transfer_violations and
  find_unbalanced_transactions find nothing.

Each run is a transaction on a connection of its own, rolled back at the end. Profiles are
tests/finance/conftest.py's. Hashes and addresses are invented."""

from collections import Counter, defaultdict
from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID

import pytest
from hypothesis import event, given, note
from hypothesis import strategies as st
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession

from lararium.core.actors import Actor
from lararium.core.commands import command_context
from lararium.core.connections import create_connection
from lararium.db import create_engine
from lararium.modules.finance.accounts import create_account
from lararium.modules.finance.identifiers import assign_identifier, get_or_create_identifier
from lararium.modules.finance.instruments import create_instrument, get_currency
from lararium.modules.finance.ledger import (
    ChainFields,
    CounterpartyFields,
    PostingInput,
    TransactionInput,
    find_unbalanced_transactions,
    upsert_connector_transaction,
)
from lararium.modules.finance.system_accounts import get_or_create_system_account
from lararium.modules.finance.transfers import (
    find_transfer_violations,
    heal_transfers,
    list_transfers,
    transfer_contra,
)

USER = Actor("user", "test")
WORKER = Actor("system", "transfers")
ACCOUNTS = ("X", "Y", "Z")
HASHES = ("c3" * 32, "d4" * 32)
ADDRESSES = ("bc1qhashpropertyaaaaaaaaaaaaaaaaaaaaaaaa", "bc1qhashpropertybbbbbbbbbbbbbbbbbbbbbbbb")
START = datetime(2026, 11, 2, 12, tzinfo=UTC)


@dataclass(frozen=True)
class Movement:
    account: int  # index into ACCOUNTS
    tx_hash: int  # index into HASHES
    address: int | None  # index into ADDRESSES; None for a movement at no address
    quantity: Decimal  # signed
    names: bool  # a sending record names the address it paid (a wallet's payment)


@dataclass(frozen=True)
class Scenario:
    movements: tuple[Movement, ...]
    holders: tuple[int | None, ...]  # per address, the account it is registered to


_AMOUNTS = (Decimal("0.01"), Decimal("0.02"))
_ACCOUNT = st.integers(0, len(ACCOUNTS) - 1)
_HASH = st.integers(0, len(HASHES) - 1)
_ADDRESS = st.integers(0, len(ADDRESSES) - 1)


@st.composite
def scenarios(draw: st.DrawFn) -> Scenario:
    """A few chain movements' two sides (a payment and its receipt, on two accounts or the
    same one), then lone movements colliding with them in hash, address and amount, all in
    a drawn order."""
    movements: list[Movement] = []
    for _ in range(draw(st.integers(1, 3), label="sides")):
        tx_hash, address = draw(_HASH), draw(_ADDRESS)
        amount = draw(st.sampled_from(_AMOUNTS))
        sender = draw(_ACCOUNT)
        # Mostly another account (the first choices, which shrinking prefers).
        receiver = (sender + draw(st.sampled_from([1, 2, 0]))) % len(ACCOUNTS)
        movements += [
            Movement(sender, tx_hash, address, -amount, draw(st.booleans())),
            Movement(receiver, tx_hash, address, amount, False),
        ]
    lone = st.builds(
        Movement,
        account=_ACCOUNT,
        tx_hash=_HASH,
        address=st.one_of(_ADDRESS, st.none()),
        quantity=st.sampled_from([q for a in _AMOUNTS for q in (a, -a)]),
        names=st.booleans(),
    )
    movements += draw(st.lists(lone, max_size=3), label="lone")
    holder = st.sampled_from([None, *range(len(ACCOUNTS))])
    return Scenario(
        movements=tuple(draw(st.permutations(movements), label="days")),
        holders=tuple(draw(holder) for _ in ADDRESSES),
    )


def expected_pairs(scenario: Scenario) -> set[tuple[int, int]]:
    """H1's pairs, (sending, receiving) by movement index."""
    found: defaultdict[int, set[int]] = defaultdict(set)
    ms = scenario.movements
    for s, sent in enumerate(ms):
        for r, received in enumerate(ms):
            if (
                sent.quantity < 0
                and received.quantity == -sent.quantity
                and sent.account != received.account
                and sent.address is not None
                and (sent.tx_hash, sent.address) == (received.tx_hash, received.address)
                and scenario.holders[sent.address] in (None, received.account)
            ):
                found[s].add(r)
                found[r].add(s)
    return {
        (s, r)
        for s, rs in found.items()
        if ms[s].quantity < 0 and len(rs) == 1
        for r in rs
        if found[r] == {s}
    }


def describe(scenario: Scenario) -> str:
    lines = [
        f"{ACCOUNTS[m.account]} {m.quantity:+} hash {m.tx_hash} address {m.address}"
        f"{' naming it' if m.names and m.quantity < 0 else ''}"
        for m in scenario.movements
    ]
    holders = [None if h is None else ACCOUNTS[h] for h in scenario.holders]
    return "\n".join([*lines, f"address holders: {holders}"])


@pytest.fixture
async def connection(backend_url: str, migrated: None) -> AsyncIterator[AsyncConnection]:
    engine = create_engine(backend_url)
    try:
        async with engine.connect() as conn:
            yield conn
    finally:
        await engine.dispose()


class Books:
    """One run of a scenario against the engine, in one session."""

    def __init__(self, session: AsyncSession, scenario: Scenario) -> None:
        self.session = session
        self.scenario = scenario
        self.accounts: list[UUID] = []
        self.connections: list[UUID] = []
        self.identifiers: list[UUID] = []
        self.posted: dict[int, UUID] = {}
        self.btc: UUID | None = None

    async def run(self, fn: Any, /, *args: Any, **kwargs: Any) -> Any:
        async with command_context(self.session, USER, "test", {}) as ctx:
            return await fn(ctx, *args, **kwargs)

    async def setup(self) -> None:
        for key in ("unassigned", "in_transit", "opening_balances"):
            await self.run(get_or_create_system_account, key)
        usd = await get_currency(self.session, "USD")
        coin = await self.run(
            create_instrument,
            kind="crypto",
            symbol="BTC",
            name="Bitcoin",
            decimals=8,
            quote_instrument_id=usd.id,
            identifiers=[],
        )
        self.btc = coin.id
        for name in ACCOUNTS:
            connection = await self.run(
                create_connection, connector="bitcoin", display_name=name, config={}
            )
            account = await self.run(
                create_account,
                name=name,
                ledger_type="asset",
                subtype="wallet",
                balance_mode="transactions",
                source_connection_id=connection.id,
            )
            self.connections.append(connection.id)
            self.accounts.append(account.id)
        for address, holder in zip(ADDRESSES, self.scenario.holders, strict=True):
            if holder is None:
                identifier = await self.run(get_or_create_identifier, "chain_address", address)
            else:
                record = await self.run(
                    assign_identifier, self.accounts[holder], "chain_address", address
                )
                identifier = record.id
            self.identifiers.append(identifier)

    async def arrive(self, index: int) -> None:
        m = self.scenario.movements[index]
        assert self.btc is not None
        connection_id, account_id = self.connections[m.account], self.accounts[m.account]
        key = f"movement_{index}"
        names = (
            self.identifiers[m.address]
            if m.names and m.quantity < 0 and m.address is not None
            else None
        )
        contra = await transfer_contra(
            self.session,
            source=("connection", f"{connection_id}:{key}"),
            own_account_id=account_id,
            counterparty_identifier_id=names,
            other_postings=0,
        )
        record, _ = await self.run(
            upsert_connector_transaction,
            TransactionInput(
                # Days apart: the booking window never decides, nor are two records alike.
                occurred_at=START + timedelta(days=3 * index),
                description="Bitcoin movement",
                origin="connector",
                connection_id=connection_id,
                external_id=key,
                postings=[
                    PostingInput(account_id, self.btc, m.quantity),
                    PostingInput(contra, self.btc, -m.quantity),
                ],
                counterparty=CounterpartyFields(identifier_id=names),
                chain=ChainFields(
                    "bitcoin",
                    HASHES[m.tx_hash],
                    None if m.address is None else ADDRESSES[m.address],
                ),
            ),
        )
        self.posted[index] = record.id

    async def heal_checked(self) -> None:
        async with command_context(self.session, WORKER, "finance.heal_transfers", {}) as ctx:
            await heal_transfers(ctx)
        assert await find_transfer_violations(self.session) == []
        assert await find_unbalanced_transactions(self.session) == []


@asynccontextmanager
async def isolated(conn: AsyncConnection, scenario: Scenario) -> AsyncGenerator[Books]:
    outer = await conn.begin()
    session = AsyncSession(bind=conn, join_transaction_mode="create_savepoint")
    try:
        books = Books(session, scenario)
        await books.setup()
        yield books
    finally:
        await session.close()
        await outer.rollback()


@given(scenario=scenarios(), data=st.data())
async def test_hash_pairs_exactly_matching_records(
    connection: AsyncConnection, scenario: Scenario, data: st.DataObject
) -> None:
    """H1 and H2."""
    note(describe(scenario))
    expected = expected_pairs(scenario)
    event(f"pairs: {len(expected)}")
    order = data.draw(st.permutations(range(len(scenario.movements))), label="arrival order")
    async with isolated(connection, scenario) as books:
        for index in order:
            await books.arrive(index)
            await books.heal_checked()
        transfers = [t for t in await list_transfers(books.session) if t.status != "rejected"]
        by_id = {transaction_id: index for index, transaction_id in books.posted.items()}
        matched = {
            (by_id[t.out_transaction_id], by_id[t.in_transaction_id])
            for t in transfers
            if t.status == "matched"
            and t.out_transaction_id is not None
            and t.in_transaction_id is not None
        }
        assert matched == expected
        legs = Counter(
            x for t in transfers for x in (t.out_transaction_id, t.in_transaction_id) if x
        )
        assert all(n == 1 for n in legs.values()), legs
