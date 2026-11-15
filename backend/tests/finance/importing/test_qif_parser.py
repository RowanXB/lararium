import time
from datetime import date
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from lararium.modules.finance.errors import FileFormatError
from lararium.modules.finance.importing import parsed
from lararium.modules.finance.importing.parsed import ParsedRow, ParsedStatement, RowError
from lararium.modules.finance.importing.profiles import QifProfileConfig
from lararium.modules.finance.importing.qif_parser import PARSER_VERSION, parse_qif

LONDON = ZoneInfo("Europe/London")
BANK = b"""!Type:Bank
D10/11/2026
T-3.50
PExample Cafe
MCard 1234
N000101
^
D11/11/2026
T2,000.00
PExample Employer
^
"""


def _config(**overrides: Any) -> QifProfileConfig:
    config: dict[str, Any] = {"format": "qif", "date_format": "%d/%m/%Y"}
    return QifProfileConfig.model_validate(config | overrides)


def _parse(content: bytes, **overrides: Any) -> ParsedStatement:
    return parse_qif(content, _config(**overrides), LONDON)


def _rows(statement: ParsedStatement) -> list[ParsedRow]:
    rows = [row for row in statement.rows if isinstance(row, ParsedRow)]
    assert len(rows) == len(statement.rows), statement.rows
    return rows


def test_parser_version() -> None:
    assert PARSER_VERSION == "qif-1"


def test_bank_records() -> None:
    statement = _parse(BANK)
    first, second = _rows(statement)
    assert [first.position, second.position] == [0, 1]
    assert [first.booking_date, second.booking_date] == [date(2026, 11, 10), date(2026, 11, 11)]
    assert [first.quantity, second.quantity] == [Decimal("-3.50"), Decimal("2000.00")]
    assert first.description == "Example Cafe Card 1234"
    assert (first.payee, first.memo, first.check_number) == ("Example Cafe", "Card 1234", "000101")
    assert second.description == "Example Employer"
    assert (second.payee, second.memo, second.check_number) == ("Example Employer", None, None)
    assert first.raw == {
        "D": "10/11/2026",
        "T": "-3.50",
        "P": "Example Cafe",
        "M": "Card 1234",
        "N": "000101",
    }
    for row in (first, second):
        assert row.occurred_at is None and row.value_date is None
        assert row.fee == 0 and row.balance is None and row.currency is None
        assert row.external_id is None and row.bank_type is None
    assert statement.account is None and statement.closing_balance is None


def test_outflow_positive_negates() -> None:
    rows = _rows(_parse(BANK, sign="outflow_positive"))
    assert [row.quantity for row in rows] == [Decimal("3.50"), Decimal("-2000.00")]


@pytest.mark.parametrize("header", [b"!Type: Bank", b"!type:bank", b"!TYPE:CCard", b"!Type:Cash"])
def test_header_variants(header: bytes) -> None:
    content = BANK.replace(b"!Type:Bank", header)
    assert len(_rows(_parse(content))) == 2


def test_leading_blank_lines_before_header() -> None:
    assert len(_rows(_parse(b"\n  \n" + BANK))) == 2


def test_crlf() -> None:
    rows = _rows(_parse(BANK.replace(b"\n", b"\r\n")))
    assert [row.payee for row in rows] == ["Example Cafe", "Example Employer"]
    assert rows[0].raw["N"] == "000101"


def test_u_amount_when_no_t() -> None:
    content = b"!Type:Bank\nD10/11/2026\nU-7.25\nPShop\n^\n"
    assert _rows(_parse(content))[0].quantity == Decimal("-7.25")


def test_other_tags_kept_in_raw() -> None:
    content = b"!Type:Bank\nD10/11/2026\nT-7.25\nCX\nLGroceries\nAExample Road\nATown\n^\n"
    row = _rows(_parse(content))[0]
    assert row.raw == {
        "D": "10/11/2026",
        "T": "-7.25",
        "C": "X",
        "L": "Groceries",
        "A": "Example Road",
        "A#2": "Town",
    }


def test_missing_amount_is_row_error() -> None:
    content = b"""!Type:Bank
D10/11/2026
PNo amount
^
T-1.00
PNo date
^
D31/02/2026
T-1.00
^
D10/11/2026
T1.2.3
^
D12/11/2026
T5.00
^
"""
    statement = _parse(content)
    *errors, good = statement.rows
    assert all(isinstance(error, RowError) for error in errors)
    messages = [error.message for error in errors if isinstance(error, RowError)]
    assert [error.position for error in errors] == [0, 1, 2, 3]
    assert "amount" in messages[0]
    assert "date" in messages[1]
    assert "31/02/2026" in messages[2]
    assert "T" in messages[3]
    assert isinstance(errors[0], RowError)
    assert errors[0].raw == {"D": "10/11/2026", "P": "No amount"}
    assert isinstance(good, ParsedRow) and good.position == 4


def test_last_record_without_caret() -> None:
    content = BANK.removesuffix(b"^\n")
    assert len(_rows(_parse(content))) == 2


def test_second_account_rejected() -> None:
    content = BANK + b"!Type:Bank\nD12/11/2026\nT1.00\n^\n"
    with pytest.raises(FileFormatError, match="one account per file"):
        _parse(content)


@pytest.mark.parametrize(
    "header", [b"!Type:Invst", b"!Account", b"!Type:Bankx", b"Type:Bank", b"D10/11/2026"]
)
def test_unknown_type_rejected(header: bytes) -> None:
    content = BANK.replace(b"!Type:Bank", header)
    with pytest.raises(FileFormatError):
        _parse(content)


@pytest.mark.parametrize("content", [b"", b"\n\n", b"!Type:Bank\n", b"!Type:Bank\n^\n"])
def test_no_records_is_file_error(content: bytes) -> None:
    with pytest.raises(FileFormatError):
        _parse(content)


def test_wrong_encoding_is_file_error() -> None:
    content = BANK.replace(b"Example Cafe", "Café".encode("cp1252"))
    with pytest.raises(FileFormatError, match="utf-8"):
        _parse(content)
    row = _rows(_parse(content, encoding="cp1252"))[0]
    assert row.payee == "Café"


def test_too_many_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(parsed, "MAX_IMPORT_ROWS", 1)
    with pytest.raises(FileFormatError, match="more than 1 rows"):
        _parse(BANK)


def test_many_repeated_tags_are_linear() -> None:
    content = b"!Type:Bank\nD10/11/2026\nT-1.00\n" + b"Ax\n" * 50_000 + b"^\n"
    started = time.perf_counter()
    row = _rows(_parse(content))[0]
    assert time.perf_counter() - started < 2
    assert row.raw["A"] == "x" and row.raw["A#50000"] == "x"
