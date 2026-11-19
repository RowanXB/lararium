import time
from datetime import date, datetime
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from lararium.modules.finance.errors import FileFormatError
from lararium.modules.finance.importing import parsed
from lararium.modules.finance.importing.csv_parser import parse_csv
from lararium.modules.finance.importing.parsed import (
    Counterparty,
    ParsedRow,
    ParsedStatement,
    RowError,
    SkippedRow,
    StatementAccount,
    StatementRate,
    parse_amount,
)
from lararium.modules.finance.importing.profiles import AmountFormat, CsvProfileConfig

LONDON = ZoneInfo("Europe/London")
SIMPLE = b"Date,Description,Amount\n10/11/2026,Coffee,-3.50\n11/11/2026,Salary,2000.00\n"


def _config(**overrides: Any) -> CsvProfileConfig:
    config: dict[str, Any] = {
        "format": "csv",
        "columns": {"date": "Date", "description": ["Description"], "amount": "Amount"},
        "date_format": "%d/%m/%Y",
    }
    return CsvProfileConfig.model_validate(config | overrides)


def _parse(content: bytes, **overrides: Any) -> ParsedStatement:
    return parse_csv(content, _config(**overrides), LONDON)


def _rows(statement: ParsedStatement) -> list[ParsedRow]:
    rows = [row for row in statement.rows if isinstance(row, ParsedRow)]
    assert len(rows) == len(statement.rows), statement.rows
    return rows


def test_signed_amount_column() -> None:
    rows = _rows(_parse(SIMPLE))
    assert [row.quantity for row in rows] == [Decimal("-3.50"), Decimal("2000.00")]
    assert [row.booking_date for row in rows] == [date(2026, 11, 10), date(2026, 11, 11)]
    assert [row.position for row in rows] == [0, 1]
    assert [row.description for row in rows] == ["Coffee", "Salary"]
    assert all(row.occurred_at is None and row.fee == 0 for row in rows)
    assert all(row.balance is None and row.currency is None for row in rows)


def test_outflow_positive_sign() -> None:
    rows = _rows(_parse(SIMPLE, sign="outflow_positive"))
    assert [row.quantity for row in rows] == [Decimal("3.50"), Decimal("-2000.00")]


def test_inflow_outflow_columns() -> None:
    content = b'Date,Description,Paid in,Paid out\n10/11/2026,Rent,,"1,234.56"\n'
    columns = {"date": "Date", "description": ["Description"]}
    rows = _rows(
        _parse(
            content,
            columns=columns | {"inflow": "Paid in", "outflow": "Paid out"},
            amount_format={"thousands_separator": ","},
            sign="outflow_positive",  # ignored with two columns
        )
    )
    assert [row.quantity for row in rows] == [Decimal("-1234.56")]


@pytest.mark.parametrize("cell", ["-12.00", "(12.00)"])
def test_signed_cell_in_unsigned_column_is_row_error(cell: str) -> None:
    """Review finding: a negative outflow was subtracted, booking a debit as a credit."""
    content = f"Date,Description,Paid in,Paid out\n10/11/2026,Shop,,{cell}\n".encode()
    columns = {"date": "Date", "description": ["Description"]}
    (row,) = _parse(content, columns=columns | {"inflow": "Paid in", "outflow": "Paid out"}).rows
    assert isinstance(row, RowError)
    assert "Paid out" in row.message and "negative" in row.message


def test_wide_padded_header_stays_linear() -> None:
    """Review finding: every short row was padded to the header's width, so a header with
    thousands of empty trailing cells cost gigabytes for a small file."""
    header = b"Date,Description,Amount" + b"," * 16_381 + b"\n"
    content = header + b"10/11/2026,Coffee,-3.50\n" * 2_000
    started = time.perf_counter()
    rows = _rows(_parse(content))
    assert time.perf_counter() - started < 2
    assert rows[0].raw == {"Date": "10/11/2026", "Description": "Coffee", "Amount": "-3.50"}


