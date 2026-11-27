import uuid
from typing import Any

import pytest
from pydantic import TypeAdapter, ValidationError

from lararium.modules.finance.errors import (
    DuplicateProfileNameError,
    FinanceValidationError,
    ImportProfileNotFoundError,
)
from lararium.modules.finance.importing.profiles import (
    AmountFormat,
    CsvColumns,
    CsvProfileConfig,
    OfxProfileConfig,
    ProfileConfig,
    create_profile,
    get_profile,
    is_bank_transfer,
    is_card_payment,
    list_profiles,
    update_profile,
)

PROFILE_CONFIG: TypeAdapter[Any] = TypeAdapter(ProfileConfig)


def _csv(**overrides: Any) -> dict[str, Any]:
    config: dict[str, Any] = {
        "format": "csv",
        "columns": {"date": "Date", "description": ["Description"], "amount": "Amount"},
        "date_format": "%d/%m/%Y",
    }
    return config | overrides


def test_amount_xor_inflow_outflow() -> None:
    with pytest.raises(ValidationError):
        CsvColumns(date="D", description=["X"])
    with pytest.raises(ValidationError):
        CsvColumns(date="D", description=["X"], amount="A", inflow="In", outflow="Out")
    with pytest.raises(ValidationError):
        CsvColumns(date="D", description=["X"], amount="A", inflow="In")
    with pytest.raises(ValidationError):
        CsvColumns(date="D", description=["X"], inflow="In")
    assert CsvColumns(date="D", description=["X"], amount="A").amount == "A"
    both = CsvColumns(date="D", description=["X"], inflow="In", outflow="Out")
    assert (both.inflow, both.outflow) == ("In", "Out")


def test_separators_must_differ() -> None:
    with pytest.raises(ValidationError):
        AmountFormat(decimal_separator=",", thousands_separator=",")
    assert AmountFormat(decimal_separator=",", thousands_separator=".").decimal_separator == ","


def test_unknown_key_rejected() -> None:
    PROFILE_CONFIG.validate_python(_csv())
    with pytest.raises(ValidationError):
        PROFILE_CONFIG.validate_python(_csv(colour="red"))


def test_date_format_must_round_trip() -> None:
    for date_format, has_time in [
        ("%d/%m/%Y", False),
        ("%b %d, %Y", False),
        ("%d %b, %Y %H:%M", True),
        ("%Y-%m-%d %H:%M:%S", True),
    ]:
        config = PROFILE_CONFIG.validate_python(_csv(date_format=date_format))
        assert isinstance(config, CsvProfileConfig)
        assert config.has_time is has_time
    # "%d/%d/%m/%Y" repeats a directive, which makes strptime's regex invalid.
    for date_format in ["%d/%m", "not a date", "%d/%d/%m/%Y"]:
        with pytest.raises(ValidationError):
            PROFILE_CONFIG.validate_python(_csv(date_format=date_format))


def test_timezone_and_filters_validated() -> None:
    config = PROFILE_CONFIG.validate_python(
        _csv(timezone="Europe/London", include_only={"State": ["COMPLETED"]})
    )
    assert config.timezone == "Europe/London"
    # "Europe" is a directory of the time zone database, not a zone.
    for timezone in ["Europe/Nowhere", "Europe"]:
        with pytest.raises(ValidationError):
            PROFILE_CONFIG.validate_python(_csv(timezone=timezone))
    with pytest.raises(ValidationError):
        PROFILE_CONFIG.validate_python(_csv(include_only={"State": []}))


def test_a_period_file_name_pattern_needs_both_groups_and_must_compile() -> None:
    pattern = r"from_(?P<start>\d{4}-\d{2}-\d{2})_to_(?P<end>\d{4}-\d{2}-\d{2})"
    config = PROFILE_CONFIG.validate_python(_csv(period_from_filename=pattern))
    assert config.period_from_filename == pattern
    assert PROFILE_CONFIG.validate_python(_csv()).period_from_filename is None
    for refused in [r"from_(?P<start>.+)", r"to_(?P<end>.+)", r"(.+)_(.+)", r"(?P<start>[", ""]:
        with pytest.raises(ValidationError):
            PROFILE_CONFIG.validate_python(_csv(period_from_filename=refused))


def test_header_names_need_no_header_row() -> None:
    config = PROFILE_CONFIG.validate_python(_csv(header=["Date", "Description", "Amount"]))
    assert config.header == ["Date", "Description", "Amount"]
    with pytest.raises(ValidationError):
        PROFILE_CONFIG.validate_python(_csv(header=["Date", "Amount"]))


