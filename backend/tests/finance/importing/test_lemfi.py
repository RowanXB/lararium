"""LemFi: the PDF layout (account and savings statements) and the catalog's CSV profile, on
synthetic files that copy only the real ones' structure."""

from datetime import date, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from lararium.modules.finance.errors import EmptyStatementError, FileFormatError
from lararium.modules.finance.importing.catalog import CATALOG
from lararium.modules.finance.importing.csv_parser import parse_csv
from lararium.modules.finance.importing.parsed import (
    Counterparty,
    ParsedRow,
    ParsedStatement,
    RowError,
    StatementAccount,
    StatementRate,
)
from lararium.modules.finance.importing.pdf import parse_pdf
from lararium.modules.finance.importing.profiles import CsvProfileConfig, PdfProfileConfig

from .synthetic_pdfs import LemfiRow, LemfiStatement, lemfi_pdf

LONDON = ZoneInfo("Europe/London")
PDF = PdfProfileConfig(format="pdf", layout="lemfi_statement")
HEADER = "Date,Description,Details,Money out (GBP),Money in (GBP),Balance (GBP)"

FROM_SAVINGS = LemfiRow(
    "02 May, 2027", ("Received from LemFi savings", "account"), paid_in="500.00"
)
IN_FROM_BANK = LemfiRow(
    "03 May, 2027",
    ("Received from A Example",),
    ("Via EXAMPLE BANK LIMITED *1234",),
    paid_in="1,250.00",
    time="09:05",
)
OUT_TO_BANK = LemfiRow(
    "04 May, 2027",
    ("Sent money to A Example",),
    ("Example Bank, 12345678, transfer from", "A Example"),
    paid_out="1,700.00",
    time="23:59",
)
ACCOUNT_ROWS = [FROM_SAVINGS, IN_FROM_BANK, OUT_TO_BANK]


def _parse(statement: LemfiStatement) -> ParsedStatement:
    return parse_pdf(lemfi_pdf(statement), PDF, LONDON)


def _rows(statement: ParsedStatement) -> list[ParsedRow]:
    rows = [row for row in statement.rows if isinstance(row, ParsedRow)]
    assert len(rows) == len(statement.rows)
    return rows


def _savings(rows: list[LemfiRow], **changes: object) -> LemfiStatement:
    return LemfiStatement([rows], savings=True, opening="20,000.00", **changes)  # pyright: ignore[reportArgumentType]


SAVINGS_ROWS = [
    LemfiRow("01 May, 2027", ("Savings Interest Payout",), paid_in="80.12", time="02:32"),
    LemfiRow("10 May, 2027", ("Savings Funding",), paid_in="1,000.00"),
    LemfiRow("30 May, 2027", ("Savings Withdrawal",), paid_out="2,000.00"),
]


# The account statement ----------------------------------------------------------------


def test_account_statement() -> None:
    statement = _parse(LemfiStatement([ACCOUNT_ROWS]))
    assert statement.period == (date(2027, 5, 1), date(2027, 5, 31))
    assert statement.period_complete
    assert statement.document_date == date(2027, 6, 19)
    assert statement.currency == "GBP"
    assert (statement.opening_balance, statement.closing_balance) == (Decimal(0), Decimal(50))
    assert (statement.money_in, statement.money_out) == (Decimal("1750.00"), Decimal("1700.00"))
    assert statement.interest is None
    assert statement.account == StatementAccount(
        sort_code="401234",
        account_number="12345678",  # from the UK IBAN
        iban="GB99EXAM40123412345678",
        bank="Example Bank",
        addressee="A Example",
        address="1 Example Street,Exampletown, Exampleshire, EX1 2MP",
    )
    assert statement.rates == []


def test_account_rows() -> None:
    rows = _rows(_parse(LemfiStatement([ACCOUNT_ROWS])))
    assert [row.description for row in rows] == [
        "Received from LemFi savings account",
        "Received from A Example",
        "Sent money to A Example",
    ]
    assert [row.quantity for row in rows] == [Decimal("500.00"), Decimal("1250.00"), -1700]
    assert [row.balance for row in rows] == [Decimal(500), Decimal(1750), Decimal(50)]
    assert [row.occurred_at for row in rows] == [
        datetime(2027, 5, 2, 13, 28, tzinfo=LONDON),
        datetime(2027, 5, 3, 9, 5, tzinfo=LONDON),
        datetime(2027, 5, 4, 23, 59, tzinfo=LONDON),
    ]
    assert all(row.bank_type is None and row.currency == "GBP" for row in rows)
    assert [row.counterparty for row in rows] == [
        Counterparty(name="LemFi savings account"),
        Counterparty(name="A Example", bank="EXAMPLE BANK LIMITED", account_ending="1234"),
        Counterparty(
            name="A Example",
            bank="Example Bank",
            account_number="12345678",
            reference="transfer from A Example",
        ),
    ]
    assert rows[2].raw == {
        "Date": "04 May, 2027 23:59",
        "Description": "Sent money to A Example",
        "Details": "Example Bank, 12345678, transfer from A Example",
        "Money out (GBP)": "1,700.00",
        "Balance (GBP)": "50.00",
    }