def test_padded_row_under_short_header() -> None:
    """A spreadsheet's trailing empty cells past the header are padding, not extra data."""
    rows = _rows(_parse(b"Date,Description,Amount\n10/11/2026,Coffee,-3.50,,,\n"))
    assert rows[0].quantity == Decimal("-3.50")


def test_short_row_has_its_mapped_columns() -> None:
    content = b"Date,Description,Amount,Balance,Note\n10/11/2026,Coffee,-3.50\n"
    rows = _rows(_parse(content, columns=_config().columns.model_dump() | {"balance": "Balance"}))
    assert rows[0].balance is None
    assert rows[0].raw == {
        "Date": "10/11/2026",
        "Description": "Coffee",
        "Amount": "-3.50",
        "Balance": "",
    }


def test_parentheses_and_currency_symbol() -> None:
    content = "Date,Description,Amount\n10/11/2026,Card,(£12.00)\n".encode()
    assert _rows(_parse(content))[0].quantity == Decimal("-12.00")
    content = 'Date;Description;Amount\n10/11/2026;Card;"€1.234,50"\n'.encode()
    rows = _rows(
        _parse(
            content,
            delimiter=";",
            amount_format={"decimal_separator": ",", "thousands_separator": "."},
        )
    )
    assert rows[0].quantity == Decimal("1234.50")


def test_utf8_bom_header() -> None:
    rows = _rows(_parse(b"\xef\xbb\xbf" + SIMPLE, encoding="utf-8-sig"))
    assert rows[0].booking_date == date(2026, 11, 10)


def test_cp1252_pound_sign() -> None:
    content = b"Date,Description,Amount\n10/11/2026,Caf\xe9 \xa3,-1.00\n"
    assert _rows(_parse(content, encoding="cp1252"))[0].description == "Café £"


def test_wrong_encoding_is_file_error() -> None:
    content = b"Date,Description,Amount\n10/11/2026,\xa3,-1.00\n"
    with pytest.raises(FileFormatError, match="utf-8"):
        _parse(content, encoding="utf-8")


def test_missing_column_is_file_error() -> None:
    with pytest.raises(FileFormatError, match="Amount"):
        _parse(b"Date,Description,Value\n10/11/2026,Coffee,-3.50\n")


def test_empty_file_is_file_error() -> None:
    with pytest.raises(FileFormatError, match="no rows"):
        _parse(b"Date,Description,Amount\n\n,,\n")
    with pytest.raises(FileFormatError, match="no rows"):
        _parse(b"")


def test_bad_row_is_row_error_not_exception() -> None:
    content = (
        "Date,Description,Amount,Currency\n"
        "10/11/2026,Good,-1.00,gbp\n"
        "31/02/2026,Bad date,-1.00,GBP\n"
        "10/11/2026,Bad amount,12.3.4,GBP\n"
        "10/11/2026,Arabic digits,١٢,GBP\n"
        "10/11/2026,Bad currency,-1.00,POUND\n"
        "10/11/2026,Empty amount,,GBP\n"
        "10/11/2026,Too many cells,-1.00,GBP,extra\n"
        "11/11/2026,Good,2.00, GBP \n"
    ).encode()
    statement = _parse(
        content,
        columns={
            "date": "Date",
            "description": ["Description"],
            "amount": "Amount",
            "currency": "Currency",
        },
    )
    rows = statement.rows
    assert [row.position for row in rows] == list(range(8))
    errors = [row for row in rows if isinstance(row, RowError)]
    assert [error.position for error in errors] == [1, 2, 3, 4, 5, 6]
    assert "31/02/2026" in errors[0].message
    assert "12.3.4" in errors[1].message
    assert "١٢" in errors[2].message
    assert "POUND" in errors[3].message
    assert "empty" in errors[4].message
    assert errors[1].raw == {
        "Date": "10/11/2026",
        "Description": "Bad amount",
        "Amount": "12.3.4",
        "Currency": "GBP",
    }
    good = [row for row in rows if isinstance(row, ParsedRow)]
    assert [(row.position, row.quantity, row.currency) for row in good] == [
        (0, Decimal("-1.00"), "GBP"),
        (7, Decimal("2.00"), "GBP"),
    ]


