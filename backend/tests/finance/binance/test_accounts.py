"""Binance wallet accounts and asset instruments (design 5.4, 5.5; plan Decisions 6, 8)."""

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from lararium.core.connections import ConnectionRecord, create_connection, get_connection
from lararium.modules.finance.accounts import get_account
from lararium.modules.finance.binance import instruments
from lararium.modules.finance.binance.accounts import wallet_account
from lararium.modules.finance.binance.catalogue import wallet_key
from lararium.modules.finance.binance.models import AssetsSeen
from lararium.modules.finance.binance.parse import parse
from lararium.modules.finance.binance.tables import records, store
from lararium.modules.finance.errors import UnknownCurrencyError
from lararium.modules.finance.instruments import (
    InstrumentIdentifier,
    create_instrument,
    find_by_identifier,
    get_currency,
    get_instrument,
    list_instruments,
)

from .fixtures import build_response, build_row

MOMENT = datetime(2026, 11, 20, 12, 0, tzinfo=UTC)


async def _connection(run) -> ConnectionRecord:
    return await run(
        lambda ctx: create_connection(ctx, connector="binance", display_name="Binance", config={})
    )


async def _account(run, session, connection: ConnectionRecord, wallet: str) -> uuid.UUID:
    fresh = await get_connection(session, connection.id)
    return await run(wallet_account, fresh, wallet)


@pytest.fixture
def account_for(run, session):
    async def _for(connection: ConnectionRecord, wallet: str) -> uuid.UUID:
        return await _account(run, session, connection, wallet)

    return _for


# Wallet accounts


async def test_wallet_accounts_created_once(run, session, account_for) -> None:
    connection = await _connection(run)
    spot = await account_for(connection, "spot")
    earn = await account_for(connection, "earn")
    assert await account_for(connection, "spot") == spot
    assert await account_for(connection, "earn") == earn
    assert spot != earn
    for account_id, name in ((spot, "Binance Spot"), (earn, "Binance Earn")):
        account = await get_account(session, account_id)
        assert account.name == name
        assert (account.ledger_type, account.subtype, account.balance_mode) == (
            "asset",
            "exchange",
            "hybrid",
        )
        assert account.source_kind == "connection"
        assert account.source_connection_id == connection.id
    institution = await session.scalar(
        text("select name from core.entities where id = :id"),
        {"id": (await get_account(session, spot)).institution_entity_id},
    )
    assert institution == "Binance"
    count = await session.scalar(
        text("select count(*) from finance.accounts where source_connection_id = :c"),
        {"c": connection.id},
    )
    assert count == 2
    config = (await get_connection(session, connection.id)).config
    assert config["account_map"] == {"spot": str(spot), "earn": str(earn)}


async def test_loans_is_a_liability(run, session, account_for) -> None:
    connection = await _connection(run)
    loans = await get_account(session, await account_for(connection, wallet_key("Crypto Loans")))
    assert (loans.name, loans.ledger_type, loans.subtype, loans.balance_mode) == (
        "Binance Crypto Loans",
        "liability",
        "other",
        "hybrid",
    )


async def test_every_known_wallet_is_named_after_binance(run, session, account_for) -> None:
    connection = await _connection(run)
    for name, key in (("Isolated Margin", "isolated_margin"), ("USDⓈ-M Futures", "um_futures")):
        account = await get_account(session, await account_for(connection, key))
        assert account.name == f"Binance {name}"


async def test_unknown_wallet_gets_its_own_account(run, session, account_for) -> None:
    connection = await _connection(run)
    odd = await account_for(connection, wallet_key("Gold Vault"))
    account = await get_account(session, odd)
    assert (account.name, account.ledger_type) == ("Binance Gold Vault", "asset")
    assert odd != await account_for(connection, "spot")


async def test_account_of_another_connection_is_another_account(run, session, account_for) -> None:
    first, second = await _connection(run), await _connection(run)
    assert await account_for(first, "spot") != await account_for(second, "spot")


