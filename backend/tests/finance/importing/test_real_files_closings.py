"""The real-files check's closings (test_real_files.py, which runs only with real files):
every stated closing balance is checked on some day, none skipped."""

from datetime import date, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from lararium.modules.finance.importing.parsed import ParsedStatement
from lararium.modules.finance.importing.pdf import parse_pdf
from lararium.modules.finance.importing.profiles import PdfProfileConfig

from .synthetic_pdfs import HsbcStatement, TemboRow, TemboStatement, hsbc_pdf, tembo_pdf
from .test_pdf_layouts import THREE_ROWS
from .test_real_files import closings

LONDON = ZoneInfo("Europe/London")


def test_undated_closing_is_checked_at_the_start_of_its_last_row_day() -> None:
    """Review: Tembo's Plan value has no date, and the check skipped it; then, checked at
    the day's end, a later file's row that day made it fail though it was right."""
    rows = [
        TemboRow("03/05/2027", "Deposit", "£50.00"),
        TemboRow("10/05/2027", "Interest", "£0.10"),
        TemboRow("10/05/2027", "Deposit", "£5.00"),
    ]
    statement = parse_pdf(
        tembo_pdf(TemboStatement(pages=[rows])),
        PdfProfileConfig(format="pdf", layout="tembo_statement"),
        LONDON,
    )
    assert statement.closing_balance_date is None
    # Before 10 May the ledger must hold 55.10 less that day's 5.10.
    assert closings([statement]) == ([(date(2027, 5, 10), Decimal("50.00"))], 0)


def test_dated_closing_is_checked_on_its_date() -> None:
    statement = parse_pdf(
        hsbc_pdf(HsbcStatement(pages=[THREE_ROWS])),
        PdfProfileConfig(format="pdf", layout="hsbc_uk_statement"),
        LONDON,
    )
    assert statement.closing_balance is not None and statement.closing_balance_date is not None
    assert closings([statement]) == (
        [(statement.closing_balance_date + timedelta(days=1), statement.closing_balance)],
        0,
    )


def test_closing_with_no_day_is_counted_not_skipped() -> None:
    statement = ParsedStatement(rows=[], closing_balance=Decimal("1.00"))
    assert closings([statement]) == ([], 1)