def test_blank_lines_ignored_and_positions_contiguous() -> None:
    content = (
        b"Date,Description,Amount\n\n10/11/2026,One,-1.00\n,,\n  , ,\n\n11/11/2026,Two,-2.00\n\n"
    )
    rows = _rows(_parse(content))
    assert [(row.position, row.description) for row in rows] == [(0, "One"), (1, "Two")]


def test_skip_rows_drops_lines_before_header() -> None:
    content = b"Example Bank export\nAccount: Current\n" + SIMPLE
    assert len(_rows(_parse(content, skip_rows=2))) == 2


def test_datetime_dates_with_comma_format() -> None:
    content = (
        b"Date,Description,Details,Money out (GBP),Money in (GBP),Balance (GBP)\n"
        b'"15 Oct, 2026 09:05",Transfer,  to   Example  Saver,,"1,000.00","1,500.00"\n'
    )
    statement = parse_csv(
        content,
        _config(
            columns={
                "date": "Date",
                "description": ["Description", "Details"],
                "inflow": "Money in (GBP)",
                "outflow": "Money out (GBP)",
                "balance": "Balance (GBP)",
            },
            date_format="%d %b, %Y %H:%M",
            amount_format={"thousands_separator": ","},
        ),
        ZoneInfo("Europe/London"),
    )
    (row,) = _rows(statement)
    assert row.booking_date == date(2026, 10, 15)
    assert row.occurred_at == datetime(2026, 10, 15, 9, 5, tzinfo=ZoneInfo("Europe/London"))
    assert row.description == "Transfer to Example Saver"
    assert (row.quantity, row.balance) == (Decimal("1000.00"), Decimal("1500.00"))


def test_include_only_skips_before_parsing() -> None:
    content = (
        b"Type,Started Date,Completed Date,Description,Amount,Fee,Currency,State,Balance\n"
        b"CARD_PAYMENT,2026-11-01 10:00:00,2026-11-02 09:00:00,Shop,"
        b"-5.00,0.00,GBP,COMPLETED,95.00\n"
        b"CARD_PAYMENT,2026-11-03 10:00:00,,Shop,-7.00,0.00,GBP,PENDING,\n"
    )
    statement = _parse(
        content,
        columns={
            "date": "Completed Date",
            "description": ["Description"],
            "amount": "Amount",
            "fee": "Fee",
            "currency": "Currency",
            "balance": "Balance",
            "type": "Type",
        },
        date_format="%Y-%m-%d %H:%M:%S",
        include_only={"State": ["COMPLETED"]},
    )
    completed, pending = statement.rows
    assert isinstance(completed, ParsedRow)
    assert (completed.balance, completed.bank_type) == (Decimal("95.00"), "CARD_PAYMENT")
    assert isinstance(pending, SkippedRow)
    assert pending.position == 1
    assert "State" in pending.reason and "PENDING" in pending.reason
    assert pending.raw["State"] == "PENDING"


def test_fee_column() -> None:
    content = (
        b"Date,Description,Amount,Fee\n10/11/2026,Exchange,-10.00,0.50\n10/11/2026,Card,-2.00,\n"
    )
    rows = _rows(
        _parse(
            content,
            columns={
                "date": "Date",
                "description": ["Description"],
                "amount": "Amount",
                "fee": "Fee",
            },
        )
    )
    assert [(row.quantity, row.fee) for row in rows] == [
        (Decimal("-10.00"), Decimal("0.50")),
        (Decimal("-2.00"), Decimal(0)),
    ]