async def test_a_mapped_account_that_is_gone_is_made_again(run, session, account_for) -> None:
    connection = await _connection(run)
    spot = await account_for(connection, "spot")
    await session.execute(
        text("update core.connections set config = cast(:c as jsonb) where id = :id"),
        {"c": f'{{"account_map": {{"spot": "{uuid.uuid4()}"}}}}', "id": connection.id},
    )
    assert await account_for(connection, "spot") != spot


async def test_the_institution_is_shared(run, session, account_for) -> None:
    connection = await _connection(run)
    spot = await get_account(session, await account_for(connection, "spot"))
    funding = await get_account(session, await account_for(connection, "funding"))
    assert spot.institution_entity_id == funding.institution_entity_id


# Instruments


async def _asset(run, connection_id, code: str, flags: list[str] | None = None):
    sink = flags if flags is not None else []
    return await run(instruments.asset, connection_id, code, flag=sink.append)


async def _coin_config(run, connection_id, **overrides) -> None:
    row = build_row("coin-config", **overrides)
    parsed = parse("coin-config", build_response("coin-config", [row]))
    key = {"connection_id": connection_id, "snapshot_at": MOMENT}
    for record in records("coin-config", parsed):
        await run(store, "coin-config", key, record, {"read_to": MOMENT})


async def test_asset_creates_a_crypto_instrument(run, session) -> None:
    connection = await _connection(run)
    found = await _asset(run, connection.id, "AAA")
    assert found is not None
    instrument = await get_instrument(session, found)
    assert (instrument.kind, instrument.symbol, instrument.name, instrument.decimals) == (
        "crypto",
        "AAA",
        "AAA",
        8,
    )
    assert instrument.quote_instrument_id is None
    assert instrument.identifiers == (InstrumentIdentifier("binance_asset", "AAA"),)
    assert await _asset(run, connection.id, "AAA") == found


async def test_asset_takes_its_name_from_coin_config(run, session) -> None:
    connection = await _connection(run)
    await _coin_config(run, connection.id, coin="AAA", name="Alpha Coin", isLegalMoney=False)
    found = await _asset(run, connection.id, "AAA")
    assert (await get_instrument(session, found)).name == "Alpha Coin"


async def test_asset_reuses_an_existing_crypto_instrument(run, session) -> None:
    connection = await _connection(run)
    manual = await run(
        create_instrument,
        kind="crypto",
        symbol="aaa",
        name="Alpha",
        decimals=18,
        quote_instrument_id=None,
        identifiers=[],
    )
    found = await _asset(run, connection.id, "AAA")
    assert found == manual.id
    assert (await get_instrument(session, found)).identifiers == (
        InstrumentIdentifier("binance_asset", "AAA"),
    )
    assert len(await list_instruments(session, include_hidden=True)) == (
        1 + len(await _currencies(session))
    )


async def _currencies(session):
    return [i for i in await list_instruments(session, include_hidden=True) if i.kind == "fiat"]


async def test_asset_does_not_guess_between_two(run, session) -> None:
    connection = await _connection(run)
    existing = []
    for name in ("Alpha one", "Alpha two"):
        existing.append(
            await run(
                create_instrument,
                kind="crypto",
                symbol="AAA",
                name=name,
                decimals=8,
                quote_instrument_id=None,
                identifiers=[],
            )
        )
    flags: list[str] = []
    found = await _asset(run, connection.id, "AAA", flags)
    assert found not in {e.id for e in existing}
    assert flags == ["several instruments named AAA"]
    instrument = await get_instrument(session, found)
    assert instrument.identifiers == (InstrumentIdentifier("binance_asset", "AAA"),)
    # Found by its identifier from now on: no new instrument, no second flag.
    assert await _asset(run, connection.id, "AAA", flags) == found
    assert len(flags) == 1


async def test_asset_ignores_instruments_of_another_kind(run, session) -> None:
    connection = await _connection(run)
    equity = await run(
        create_instrument,
        kind="equity",
        symbol="AAA",
        name="A share",
        decimals=4,
        quote_instrument_id=(await get_currency(session, "GBP")).id,
        identifiers=[],
    )
    found = await _asset(run, connection.id, "AAA")
    assert found != equity.id


