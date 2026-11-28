"""Checking a Bitcoin wallet's posted balance against the sums its addresses report (design
5.6): one `reported` snapshot a sync, one warning a difference, told once, cleared when the
books agree again, and never a correction."""

from dataclasses import replace
from datetime import date, datetime, timedelta
from decimal import Decimal

import httpx
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from lararium.core.connections import get_connection, merge_cursor
from lararium.core.connectors import SyncContext
from lararium.core.timeutil import local_date
from lararium.modules.finance.accounts import get_account
from lararium.modules.finance.bitcoin import reconcile
from lararium.modules.finance.bitcoin.fetch import fetch
from lararium.modules.finance.bitcoin.normalize import normalize
from lararium.modules.finance.connector_run import LONDON
from lararium.modules.finance.valuation import balances_asof

from .fake_esplora import TIP, FakeEsplora, make_tx
from .test_fetch import change, receiving
from .test_normalize import WALLET, Synced, at, btc

NOW = at(TIP) + timedelta(hours=1)
DAY = local_date(NOW, LONDON)


class Notifier:
    def __init__(self) -> None:
        self.messages: list[str] = []

    async def send(self, text: str) -> None:
        self.messages.append(text)


class Reconciled(Synced):
    """A sync as the connector will run it: fetch, normalize, check, publish."""

    def __init__(self, started: Synced) -> None:
        super().__init__(started.session, started.connection, started.chain)
        self.notifier = Notifier()

    async def context(self, http: httpx.AsyncClient) -> SyncContext:
        return replace(await super().context(http), notifier=self.notifier)  # pyright: ignore[reportAttributeAccessIssue]

    async def reconcile(self, now: datetime = NOW) -> list[reconcile.Mismatch]:
        async with httpx.AsyncClient(transport=httpx.MockTransport(self.chain.handler)) as http:
            ctx = await self.context(http)
            found = await fetch(ctx, self.chain.client(http), WALLET)
            await normalize(ctx, found.changed_txids | found.left_chain)
            account_id = await self.account_id()
            name = (await get_account(self.session, account_id)).name
            mismatches = await reconcile.check(ctx, account_id, name, found.scanned, now)
            cursor = await reconcile.publish(ctx, mismatches)
            await merge_cursor(ctx.command, self.connection.id, cursor)
        await self.session.flush()
        return mismatches

    async def warnings(self) -> list[str]:
        return (await get_connection(self.session, self.connection.id)).warnings


async def _start(session: AsyncSession, chain: FakeEsplora) -> Reconciled:
    return Reconciled(await Synced.start(session, chain))


def _warning(difference: str) -> str:
    return (
        f"Bitcoin wallet BTC differs from the Bitcoin blockchain on {DAY.isoformat()}"
        f" by {difference}"
    )


def _hide(chain: FakeEsplora, hidden: str) -> None:
    """The history leaves out a transaction the address stats still count."""
    history = chain.history
    chain.history = lambda address: [  # type: ignore[method-assign]
        tx for tx in history(address) if tx["txid"] != hidden
    ]


async def _snapshots(session: AsyncSession) -> list[tuple[Decimal, datetime, date]]:
    rows = await session.execute(
        text(
            "select quantity, as_of, as_of_date from finance.balance_snapshots"
            " where kind = 'reported' order by as_of"
        )
    )
    return [(r.quantity, r.as_of, r.as_of_date) for r in rows]


async def test_consistent_wallet_has_no_warning(session: AsyncSession) -> None:
    chain = FakeEsplora()
    chain.add(make_tx(1, [(receiving(0), 100_000)], height=TIP - 200))
    chain.add(
        make_tx(
            2,
            [(change(0), 39_000), ("bc1qother" + "x" * 33, 60_000)],
            spends=[(receiving(0), 100_000)],
            fee=1_000,
        )
    )
    wallet = await _start(session, chain)

    assert await wallet.reconcile() == []

    assert await wallet.warnings() == []
    assert wallet.notifier.messages == []
    assert await wallet.balance() == btc(39_000)
    assert await _snapshots(session) == [(btc(39_000), NOW, DAY)]

    # The same figure again, the same day: no new snapshot.
    await wallet.reconcile(NOW + timedelta(minutes=5))
    assert len(await _snapshots(session)) == 1


async def test_missing_transaction_warns_once(session: AsyncSession) -> None:
    chain = FakeEsplora()
    chain.add(make_tx(1, [(receiving(0), 100_000)], height=TIP - 200))
    missed = chain.add(make_tx(2, [(receiving(1), 50_000)], height=TIP - 100))
    _hide(chain, missed["txid"])
    wallet = await _start(session, chain)

    found = await wallet.reconcile()

    expected = _warning("-0.00050000")
    assert [m.warning for m in found] == [expected]
    assert await wallet.warnings() == [expected]
    assert wallet.notifier.messages == [f"Bitcoin wallet: {expected}"]
    assert await _snapshots(session) == [(btc(150_000), NOW, DAY)]

    # The same state again: the warning stands and nothing more is said.
    await wallet.reconcile(NOW + timedelta(hours=1))
    assert await wallet.warnings() == [expected]
    assert len(wallet.notifier.messages) == 1


async def test_snapshot_never_changes_the_balance(session: AsyncSession) -> None:
    chain = FakeEsplora()
    chain.add(make_tx(1, [(receiving(0), 100_000)], height=TIP - 200))
    missed = chain.add(make_tx(2, [(receiving(1), 50_000)], height=TIP - 100))
    _hide(chain, missed["txid"])
    wallet = await _start(session, chain)

    await wallet.reconcile()

    account_id = await wallet.account_id()
    assert await wallet.balance() == btc(100_000)
    rows = {r.account_id: r for r in await balances_asof(session, DAY)}
    assert rows[account_id].quantity == btc(100_000)
    snapshot = await session.scalar(
        text("select quantity from finance.balance_snapshots where account_id = :a"),
        {"a": account_id},
    )
    assert snapshot == btc(150_000)


async def test_agreeing_again_clears_the_warning(session: AsyncSession) -> None:
    chain = FakeEsplora()
    chain.add(make_tx(1, [(receiving(0), 100_000)], height=TIP - 200))
    missed = chain.add(make_tx(2, [(receiving(1), 50_000)], height=TIP - 100))
    chain.history, shown = chain.history, chain.history  # restored below
    _hide(chain, missed["txid"])
    wallet = await _start(session, chain)
    await wallet.reconcile()
    assert len(await wallet.warnings()) == 1

    # The server returns the history again; a new transaction makes the address be read anew.
    chain.history = shown  # type: ignore[method-assign]
    chain.add(make_tx(3, [(receiving(1), 10_000)], height=TIP - 50))
    assert await wallet.reconcile(NOW + timedelta(hours=1)) == []

    assert await wallet.warnings() == []
    assert len(wallet.notifier.messages) == 1
    assert await wallet.balance() == btc(160_000)