def test_currency_symbol_in_quoted_amounts() -> None:
    content = 'Date,Description,Amount\n10/11/2026,Salary,"£12,345.67"\n'.encode()
    rows = _rows(_parse(content, amount_format={"thousands_separator": ","}))
    assert rows[0].quantity == Decimal("12345.67")


def test_too_many_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(parsed, "MAX_IMPORT_ROWS", 3)
    content = b"Date,Description,Amount\n" + b"10/11/2026,Coffee,-3.50\n" * 3
    assert len(_parse(content).rows) == 3
    with pytest.raises(FileFormatError):
        _parse(content + b"10/11/2026,Coffee,-3.50\n")


def test_headerless_csv() -> None:
    rows = _rows(_parse(b"10/11/2026,Coffee,-3.50\n", header=["Date", "Description", "Amount"]))
    assert [(row.description, row.quantity) for row in rows] == [("Coffee", Decimal("-3.50"))]


MIDATA = (
    "Date,Type,Merchant/Description,Debit/Credit,Balance\n"
    "2026-11-03,DD,EXAMPLE ENERGY,-£40.00,£960.00\n"
    "2026-11-02,CR,EXAMPLE EMPLOYER,+£1000.00,£1000.00\n"
    "\n"
    "Arranged overdraft limit,2026-11-01,+£100.00\n"
).encode()
MIDATA_CONFIG: dict[str, Any] = {
    "columns": {
        "date": "Date",
        "description": ["Merchant/Description"],
        "amount": "Debit/Credit",
        "balance": "Balance",
        "type": "Type",
    },
    "date_format": "%Y-%m-%d",
    "skip_footer_rows": 1,
    "row_order": "newest_first",
}


def test_footer_rows_skipped() -> None:
    statement = _parse(MIDATA, **MIDATA_CONFIG)
    rows = _rows(statement)
    assert [(row.quantity, row.balance, row.bank_type) for row in rows] == [
        (Decimal("-40.00"), Decimal("960.00"), "DD"),
        (Decimal("1000.00"), Decimal("1000.00"), "CR"),
    ]
    assert (statement.overdraft_limit, statement.overdraft_limit_date) == (None, None)


def test_missing_footer_refuses_rather_than_drops_a_row() -> None:
    """Third review: with no footer line, skip_footer_rows silently dropped the last
    transaction."""
    no_footer = MIDATA.split(b"\n\n")[0] + b"\n"
    label = {"overdraft_footer_label": "Arranged overdraft limit"}
    with pytest.raises(FileFormatError, match="reads as a transaction"):
        _parse(no_footer, **MIDATA_CONFIG, **label)


def test_padded_midata_footer_keeps_its_amount() -> None:
    """Review finding: a spreadsheet re-save pads every line with commas, and the footer's
    amount was then read from an empty last cell."""
    padded = MIDATA.replace(b"+\xc2\xa3100.00\n", b"+\xc2\xa3100.00,,\n")
    assert padded != MIDATA
    label = {"overdraft_footer_label": "Arranged overdraft limit"}
    assert _parse(padded, **MIDATA_CONFIG, **label).overdraft_limit == Decimal("100.00")


def test_midata_overdraft_footer() -> None:
    label = {"overdraft_footer_label": "Arranged overdraft limit"}
    statement = _parse(MIDATA, **MIDATA_CONFIG, **label)
    assert len(_rows(statement)) == 2
    assert statement.overdraft_limit == Decimal("100.00")
    assert statement.overdraft_limit_date == date(2026, 11, 1)
    # Another footer line is still just dropped.
    other = MIDATA.replace(b"Arranged overdraft limit", b"Generated by Example Bank")
    assert _parse(other, **MIDATA_CONFIG, **label).overdraft_limit is None
    # No cell parses as a date, the last cell isn't an amount, or the line has one cell.
    for broken in ["no date,+£100.00", "2026-11-01,lots", ""]:
        footer = f"Arranged overdraft limit,{broken}".rstrip(",").encode()
        content = MIDATA.replace("Arranged overdraft limit,2026-11-01,+£100.00".encode(), footer)
        with pytest.raises(FileFormatError, match="Arranged overdraft limit"):
            _parse(content, **MIDATA_CONFIG, **label)


