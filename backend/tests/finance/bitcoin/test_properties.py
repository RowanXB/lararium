"""Property tests pinning the Bitcoin normalizer's invariant on random chains (strategies.py),
fetched from the fake server and normalized as a sync does:

- each chain transaction's live ledger transactions move the wallet's account by its own
  outputs less its own inputs (none while it is unconfirmed);
- the account's balance is then the sum over the wallet's addresses of what the server
  reports as funded less spent on the chain, the figure the balance check compares;
- normalizing everything again writes nothing.

Each example runs in a transaction of its own, rolled back at the end. Profiles:
tests/finance/conftest.py."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from decimal import Decimal

import pytest
from hypothesis import event, given, note
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession

from lararium.db import create_engine
from lararium.modules.finance import models
from lararium.modules.finance.bitcoin.derive import build_wallet
from lararium.modules.finance.bitcoin.keys import WalletInput, parse_input

from .fake_esplora import STRANGER, FakeEsplora, txid
from .strategies import ChainTx, chains
from .test_fetch import change, receiving
from .test_normalize import OTHERS, Synced, btc

OWN = (receiving(0), receiving(1), change(0))
STRANGERS = (STRANGER, *OTHERS[:2])
# An address list: nothing to derive, so an example costs one fetch.
LISTED = build_wallet(parse_input(WalletInput("addresses", addresses=OWN)))


@pytest.fixture
async def connection(backend_url: str, migrated: None) -> AsyncIterator[AsyncConnection]:
    engine = create_engine(backend_url)
    try:
        async with engine.connect() as conn:
            yield conn
    finally:
        await engine.dispose()


@asynccontextmanager
async def isolated(conn: AsyncConnection) -> AsyncIterator[AsyncSession]:
    outer = await conn.begin()
    session = AsyncSession(bind=conn, join_transaction_mode="create_savepoint")
    try:
        yield session
    finally:
        await session.close()
        await outer.rollback()


async def _moved(session: AsyncSession, wallet: Synced, tx: ChainTx) -> Decimal:
    t, p = models.Transaction, models.Posting
    total = await session.scalar(
        select(func.coalesce(func.sum(p.quantity), 0))
        .join(t, t.id == p.transaction_id)
        .where(
            t.connection_id == wallet.connection.id,
            t.tx_hash == txid(tx.n),
            t.status != "void",
            p.account_id == await wallet.account_id(),
        )
    )
    return Decimal(total)


def _shape(tx: ChainTx) -> str:
    own = [address in OWN for address, _ in tx.inputs]
    if tx.height is None:
        return "unconfirmed"
    return "spend" if all(own) else "shared" if any(own) else "receipt"


@given(txs=chains(OWN, STRANGERS))
async def test_movements_sum_to_the_wallets_change(
    connection: AsyncConnection, txs: list[ChainTx]
) -> None:
    note(repr(txs))
    async with isolated(connection) as session:
        chain = FakeEsplora()
        for tx in txs:
            chain.add(tx.build())
        wallet = await Synced.start(session, chain)

        await wallet.sync(LISTED)

        for tx in txs:
            event(_shape(tx))
            expected = 0 if tx.height is None else tx.change(OWN)
            assert await _moved(session, wallet, tx) == btc(expected), tx
        reported = sum(
            chain.stats(a)["chain_stats"]["funded_txo_sum"]
            - chain.stats(a)["chain_stats"]["spent_txo_sum"]
            for a in OWN
        )
        assert await wallet.balance() == btc(reported)

        again = await wallet.normalize(None)
        assert (again.created, again.updated) == (0, 0)
