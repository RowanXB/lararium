"""A Binance withdrawal to the owner's Bitcoin wallet pairs with the wallet's receipt by its
hash through the real normalizers, in the order production runs them (Bitcoin wallet design
§5.8): Binance's withdrawal is normalized while its destination is an address nobody holds;
the wallet's sync registers that address and posts the receipt; a heal pairs the two. Values,
hashes and times are invented; the wallet is the BIP84 test vector's."""

from datetime import timedelta
from decimal import Decimal

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from lararium.modules.finance.identifiers import identifier_account, list_identifiers
from lararium.modules.finance.pricing import record_fx_rate, record_price
from lararium.modules.finance.transfers import list_transfers
from lararium.modules.finance.transfers.pairs import HASH
from tests.finance.binance.normalizing import normalize_rows
from tests.finance.binance.syncing import binance_connection
from tests.finance.binance.test_records_capital import _text_time, _withdrawal
from tests.finance.bitcoin.fake_esplora import TIP, FakeEsplora, make_tx
from tests.finance.bitcoin.test_fetch import receiving
from tests.finance.bitcoin.test_normalize import at
from tests.finance.bitcoin.test_sync import Wallet

from .conftest import (
    Heal,
    Run,
    SystemIds,
    assert_consistent,
    balances_on,
    contra_account,
    net_worth_on,
)
from .test_hash import _relation

HEIGHT = TIP - 100  # the block the wallet's receipt is in
SENT_SATS = 1_000_000


async def _transaction_id(session: AsyncSession, key: str):
    return await session.scalar(
        text("select id from finance.transactions where external_id = :k and status <> 'void'"),
        {"k": key},
    )


async def _account_of(session: AsyncSession, connection_id) -> object:
    return await session.scalar(
        text("select id from finance.accounts where source_connection_id = :c"),
        {"c": connection_id},
    )


@pytest.mark.parametrize("spelling", ["lower", "upper"])
async def test_binance_withdrawal_pairs_with_the_synced_wallet_receipt(
    session: AsyncSession,
    run: Run,
    ids: SystemIds,
    heal: Heal,
    usd,
    gbp,
    spelling: str,
) -> None:
    """Binance sends 0.01 BTC to the wallet's first receiving address two days before the
    block confirms it, and charges 0.0001 BTC: one matched transfer, the fee its own
    transaction, and net worth down by the fee only. Binance may write the bech32 address in
    upper case, which is the same address (BIP173): the pair is found either way."""
    chain = FakeEsplora()
    receipt_tx = chain.add(make_tx(1, [(receiving(0), SENT_SATS)], height=HEIGHT))
    tx_hash = receipt_tx["txid"]
    confirmed_at = at(HEIGHT)
    applied_ms = int((confirmed_at - timedelta(days=2)).timestamp()) * 1000
    address = receiving(0).upper() if spelling == "upper" else receiving(0)

    # (a) Binance's withdrawal, normalized while nobody holds the address.
    binance = await binance_connection(run)
    row = _withdrawal(
        6,
        coin="BTC",
        amount="0.01",
        transactionFee="0.0001",
        network="BTC",
        txId=tx_hash,
        address=address,
        applyTime=_text_time(applied_ms),
        completeTime=_text_time(applied_ms + 3_600_000),
    )
    await normalize_rows(session, binance.id, ("withdrawals", [row]))
    key = f"withdrawal:{row['id']}"
    withdrawal = await _transaction_id(session, key)
    fee = await _transaction_id(session, f"{key}:fee")
    spot = await _account_of(session, binance.id)
    assert withdrawal is not None and fee is not None
    [identifier] = await list_identifiers(session)
    assert identifier.value == receiving(0)
    assert await identifier_account(session, identifier.id) is None
    assert await contra_account(session, withdrawal, spot) == ids.unassigned

    btc = await session.scalar(
        text(
            "select instrument_id from finance.instrument_identifiers"
            " where scheme = 'binance_asset' and value = 'BTC'"
        )
    )
    first = (confirmed_at - timedelta(days=4)).date()
    await run(
        record_price, instrument_id=btc, quote_instrument_id=usd.id, price=Decimal("60000"),
        as_of_date=first,
    )  # fmt: skip
    await run(
        record_fx_rate, base_instrument_id=usd.id, quote_instrument_id=gbp.id,
        rate=Decimal("0.8"), as_of_date=first,
    )  # fmt: skip
    before = await net_worth_on(session, first)

    # (b) The wallet's sync: its fetch registers the address, its normalize posts the receipt.
    wallet = await Wallet.create(session, chain)
    await wallet.run()
    account = await _account_of(session, wallet.connection.id)
    assert await identifier_account(session, identifier.id) == account
    receipt = await _transaction_id(session, f"{tx_hash}:0")
    assert receipt is not None

    # (c) The heal pairs them by the hash.
    await heal()
    [transfer] = await list_transfers(session)
    assert (transfer.status, transfer.out_transaction_id, transfer.in_transaction_id) == (
        "matched",
        withdrawal,
        receipt,
    )
    assert (transfer.from_account_id, transfer.to_account_id) == (spot, account)
    assert (await _relation(session)).tiers[withdrawal][receipt] == HASH
    assert await contra_account(session, withdrawal, spot) == ids.in_transit
    assert await contra_account(session, receipt, account) == ids.in_transit
    assert await contra_account(session, fee, spot) == ids.unassigned
    later = confirmed_at.date() + timedelta(days=1)
    assert (await balances_on(session, later)).get(ids.in_transit, 0) == 0
    fee_worth = Decimal("0.0001") * Decimal("60000") * Decimal("0.8")
    # From the day Binance sent it, through the days in transit, to the day after the block.
    day = (confirmed_at - timedelta(days=2)).date()
    while day <= later:
        assert await net_worth_on(session, day) == before - fee_worth, day
        day += timedelta(days=1)
    await assert_consistent(session)