def test_signed_pound_amounts() -> None:
    content = "Date,Description,Amount\n10/11/2026,A,-£12.34\n10/11/2026,B,+£1,000.00\n"
    content = content.replace("+£1,000.00", '"+£1,000.00"')
    rows = _rows(_parse(content.encode(), amount_format={"thousands_separator": ","}))
    assert [row.quantity for row in rows] == [Decimal("-12.34"), Decimal("1000.00")]


def test_whole_number_amounts() -> None:
    content = b"Date,Description,Debit Amount,Credit Amount\n10/11/2026,Gym,85,\n"
    rows = _rows(
        _parse(
            content,
            columns={
                "date": "Date",
                "description": ["Description"],
                "inflow": "Credit Amount",
                "outflow": "Debit Amount",
            },
        )
    )
    assert rows[0].quantity == Decimal(-85)


def test_type_column_kept() -> None:
    content = b"Date,Type,Description,Amount\n10/11/2026,FPO,Rent,-500.00\n10/11/2026,,Cash,-5\n"
    columns = {"date": "Date", "description": ["Description"], "amount": "Amount", "type": "Type"}
    rows = _rows(_parse(content, columns=columns))
    assert [row.bank_type for row in rows] == ["FPO", None]


def test_raw_keeps_unmapped_columns() -> None:
    content = b"Date,Description,Amount,Notes\n10/11/2026,Coffee,-3.50, with oat milk \n"
    (row,) = _rows(_parse(content))
    assert row.raw == {
        "Date": "10/11/2026",
        "Description": "Coffee",
        "Amount": "-3.50",
        "Notes": " with oat milk ",
    }


def test_typed_fields_from_columns() -> None:
    content = (
        b"Date,Value Date,Description,Payee,Memo,Cheque,Name,Sort,Account,Ref,Amount,Id\n"
        b"10/11/2026,11/11/2026,Rent,Example Lettings,November,000123,"
        b"Example Lettings Ltd,12-34-56,1234 5678,FLAT 1,-500.00,T-1\n"
        b"10/11/2026,,Coffee,,,,,,,,-3.50,\n"
        b"10/11/2026,30/02/2026,Bad value date,,,,,,,,-3.50,\n"
    )
    statement = _parse(
        content,
        columns={
            "date": "Date",
            "description": ["Description"],
            "amount": "Amount",
            "external_id": "Id",
            "value_date": "Value Date",
            "payee": "Payee",
            "memo": "Memo",
            "check_number": "Cheque",
            "counterparty_name": "Name",
            "counterparty_sort_code": "Sort",
            "counterparty_account_number": "Account",
            "reference": "Ref",
        },
    )
    rent, coffee, bad = statement.rows
    assert isinstance(rent, ParsedRow)
    assert rent.value_date == date(2026, 11, 11)
    assert (rent.payee, rent.memo, rent.check_number) == ("Example Lettings", "November", "000123")
    assert rent.external_id == "T-1"
    assert rent.counterparty == Counterparty(
        name="Example Lettings Ltd",
        sort_code="123456",
        account_number="12345678",
        reference="FLAT 1",
    )
    assert isinstance(coffee, ParsedRow)
    assert (coffee.value_date, coffee.payee, coffee.memo, coffee.check_number) == (
        None,
        None,
        None,
        None,
    )
    assert (coffee.counterparty, coffee.external_id) == (None, None)
    assert isinstance(bad, RowError)
    assert "30/02/2026" in bad.message
    assert statement.account is None