async def test_fiat_asset_is_the_currency(run, session) -> None:
    connection = await _connection(run)
    await _coin_config(run, connection.id, coin="EUR", name="Euro", isLegalMoney=True)
    before = len(await list_instruments(session, include_hidden=True))
    assert await _asset(run, connection.id, "EUR") == (await get_currency(session, "EUR")).id
    assert len(await list_instruments(session, include_hidden=True)) == before
    assert "EUR" in await instruments.assets_seen(session, connection.id)


async def test_legal_money_that_is_no_currency_is_refused_by_name(run) -> None:
    connection = await _connection(run)
    await _coin_config(run, connection.id, coin="ZZZ", isLegalMoney=True)
    with pytest.raises(UnknownCurrencyError):
        await _asset(run, connection.id, "ZZZ")


async def test_ld_mirror_is_not_an_instrument(run, session) -> None:
    connection = await _connection(run)
    assert await _asset(run, connection.id, "AAA") is not None
    before = len(await list_instruments(session, include_hidden=True))
    assert await _asset(run, connection.id, "LDAAA") is None
    assert len(await list_instruments(session, include_hidden=True)) == before
    # A real asset whose code begins LD is one: there is no asset `O`.
    found = await _asset(run, connection.id, "LDO")
    assert found is not None
    assert (await get_instrument(session, found)).symbol == "LDO"
    # The mirror of an asset only coin-config lists is a mirror too.
    await _coin_config(run, connection.id, coin="BBB", isLegalMoney=False)
    assert await _asset(run, connection.id, "LDBBB") is None


async def test_assets_seen_records_each_asset_once(run, session) -> None:
    connection = await _connection(run)
    other = await _connection(run)
    for code in ("AAA", "BBB", "AAA", "LDAAA"):
        await _asset(run, connection.id, code)
    await _asset(run, other.id, "CCC")
    assert await instruments.assets_seen(session, connection.id) == {"AAA", "BBB"}
    assert await instruments.assets_seen(session, other.id) == {"CCC"}
    first = await session.scalar(
        text("select first_seen_at from finance.binance_assets_seen where asset = 'AAA'")
    )
    assert first is not None
    assert AssetsSeen.__tablename__ == "binance_assets_seen"


# What the database checks


async def test_binance_asset_scheme_checked_by_database(run, session) -> None:
    coin = await run(
        create_instrument,
        kind="crypto",
        symbol="AAA",
        name="Alpha",
        decimals=8,
        quote_instrument_id=None,
        identifiers=[],
    )
    insert = (
        "insert into finance.instrument_identifiers (instrument_id, scheme, value)"
        " values (:i, :s, :v)"
    )
    await session.execute(text(insert), {"i": coin.id, "s": "binance_asset", "v": "AAA"})
    found = await find_by_identifier(session, "binance_asset", "aaa")
    assert found is not None and found.id == coin.id
    with pytest.raises(IntegrityError):
        async with session.begin_nested():
            await session.execute(text(insert), {"i": coin.id, "s": "binance_assets", "v": "X"})


async def test_binance_asset_codes_are_upper_case(run, session) -> None:
    connection = await _connection(run)
    found = await _asset(run, connection.id, "aaa")
    assert (await get_instrument(session, found)).identifiers == (
        InstrumentIdentifier("binance_asset", "AAA"),
    )


async def test_chain_address_identifier_kind_is_checked_by_database(session) -> None:
    insert = (
        "insert into finance.account_identifiers (kind, value, created_by) values (:k, :v, 'user')"
    )
    await session.execute(text(insert), {"k": "chain_address", "v": "0x" + "1" * 40})
    await session.execute(text(insert), {"k": "chain_address", "v": "bc1" + "q" * 39})
    for bad in ("0x1234", "x" * 129, "has space " + "1" * 30, "0x" + "1" * 39 + "!"):
        with pytest.raises(IntegrityError):
            async with session.begin_nested():
                await session.execute(text(insert), {"k": "chain_address", "v": bad})
    with pytest.raises(IntegrityError):
        async with session.begin_nested():
            await session.execute(text(insert), {"k": "evm_address", "v": "0x" + "1" * 40})