def test_rows_over_pages_and_carried_over() -> None:
    pages = [[FROM_SAVINGS, IN_FROM_BANK], [OUT_TO_BANK]]
    for carry_over in (False, True):
        rows = _rows(_parse(LemfiStatement(pages, carry_over=carry_over)))
        assert [row.counterparty for row in rows] == [
            row.counterparty for row in _rows(_parse(LemfiStatement([ACCOUNT_ROWS])))
        ]
        assert rows[1].occurred_at == datetime(2027, 5, 3, 9, 5, tzinfo=LONDON)


def test_a_missing_page_refuses_the_statement() -> None:
    """Review finding: without its last page a statement still added up (that page held only
    a row's carried-over lines), so it would vouch for a period it doesn't list in full."""
    with pytest.raises(FileFormatError, match="page 1 isn't numbered 1 of 1"):
        _parse(LemfiStatement([ACCOUNT_ROWS], printed_pages=2))


def test_page_numbers_are_read_from_the_footer_only() -> None:
    """Review finding: a reference printing "Page 1 of 2" refused a whole statement."""
    out = LemfiRow(
        "04 May, 2027",
        ("Sent money to A Example",),
        ("Example Bank, 12345678, Page 1 of 2",),
        paid_out="5.00",
    )
    [row] = _rows(_parse(LemfiStatement([[out]], opening="5.00")))
    assert row.counterparty is not None
    assert row.counterparty.reference == "Page 1 of 2"


def test_details_line_like_a_footer_stays_in_the_row() -> None:
    """Review finding: a details line starting "©" ended the table."""
    out = LemfiRow(
        "04 May, 2027",
        ("Sent money to A Example",),
        ("Example Bank, 12345678, A", "© invoice 24"),
        paid_out="5.00",
    )
    [row] = _rows(_parse(LemfiStatement([[out]], opening="5.00")))
    assert row.counterparty is not None
    assert row.counterparty.reference == "A © invoice 24"


def test_details_that_read_as_neither_shape_refuse_the_statement() -> None:
    odd = LemfiRow("04 May, 2027", ("Sent money to A Example",), ("Somewhere else",), "1.00")
    with pytest.raises(FileFormatError, match=r"row 1 \(page 1\): the details"):
        _parse(LemfiStatement([[odd]]))


def test_iban_and_sort_code_must_agree() -> None:
    with pytest.raises(FileFormatError, match="sort code isn't the one in its IBAN"):
        _parse(LemfiStatement([ACCOUNT_ROWS], sort_code="409999"))


def test_a_word_outside_the_columns_refuses_the_statement() -> None:
    with pytest.raises(FileFormatError, match="words outside any column on page 1"):
        _parse(LemfiStatement([ACCOUNT_ROWS], extra=[(0, 32.0, 486.0, "stray")]))


# Summary checks -----------------------------------------------------------------------


def test_totals_may_be_a_penny_short() -> None:
    """LemFi cuts its totals down to the penny: one may be a penny below its rows."""
    short = _parse(LemfiStatement([ACCOUNT_ROWS], summary={"Money in": "1,749.99"}))
    assert short.money_in == Decimal("1749.99")
    for printed in ("1,749.98", "1,750.01"):
        with pytest.raises(FileFormatError, match="money in is"):
            _parse(LemfiStatement([ACCOUNT_ROWS], summary={"Money in": printed}))


def test_closing_balance_follows_from_the_rows() -> None:
    with pytest.raises(FileFormatError, match=r"closing balance is 50.01"):
        _parse(LemfiStatement([ACCOUNT_ROWS], summary={"Closing balance": "50.01"}))


def test_a_printed_balance_must_follow_from_the_rows() -> None:
    wrong = LemfiRow(
        "02 May, 2027", ("Received from LemFi savings",), paid_in="5.00", balance="6.00"
    )
    with pytest.raises(FileFormatError, match=r"balance printed on page 1 is 6.00"):
        _parse(LemfiStatement([[wrong]]))


def test_empty_statement_is_refused_as_empty() -> None:
    """A period with no transactions: nothing to import (a batch speaks for its days only in
    the currencies it has rows in), so it's refused, but only once its summary says empty."""
    with pytest.raises(EmptyStatementError):
        _parse(LemfiStatement([[]], opening="12.00"))
    with pytest.raises(FileFormatError, match=r"money in is 5.00") as error:
        _parse(LemfiStatement([[]], summary={"Money in": "5.00"}))
    assert not isinstance(error.value, EmptyStatementError)


