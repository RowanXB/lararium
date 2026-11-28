"""The account types the owner can pick: where an account's data comes from and how it posts.

A type with an API syncs through a connector; a type without one is filled from files, whose
profiles come from the import catalog. The invariants are checked when the module loads.
"""

from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import Literal

from lararium.core.json_types import JSONValue
from lararium.modules.finance.accounts import AccountSubtype, BalanceMode, LedgerType, TaxWrapper
from lararium.modules.finance.importing.catalog import CATALOG
from lararium.modules.finance.importing.profiles import ProfileConfigModel

Method = Literal["card", "bank_transfer"]


@dataclass(frozen=True)
class AccountShape:
    ledger_type: LedgerType | None
    subtype: AccountSubtype
    tax_wrapper: TaxWrapper
    balance_mode: BalanceMode


@dataclass(frozen=True)
class Api:
    connector: str
    config: Mapping[str, JSONValue]


@dataclass(frozen=True)
class FileFormat:
    label: str
    format: str
    profile: ProfileConfigModel | None
    recommended: bool = False


@dataclass(frozen=True)
class AccountType:
    key: str
    institution: str
    label: str  # "" when it is its institution's only type
    shape: AccountShape | None
    api: Api | None
    files: tuple[FileFormat, ...]
    funding: frozenset[Method] = frozenset()
    family: str | None = None

    @property
    def name(self) -> str:
        return f"{self.institution} {self.label}".strip()


def _catalog_files(name: str) -> tuple[FileFormat, ...]:
    return tuple(
        FileFormat(e.label, e.format, e.config, e.recommended)
        for e in CATALOG
        if e.institution == name
    )


def _statement_type(key: str, institution: str, label: str = "") -> AccountType:
    bare = AccountType(key, institution, label, None, None, ())
    return replace(
        bare,
        shape=AccountShape(None, "bank", "none", "transactions"),
        files=_catalog_files(bare.name),
    )


_TRADING212_FUNDING: frozenset[Method] = frozenset({"card", "bank_transfer"})
_HISTORY_CSV = FileFormat("History CSV", "csv", None)

ACCOUNT_TYPES: tuple[AccountType, ...] = (
    AccountType("monzo", "Monzo", "", None, Api("monzo", {}), ()),
    AccountType(
        "ibkr",
        "Interactive Brokers",
        "",
        None,
        Api("ibkr", {}),
        (FileFormat("Flex statement", "xml", None),),
    ),
    AccountType(
        "etoro",
        "eToro",
        "",
        None,
        Api("etoro", {}),
        (FileFormat("Account statement", "xlsx", None),),
    ),
    AccountType("binance", "Binance", "", None, Api("binance", {}), ()),
    AccountType(
        "trading212.invest",
        "Trading 212",
        "Invest",
        None,
        Api("trading212", {"account_kind": "invest"}),
        (_HISTORY_CSV,),
        _TRADING212_FUNDING,
        "trading212",
    ),
    AccountType(
        "trading212.stocks_isa",
        "Trading 212",
        "Stocks ISA",
        None,
        Api("trading212", {"account_kind": "isa"}),
        (_HISTORY_CSV,),
        _TRADING212_FUNDING,
        "trading212",
    ),
    AccountType(
        "trading212.cash_isa",
        "Trading 212",
        "Cash ISA",
        AccountShape("asset", "savings_pot", "isa", "transactions"),
        None,
        _catalog_files("Trading 212 Cash ISA"),
        frozenset({"bank_transfer"}),
        "trading212",
    ),
    _statement_type("hsbc_uk", "HSBC UK"),
    _statement_type("lloyds", "Lloyds"),
    _statement_type("boc_uk", "BOC UK"),
    _statement_type("revolut.current", "Revolut", "Current"),
    _statement_type("revolut.savings", "Revolut", "Savings"),
    _statement_type("tembo", "Tembo"),
    _statement_type("lemfi", "LemFi"),
)


def check_catalog(types: tuple[AccountType, ...]) -> None:
    keys = [t.key for t in types]
    if len(keys) != len(set(keys)):
        raise RuntimeError("account type keys are not unique")
    for t in types:
        if t.api is None and not t.files:
            raise RuntimeError(f"account type {t.key} has neither an API nor a file")
        if t.api is None and t.shape is None:
            raise RuntimeError(f"account type {t.key} has neither an API nor a shape")
        if t.api is None and any(f.profile is None for f in t.files):
            raise RuntimeError(f"account type {t.key} has a file without a profile or an API")
    families = [t.family for t in types if t.family]
    if any(families.count(f) < 2 for f in families):
        raise RuntimeError("a family has fewer than two account types")


check_catalog(ACCOUNT_TYPES)

_BY_KEY = {t.key: t for t in ACCOUNT_TYPES}


def account_type(key: str | None) -> AccountType | None:
    return _BY_KEY.get(key) if key is not None else None


def families() -> frozenset[str]:
    """Every family some account type belongs to."""
    return frozenset(t.family for t in ACCOUNT_TYPES if t.family)


def family_of(type_key: str | None) -> str | None:
    found = account_type(type_key)
    return None if found is None else found.family


def family_keys(family: str) -> frozenset[str]:
    return frozenset(t.key for t in ACCOUNT_TYPES if t.family == family)


def keys_funded_by(method: Method) -> frozenset[str]:
    return frozenset(t.key for t in ACCOUNT_TYPES if method in t.funding)