def test_qif_and_pdf_configs() -> None:
    qif = PROFILE_CONFIG.validate_python({"format": "qif", "date_format": "%d/%m/%Y"})
    assert qif.encoding == "utf-8-sig"
    assert qif.amount_format == AmountFormat(thousands_separator=",")
    euro = PROFILE_CONFIG.validate_python(
        {
            "format": "qif",
            "date_format": "%d.%m.%Y",
            "amount_format": {"decimal_separator": ",", "thousands_separator": "."},
        }
    )
    assert euro.amount_format == AmountFormat(decimal_separator=",", thousands_separator=".")
    with pytest.raises(ValidationError):
        PROFILE_CONFIG.validate_python(
            {
                "format": "qif",
                "date_format": "%d/%m/%Y",
                "amount_format": {"decimal_separator": ",", "thousands_separator": ","},
            }
        )
    pdf = PROFILE_CONFIG.validate_python({"format": "pdf", "layout": "lloyds_statement"})
    assert pdf.layout == "lloyds_statement"
    with pytest.raises(ValidationError):
        PROFILE_CONFIG.validate_python({"format": "qif"})
    with pytest.raises(ValidationError):
        PROFILE_CONFIG.validate_python({"format": "pdf", "layout": "example_bank_statement"})


async def test_update_bumps_version(session, run) -> None:
    config = PROFILE_CONFIG.validate_python(_csv())
    created = await run(create_profile, name=" Example Bank CSV ", config=config)
    assert (created.name, created.version, created.config) == ("Example Bank CSV", 1, config)

    new_config = PROFILE_CONFIG.validate_python(_csv(row_order="newest_first"))
    updated = await run(update_profile, created.id, name="Example Bank CSV", config=new_config)
    assert (updated.version, updated.config) == (2, new_config)
    assert await get_profile(session, created.id) == updated
    assert await list_profiles(session) == [updated]


async def test_duplicate_name_case_insensitive(run) -> None:
    config = PROFILE_CONFIG.validate_python({"format": "ofx"})
    await run(create_profile, name="Example Bank", config=config)
    with pytest.raises(DuplicateProfileNameError):
        await run(create_profile, name=" example bank ", config=config)


def test_delimiter_rejects_quote_and_line_breaks() -> None:
    assert PROFILE_CONFIG.validate_python(_csv(delimiter=";")).delimiter == ";"
    for delimiter in ['"', "\n", "\r"]:
        with pytest.raises(ValidationError):
            PROFILE_CONFIG.validate_python(_csv(delimiter=delimiter))


async def test_duplicate_name_on_update(run) -> None:
    config = PROFILE_CONFIG.validate_python({"format": "ofx"})
    await run(create_profile, name="Example Bank", config=config)
    other = await run(create_profile, name="Example Bank OFX", config=config)
    with pytest.raises(DuplicateProfileNameError):
        await run(update_profile, other.id, name="EXAMPLE BANK", config=config)


async def test_missing_profile_and_empty_name(session, run) -> None:
    config = PROFILE_CONFIG.validate_python({"format": "ofx"})
    with pytest.raises(ImportProfileNotFoundError):
        await get_profile(session, uuid.uuid4())
    with pytest.raises(ImportProfileNotFoundError):
        await run(update_profile, uuid.uuid4(), name="Example Bank", config=config)
    with pytest.raises(FinanceValidationError):
        await run(create_profile, name="   ", config=config)


async def test_list_orders_names_case_insensitively(session, run) -> None:
    config = PROFILE_CONFIG.validate_python({"format": "ofx"})
    for name in ["beta", "Alpha", "Gamma"]:
        await run(create_profile, name=name, config=config)
    assert [p.name for p in await list_profiles(session)] == ["Alpha", "beta", "Gamma"]


def test_typed_field_columns_checked_against_header() -> None:
    typed = {
        "value_date": "Value Date",
        "payee": "Payee",
        "memo": "Memo",
        "check_number": "Cheque",
        "counterparty_name": "Name",
        "counterparty_sort_code": "Their Sort Code",
        "counterparty_account_number": "Their Account",
        "reference": "Reference",
        "account_sort_code": "Sort Code",
        "account_number": "Account Number",
    }
    base = {"date": "Date", "description": ["Description"], "amount": "Amount"}
    columns = CsvColumns.model_validate(base | typed)
    assert set(typed.values()) <= set(columns.names())
    header = ["Date", "Description", "Amount", *typed.values()]
    config = PROFILE_CONFIG.validate_python(_csv(columns=base | typed, header=header))
    assert config.columns.reference == "Reference"
    for name in typed.values():
        with pytest.raises(ValidationError, match=name):
            PROFILE_CONFIG.validate_python(
                _csv(columns=base | typed, header=[h for h in header if h != name])
            )


