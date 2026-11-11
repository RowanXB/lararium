import pytest

from financial_gate.core.settings_store import get_setting
from financial_gate.modules.finance.errors import DuplicateIdentifierError
from financial_gate.modules.finance.instruments import (
    InstrumentIdentifier,
    create_instrument,
    find_by_identifier,
    get_currency,
    list_instruments,
    set_hidden,
)


async def test_seeded_currencies(session) -> None:
    gbp = await get_currency(session, "gbp")
    jpy = await get_currency(session, "JPY")
    assert (gbp.kind, gbp.symbol, gbp.decimals) == ("fiat", "GBP", 2)
    assert jpy.decimals == 0
    assert gbp.identifiers == (InstrumentIdentifier("iso4217", "GBP"),)


async def test_create_crypto_with_normalized_identifier(session, run, usd) -> None:
    btc = await run(
        create_instrument,
        kind="crypto",
        symbol="BTC",
        name="Bitcoin",
        decimals=8,
        quote_instrument_id=usd.id,
        identifiers=[InstrumentIdentifier("coingecko", " Bitcoin ")],
    )
    found = await find_by_identifier(session, "coingecko", "BITCOIN")
    assert found is not None and found.id == btc.id
    assert found.identifiers == (InstrumentIdentifier("coingecko", "bitcoin"),)


async def test_duplicate_identifier_rejected(run) -> None:
    with pytest.raises(DuplicateIdentifierError):
        await run(
            create_instrument,
            kind="fiat",
            symbol="GBX",
            name="dup",
            decimals=2,
            quote_instrument_id=None,
            identifiers=[InstrumentIdentifier("iso4217", "gbp")],
        )


async def test_hidden_instrument_left_out_of_default_list(session, run, usd) -> None:
    spam = await run(
        create_instrument,
        kind="crypto",
        symbol="SPAM",
        name="Airdrop",
        decimals=18,
        quote_instrument_id=usd.id,
        identifiers=[],
    )
    await run(set_hidden, spam.id, True)
    assert spam.id not in [i.id for i in await list_instruments(session, include_hidden=False)]
    assert spam.id in [i.id for i in await list_instruments(session, include_hidden=True)]


async def test_reporting_currency_defaults_to_gbp(session) -> None:
    assert await get_setting(session, "finance.reporting_currency") == "GBP"
