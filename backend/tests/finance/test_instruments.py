import pytest

from lararium.core.settings_store import get_setting
from lararium.modules.finance.errors import DuplicateIdentifierError, FinanceValidationError
from lararium.modules.finance.instruments import (
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


@pytest.mark.parametrize(
    ("kind", "identifiers"),
    [
        # A currency is found by its ISO 4217 code: without one it could never be valued.
        ("fiat", []),
        ("fiat", [InstrumentIdentifier("isin", "XS0000000001")]),
        ("fiat", [InstrumentIdentifier("iso4217", "NGN"), InstrumentIdentifier("iso4217", "NGX")]),
        # Anything with an ISO 4217 code would be taken for a currency.
        ("crypto", [InstrumentIdentifier("iso4217", "XBT")]),
    ],
)
async def test_iso4217_code_if_and_only_if_fiat(session, run, kind, identifiers) -> None:
    with pytest.raises(FinanceValidationError, match="ISO 4217"):
        await run(
            create_instrument,
            kind=kind,
            symbol="X",
            name="X",
            decimals=2,
            quote_instrument_id=None,
            identifiers=identifiers,
        )
    assert await find_by_identifier(session, "iso4217", "NGN") is None


async def test_create_fiat_with_its_code(session, run) -> None:
    naira = await run(
        create_instrument,
        kind="fiat",
        symbol="NGN",
        name="Naira",
        decimals=2,
        quote_instrument_id=None,
        identifiers=[InstrumentIdentifier("iso4217", "ngn")],
    )
    assert (await get_currency(session, "NGN")).id == naira.id