def test_counterparty_normalisation() -> None:
    assert Counterparty(sort_code="12 34 56").sort_code == "123456"
    assert Counterparty(account_number="1234-5678").account_number == "12345678"
    assert StatementAccount(sort_code="'12-34-56", account_number="12345678") == StatementAccount(
        sort_code="123456", account_number="12345678"
    )
    for sort_code in ["12-34-5", "AB-CD-EF", "١٢-٣٤-٥٦"]:
        with pytest.raises(ValueError, match="sort code"):
            Counterparty(sort_code=sort_code)
    with pytest.raises(ValueError, match="account number"):
        StatementAccount(sort_code=None, account_number="ACC 1")


def test_statement_account_from_columns() -> None:
    header = (
        b"Transaction Date,Transaction Type,Sort Code,Account Number,"
        b"Transaction Description,Debit Amount,Credit Amount,Balance\n"
    )
    rows = (
        b"01/11/2026,DEB,'12-34-56,12345678,EXAMPLE SHOP,4.50,,95.50\n"
        b"02/11/2026,FPI,'12-34-56,12345678,EXAMPLE EMPLOYER,,100,195.50\n"
    )
    config = {
        "columns": {
            "date": "Transaction Date",
            "type": "Transaction Type",
            "description": ["Transaction Description"],
            "outflow": "Debit Amount",
            "inflow": "Credit Amount",
            "balance": "Balance",
            "account_sort_code": "Sort Code",
            "account_number": "Account Number",
        }
    }
    statement = _parse(header + rows, **config)
    assert len(_rows(statement)) == 2
    assert statement.account == StatementAccount(sort_code="123456", account_number="12345678")
    other = b"03/11/2026,DEB,'12-34-56,87654321,EXAMPLE SHOP,1.00,,194.50\n"
    with pytest.raises(FileFormatError, match="one account per file"):
        _parse(header + rows + other, **config)


def test_statement_account_normalises_iban() -> None:
    account = StatementAccount(iban="gb29 nwbk 6016 1331 9268 19", bic="EXAMBGB2")
    assert account.iban == "GB29NWBK60161331926819"
    assert (account.sort_code, account.account_number, account.name) == (None, None, None)


def test_statement_level_defaults() -> None:
    statement = ParsedStatement(rows=[])
    assert statement.rates == [] and statement.rates is not ParsedStatement(rows=[]).rates
    assert (statement.currency, statement.statement_reference, statement.document_date) == (
        None,
        None,
        None,
    )
    rate = StatementRate(
        kind="Arranged overdraft",
        band_from=None,
        band_to=Decimal(500),
        rate=Decimal("39.9"),
        ear=None,
    )
    assert rate.rate == Decimal("39.9")
    assert Counterparty(bank="Example Bank").bank == "Example Bank"


def test_parse_amount_rules() -> None:
    fmt = AmountFormat()
    assert parsed.parse_amount(" 12 ", fmt) == Decimal(12)
    assert parsed.parse_amount("$0.10", fmt) == Decimal("0.10")
    assert parsed.parse_amount("(0.00)", fmt) == Decimal("0.00")
    for text in ["", "-", "--1", "+-1", "(-1)", "(1", "££1", "1.", ".5", "1e5", "NaN", "1,000"]:
        with pytest.raises(ValueError, match="amount"):
            parsed.parse_amount(text, fmt)
    with pytest.raises(ValueError, match="1" * 41):
        parsed.parse_amount("1" * 41, fmt)


def test_clean_description() -> None:
    assert parsed.clean_description(["  A ", "", "b\t c  "]) == "A b c"
    assert len(parsed.clean_description(["x" * 1500])) == 1000