def test_overdraft_label_validated() -> None:
    label = "Arranged overdraft limit"
    config = PROFILE_CONFIG.validate_python(_csv(overdraft_footer_label=label, skip_footer_rows=1))
    assert config.overdraft_footer_label == label
    assert PROFILE_CONFIG.validate_python(_csv()).overdraft_footer_label is None
    for empty in ["", "  "]:
        with pytest.raises(ValidationError):
            PROFILE_CONFIG.validate_python(_csv(overdraft_footer_label=empty, skip_footer_rows=1))
    # The label is looked for only among the footer rows, so it needs some.
    with pytest.raises(ValidationError, match="skip_footer_rows"):
        PROFILE_CONFIG.validate_python(_csv(overdraft_footer_label=label))


def test_started_at_needs_a_time() -> None:
    columns = {
        "date": "Completed",
        "description": ["Description"],
        "amount": "Amount",
        "started_at": "Started",
    }
    config = PROFILE_CONFIG.validate_python(_csv(columns=columns, date_format="%Y-%m-%d %H:%M:%S"))
    assert config.columns.started_at == "Started"
    with pytest.raises(ValidationError, match="started_at is a time"):
        PROFILE_CONFIG.validate_python(_csv(columns=columns, date_format="%Y-%m-%d"))


def test_a_card_number_makes_no_bank_transfer() -> None:
    assert is_bank_transfer(["VIS"], "CR", None)
    assert not is_bank_transfer(["VIS"], "VIS", None)
    assert not is_bank_transfer(["VIS"], "CR", "123456******1234")
    assert not is_bank_transfer([], None, "123456******1234")


def test_extract_patterns_validated() -> None:
    details = {"column": "Details", "patterns": [r"(?P<counterparty_bank>[^,]+), (?P<memo>.+)"]}
    config = PROFILE_CONFIG.validate_python(_csv(extract=[details]))
    assert config.extract[0].fields() == {"counterparty_bank", "memo"}
    assert "Details" in config.read_columns()
    for patterns, message in [
        ([], "at least 1"),
        (["(unclosed"], "not a regular expression"),
        ([r"(?P<colour>.+)"], "can't fill: colour"),
    ]:
        with pytest.raises(ValidationError, match=message):
            PROFILE_CONFIG.validate_python(_csv(extract=[{"column": "D", "patterns": patterns}]))


def test_extract_fills_a_field_once() -> None:
    memo = {"column": "Details", "patterns": [r"(?P<memo>.+)"]}
    with pytest.raises(ValidationError, match="fields filled twice: memo"):
        PROFILE_CONFIG.validate_python(_csv(extract=[memo, memo]))
    columns = {"date": "Date", "description": ["Description"], "amount": "Amount", "memo": "Note"}
    with pytest.raises(ValidationError, match="fields filled twice: memo"):
        PROFILE_CONFIG.validate_python(_csv(columns=columns, extract=[memo]))


def test_extract_column_checked_against_header() -> None:
    extract = [{"column": "Details", "patterns": [r"(?P<memo>.+)"]}]
    header = ["Date", "Description", "Amount"]
    with pytest.raises(ValidationError, match="columns not in the header: Details"):
        PROFILE_CONFIG.validate_python(_csv(extract=extract, header=header))
    PROFILE_CONFIG.validate_python(_csv(extract=extract, header=[*header, "Details"]))


def test_currency_set_once_or_by_column() -> None:
    assert PROFILE_CONFIG.validate_python(_csv(currency="GBP")).currency == "GBP"
    with pytest.raises(ValidationError):
        PROFILE_CONFIG.validate_python(_csv(currency="gbp"))
    columns = {"date": "Date", "description": ["D"], "amount": "A", "currency": "Currency"}
    with pytest.raises(ValidationError, match="not both"):
        PROFILE_CONFIG.validate_python(_csv(columns=columns, currency="GBP"))


def test_a_card_number_or_a_card_payment_type_is_a_card_payment() -> None:
    assert is_card_payment(["VIS"], "VIS", None)
    assert is_card_payment(["VIS"], " VIS ", None)
    assert is_card_payment([], None, "123456******1234")
    assert not is_card_payment(["VIS"], "CHQ", None)
    assert not is_card_payment([], None, None)
    assert OfxProfileConfig(format="ofx").card_payment_types == ["POS"]
