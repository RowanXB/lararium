from typing import Any

import pytest
from pydantic import TypeAdapter, ValidationError

from lararium.modules.finance.errors import DuplicateProfileNameError
from lararium.modules.finance.importing.profiles import (
    AmountFormat,
    CsvColumns,
    CsvProfileConfig,
    ProfileConfig,
    create_profile,
    get_profile,
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
    for date_format in ["%d/%m", "not a date"]:
        with pytest.raises(ValidationError):
            PROFILE_CONFIG.validate_python(_csv(date_format=date_format))


def test_timezone_and_filters_validated() -> None:
    config = PROFILE_CONFIG.validate_python(
        _csv(timezone="Europe/London", include_only={"State": ["COMPLETED"]})
    )
    assert config.timezone == "Europe/London"
    with pytest.raises(ValidationError):
        PROFILE_CONFIG.validate_python(_csv(timezone="Europe/Nowhere"))
    with pytest.raises(ValidationError):
        PROFILE_CONFIG.validate_python(_csv(include_only={"State": []}))


def test_header_names_need_no_header_row() -> None:
    config = PROFILE_CONFIG.validate_python(_csv(header=["Date", "Description", "Amount"]))
    assert config.header == ["Date", "Description", "Amount"]
    with pytest.raises(ValidationError):
        PROFILE_CONFIG.validate_python(_csv(header=["Date", "Amount"]))


def test_qif_and_pdf_configs() -> None:
    qif = PROFILE_CONFIG.validate_python({"format": "qif", "date_format": "%d/%m/%Y"})
    assert qif.encoding == "utf-8-sig"
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