@pytest.mark.parametrize(
    ("text", "fmt", "expected"),
    [
        ("1,234.56", AmountFormat(thousands_separator=","), Decimal("1234.56")),
        ("-1,234,567", AmountFormat(thousands_separator=","), Decimal("-1234567")),
        ("£12.50", AmountFormat(thousands_separator=","), Decimal("12.50")),
        ("999", AmountFormat(thousands_separator=","), Decimal("999")),
        (
            "1.234,50",
            AmountFormat(decimal_separator=",", thousands_separator="."),
            Decimal("1234.50"),
        ),
        (
            "1 234,50",
            AmountFormat(decimal_separator=",", thousands_separator=" "),
            Decimal("1234.50"),
        ),
        ("12,50", AmountFormat(decimal_separator=","), Decimal("12.50")),
    ],
)
def test_parse_amount_thousands_grouping(text: str, fmt: AmountFormat, expected: Decimal) -> None:
    assert parsed.parse_amount(text, fmt) == expected


@pytest.mark.parametrize(
    "text", ["12,50", "-1,5", "1,23", "1234,567", ",123", "1,234,5", "1,234.5,6", "1,,234"]
)
def test_parse_amount_rejects_misgrouped_thousands(text: str) -> None:
    with pytest.raises(ValueError, match="not an amount"):
        parsed.parse_amount(text, AmountFormat(thousands_separator=","))


def test_misgrouped_amount_is_row_error_not_wrong_number() -> None:
    content = b'Date,Description,Amount\n10/11/2026,Card,"12,50"\n11/11/2026,Rent,"1,234.56"\n'
    statement = _parse(content, amount_format={"thousands_separator": ","})
    error, row = statement.rows
    assert isinstance(error, RowError) and "12,50" in error.message
    assert isinstance(row, ParsedRow) and row.quantity == Decimal("1234.56")


def test_repeated_unmapped_header_keeps_every_column() -> None:
    content = b"Date,Description,Amount,Note,Note\n10/11/2026,Coffee,-3.50,first,second\n"
    row = _rows(_parse(content))[0]
    assert row.raw == {
        "Date": "10/11/2026",
        "Description": "Coffee",
        "Amount": "-3.50",
        "Note": "first",
        "Note#2": "second",
    }


def test_repeated_mapped_header_is_file_error() -> None:
    content = b"Date,Description,Amount,Description\n10/11/2026,Coffee,-3.50,Shop\n"
    with pytest.raises(FileFormatError, match="Description"):
        _parse(content)


def test_repeated_include_only_header_is_file_error() -> None:
    content = b"Date,Description,Amount,State,State\n10/11/2026,Coffee,-3.50,DONE,PENDING\n"
    with pytest.raises(FileFormatError, match="State"):
        _parse(content, include_only={"State": ["DONE"]})


def test_repeated_name_in_configured_header_is_file_error() -> None:
    header = ["Date", "Amount", "Description", "Amount"]
    with pytest.raises(FileFormatError, match="Amount"):
        _parse(b"10/11/2026,-3.50,Coffee,-3.50\n", header=header)


BIG = "1234567890123456789012345678.91"  # 30 significant digits, past the default 28


@pytest.mark.parametrize("text", [f"-{BIG}", f"({BIG})"])
def test_parse_amount_negates_exactly(text: str) -> None:
    assert parse_amount(text, AmountFormat()) == Decimal(f"-{BIG}")


def test_large_amounts_keep_precision_through_signs() -> None:
    content = f"Date,Description,Amount\n10/11/2026,Big,{BIG}\n".encode()
    rows = _rows(_parse(content, sign="outflow_positive"))
    assert [row.quantity for row in rows] == [Decimal(f"-{BIG}")]
    two = f"Date,Description,In,Out\n10/11/2026,Big,0.01,{BIG}\n".encode()
    columns = {"date": "Date", "description": ["Description"], "inflow": "In", "outflow": "Out"}
    rows = _rows(_parse(two, columns=columns))
    assert rows[0].quantity == Decimal("-1234567890123456789012345678.90")
