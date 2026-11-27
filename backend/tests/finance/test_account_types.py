from dataclasses import replace

import pytest

from lararium.modules.finance.account_types import (
    ACCOUNT_TYPES,
    AccountShape,
    Api,
    FileFormat,
    account_type,
    check_catalog,
    family_keys,
    keys_funded_by,
)
from lararium.modules.finance.importing.catalog import CATALOG


def test_keys_are_unique():
    keys = [t.key for t in ACCOUNT_TYPES]
    assert len(keys) == len(set(keys))
    check_catalog(ACCOUNT_TYPES)


def test_every_type_has_an_api_or_a_file():
    assert all(t.api or t.files for t in ACCOUNT_TYPES)
    check_catalog(ACCOUNT_TYPES)


def test_a_file_without_a_profile_is_on_a_type_with_an_api():
    for t in ACCOUNT_TYPES:
        if any(f.profile is None for f in t.files):
            assert t.api is not None
    check_catalog(ACCOUNT_TYPES)


def test_a_family_has_at_least_two_types():
    families = {t.family for t in ACCOUNT_TYPES if t.family}
    assert all(len(family_keys(f)) >= 2 for f in families)
    check_catalog(ACCOUNT_TYPES)


def test_the_invariants_refuse_a_bad_catalog():
    first = ACCOUNT_TYPES[0]
    statement = account_type("hsbc_uk")
    assert statement is not None
    with pytest.raises(RuntimeError, match="unique"):
        check_catalog((first, first))
    with pytest.raises(RuntimeError, match="neither"):
        check_catalog((replace(first, api=None, files=()),))
    with pytest.raises(RuntimeError, match="nor a shape"):
        check_catalog((replace(statement, shape=None),))
    with pytest.raises(RuntimeError, match="without a profile"):
        check_catalog((replace(statement, files=(FileFormat("x", "csv", None),)),))
    with pytest.raises(RuntimeError, match="family"):
        check_catalog((replace(first, family="solo"),))


def test_the_entries():
    assert [(t.key, t.name) for t in ACCOUNT_TYPES] == [
        ("monzo", "Monzo"),
        ("ibkr", "Interactive Brokers"),
        ("etoro", "eToro"),
        ("binance", "Binance"),
        ("trading212.invest", "Trading 212 Invest"),
        ("trading212.stocks_isa", "Trading 212 Stocks ISA"),
        ("trading212.cash_isa", "Trading 212 Cash ISA"),
        ("hsbc_uk", "HSBC UK"),
        ("lloyds", "Lloyds"),
        ("boc_uk", "BOC UK"),
        ("revolut.current", "Revolut Current"),
        ("revolut.savings", "Revolut Savings"),
        ("tembo", "Tembo"),
        ("lemfi", "LemFi"),
    ]


def test_statement_files_are_the_import_catalogs():
    by_name = {t.name: t for t in ACCOUNT_TYPES}
    for institution in {e.institution for e in CATALOG}:
        files = [f for f in by_name[institution].files if f.profile is not None]
        entries = [e for e in CATALOG if e.institution == institution]
        assert [(f.label, f.format, f.recommended) for f in files] == [
            (e.label, e.format, e.recommended) for e in entries
        ]
        assert all(f.profile is e.config for f, e in zip(files, entries, strict=True))
    assert sum(f.profile is not None for t in ACCOUNT_TYPES for f in t.files) == len(CATALOG)


def test_trading212():
    cash = account_type("trading212.cash_isa")
    assert cash is not None
    assert cash.api is None
    assert cash.shape == AccountShape("asset", "savings_pot", "isa", "transactions")
    assert cash.funding == frozenset({"bank_transfer"})
    assert family_keys("trading212") == {
        "trading212.invest",
        "trading212.stocks_isa",
        "trading212.cash_isa",
    }
    assert keys_funded_by("bank_transfer") == family_keys("trading212")
    invest = account_type("trading212.invest")
    isa = account_type("trading212.stocks_isa")
    assert invest is not None and isa is not None
    assert invest.api == Api("trading212", {"account_kind": "invest"})
    assert isa.api == Api("trading212", {"account_kind": "isa"})


def test_an_unknown_key_is_no_type():
    assert account_type("gone") is None and account_type(None) is None
