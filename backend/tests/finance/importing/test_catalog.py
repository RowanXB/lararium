from collections import Counter

from pydantic import TypeAdapter

from lararium.modules.finance.importing.catalog import CATALOG
from lararium.modules.finance.importing.profiles import (
    CsvProfileConfig,
    ProfileConfig,
    ProfileConfigModel,
    is_bank_transfer,
)

_CONFIG: TypeAdapter[ProfileConfigModel] = TypeAdapter(ProfileConfig)


def test_every_config_validates() -> None:
    for entry in CATALOG:
        config = _CONFIG.validate_python(entry.config.model_dump(mode="json"))
        assert config == entry.config
        assert config.format == entry.format


def test_one_recommended_format_per_institution() -> None:
    recommended = Counter(entry.institution for entry in CATALOG if entry.recommended)
    institutions = {entry.institution for entry in CATALOG}
    assert institutions == {"HSBC UK", "Lloyds", "BOC UK", "Tembo", "LemFi"}
    assert recommended == Counter(dict.fromkeys(institutions, 1))


def test_decision_14_formats() -> None:
    by_institution = {
        institution: sorted(e.format for e in CATALOG if e.institution == institution)
        for institution in {e.institution for e in CATALOG}
    }
    assert by_institution == {
        "HSBC UK": ["csv", "csv", "ofx", "pdf", "qif"],
        "Lloyds": ["csv", "pdf", "qif"],
        "BOC UK": ["pdf"],
        "Tembo": ["pdf"],
        "LemFi": ["csv", "pdf"],
    }
    recommended = {(e.institution, e.format) for e in CATALOG if e.recommended}
    assert recommended == {
        ("HSBC UK", "pdf"),
        ("Lloyds", "csv"),
        ("BOC UK", "pdf"),
        ("Tembo", "pdf"),
        ("LemFi", "pdf"),
    }


def test_labels_are_unique() -> None:
    labels = [(e.institution, e.label) for e in CATALOG]
    assert len(labels) == len(set(labels))


def _csv(institution: str, label: str) -> CsvProfileConfig:
    [entry] = [e for e in CATALOG if (e.institution, e.label) == (institution, label)]
    assert isinstance(entry.config, CsvProfileConfig)
    return entry.config


def test_hsbc_transaction_csv() -> None:
    config = _csv("HSBC UK", "CSV (transaction list)")
    assert config.header == ["Date", "Description", "Amount"]
    assert config.date_format == "%d/%m/%Y"
    assert (config.columns.date, config.columns.description, config.columns.amount) == (
        "Date",
        ["Description"],
        "Amount",
    )


def test_midata_csv() -> None:
    config = _csv("HSBC UK", "midata CSV")
    assert config.date_format == "%Y-%m-%d"
    columns = config.columns
    assert (columns.amount, columns.balance, columns.type) == ("Debit/Credit", "Balance", "Type")
    assert config.row_order == "newest_first"
    assert config.skip_footer_rows == 1
    assert config.overdraft_footer_label == "Arranged overdraft limit"


def test_lloyds_csv() -> None:
    config = _csv("Lloyds", "CSV")
    columns = config.columns
    assert (columns.inflow, columns.outflow) == ("Credit Amount", "Debit Amount")
    assert columns.type == "Transaction Type"
    assert (columns.account_sort_code, columns.account_number) == ("Sort Code", "Account Number")
    assert config.row_order == "newest_first"
    assert config.amount_format.thousands_separator == ","


def test_tembo_interest_is_no_bank_transfer() -> None:
    """Review: Tembo pays interest itself; only deposits and withdrawals move money between
    bank accounts."""
    [entry] = [e for e in CATALOG if e.institution == "Tembo"]
    for kind in ("Interest", "Interest Adjustment"):
        assert not is_bank_transfer(entry.config.card_types, kind)
    for kind in ("Deposit", "Withdrawal", "Transfer In", "Transfer Out"):
        assert is_bank_transfer(entry.config.card_types, kind)