# The savings statement ----------------------------------------------------------------


def test_savings_statement() -> None:
    statement = _parse(_savings(SAVINGS_ROWS))
    assert statement.period == (date(2027, 5, 1), date(2027, 5, 31))
    assert statement.opening_balance == Decimal("20000.00")
    assert (statement.money_in, statement.money_out) == (Decimal("1000.00"), Decimal("2000.00"))
    assert statement.interest == Decimal("80.12")
    assert statement.closing_balance == Decimal("19080.12")
    assert statement.account == StatementAccount(
        name="Example Instant Saver",
        addressee="A Example",
        address="1 Example Street,Exampletown, Exampleshire, EX1 2MP",
    )
    assert statement.rates == [
        StatementRate(
            kind="Interest rate",
            band_from=None,
            band_to=None,
            rate=Decimal("4.02"),
            ear=Decimal("4.10"),
            valid_from=date(2027, 5, 2),
            valid_to=date(2027, 10, 29),
            frequency="monthly",
        )
    ]
    rows = _rows(statement)
    assert [row.counterparty for row in rows] == [None, None, None]
    assert rows[0].occurred_at == datetime(2027, 5, 1, 2, 32, tzinfo=LONDON)


def test_savings_period_with_a_dash() -> None:
    statement = _parse(_savings(SAVINGS_ROWS, period="1 May 2027 — 31 May 2027"))
    assert statement.period == (date(2027, 5, 1), date(2027, 5, 31))


def test_interest_must_add_up_exactly() -> None:
    """Review finding: the penny allowed on money in and out let interest be a penny short."""
    with pytest.raises(FileFormatError, match=r"interest is 80.11"):
        _parse(_savings(SAVINGS_ROWS, summary={"Interest accrued": "80.11"}))


def test_interest_counts_apart_from_money_in() -> None:
    with pytest.raises(FileFormatError, match=r"interest is 0.00 but its rows add up to 80.12"):
        _parse(_savings(SAVINGS_ROWS, summary={"Interest accrued": "0.00"}))
    # An interest payout on a statement that totals no interest isn't money in either.
    with pytest.raises(FileFormatError, match="interest is 0 but"):
        _parse(LemfiStatement([SAVINGS_ROWS[:1]]))


def test_rate_text_must_read_as_aer_and_gross() -> None:
    with pytest.raises(FileFormatError, match="interest rate doesn't read"):
        _parse(_savings(SAVINGS_ROWS, rate="Variable, see the app."))


# The CSV export -----------------------------------------------------------------------


def _csv_config() -> CsvProfileConfig:
    [entry] = [e for e in CATALOG if (e.institution, e.label) == ("LemFi", "CSV")]
    assert isinstance(entry.config, CsvProfileConfig)
    return entry.config


def _csv(*lines: str) -> ParsedStatement:
    content = "﻿" + "\n".join([HEADER, *lines]) + "\n"
    return parse_csv(content.encode(), _csv_config(), LONDON)


ACCOUNT_CSV = [
    '"02 May, 2027 13:28",Received from LemFi savings account,,,500.00,500.00',
    '"03 May, 2027 09:05",Received from A Example,Via EXAMPLE BANK LIMITED *1234,,"1,250.00",'
    '"1,750.00"',
    '"04 May, 2027 23:59",Sent money to A Example,'
    '"Example Bank, 12345678, transfer from A Example","1,700.00",,50.00',
]


def test_csv_rows_equal_the_pdf_rows() -> None:
    """Same typed fields, description and balance: the two formats' dedupe keys agree."""
    csv_rows = _rows(_csv(*ACCOUNT_CSV))
    pdf_rows = _rows(_parse(LemfiStatement([ACCOUNT_ROWS])))
    fields = (
        "booking_date",
        "occurred_at",
        "description",
        "quantity",
        "currency",
        "balance",
        "counterparty",
    )
    for csv_row, pdf_row in zip(csv_rows, pdf_rows, strict=True):
        assert {f: getattr(csv_row, f) for f in fields} == {f: getattr(pdf_row, f) for f in fields}
        assert csv_row.raw == {"Money in (GBP)": "", "Money out (GBP)": ""} | pdf_row.raw


def test_csv_details_that_read_as_neither_shape_are_row_errors() -> None:
    [row] = _csv('"04 May, 2027 23:59",Sent money to A Example,Somewhere else,1.00,,0.00').rows
    assert isinstance(row, RowError)
    assert row.message == "Details matches none of the profile's patterns for it"


def test_header_only_csv_is_refused_as_empty() -> None:
    with pytest.raises(EmptyStatementError):
        _csv()
