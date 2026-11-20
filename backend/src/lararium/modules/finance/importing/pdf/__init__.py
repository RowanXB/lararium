"""Reads a PDF statement through its bank's layout, which must pass every self-check.

Layouts are positional code, one module per bank (Decision 15). A layout that misreads a
changed design fails one of the checks below, and the whole file is refused.
"""

from collections.abc import Callable
from decimal import Decimal
from zoneinfo import ZoneInfo

from lararium.modules.finance.errors import FileFormatError
from lararium.modules.finance.importing import parsed
from lararium.modules.finance.importing.parsed import (
    ParsedRow,
    ParsedStatement,
    RowError,
    SkippedRow,
)
from lararium.modules.finance.importing.pdf import boc_uk, hsbc_uk, lloyds, tembo
from lararium.modules.finance.importing.pdf.layout import LayoutResult
from lararium.modules.finance.importing.pdf.words import Word, visible_lines
from lararium.modules.finance.importing.profiles import PdfProfileConfig

PARSER_VERSION = "pdf-1"

type _Layout = tuple[Decimal, Callable[[list[list[Word]]], LayoutResult]]

_LAYOUTS: dict[str, _Layout] = {
    "hsbc_uk_statement": (hsbc_uk.X_TOLERANCE, hsbc_uk.parse),
    "lloyds_statement": (lloyds.X_TOLERANCE, lloyds.parse),
    "boc_uk_statement": (boc_uk.X_TOLERANCE, boc_uk.parse),
    "tembo_statement": (tembo.X_TOLERANCE, tembo.parse),
}


def _rows_check(result: LayoutResult) -> None:
    start, end = result.period
    for n, layout_row in enumerate(result.rows, start=1):
        row = layout_row.row
        where = f"row {n} (page {layout_row.page + 1})"
        if layout_row.amounts != 1:
            raise FileFormatError(f"{where} has {layout_row.amounts} amounts, not one")
        if not row.bank_type:
            raise FileFormatError(f"{where} has no type code")
        if not start <= row.booking_date <= end:
            raise FileFormatError(
                f"{where} is dated {row.booking_date}, outside the statement period "
                f"{start} to {end}"
            )


def _opening(result: LayoutResult) -> Decimal:
    """The printed opening balance or, when the statement prints none, the one its first
    printed balance implies: that balance then checks nothing, and every later one and the
    closing balance still do."""
    if result.opening_balance is not None:
        return result.opening_balance
    if not result.balances:
        raise FileFormatError("the statement prints no opening balance and no row balance")
    first = result.balances[0]
    moved = sum((r.row.quantity for r in result.rows[: first.after]), Decimal(0))
    return first.amount - moved


def _summary_check(result: LayoutResult) -> None:
    paid_in = [r.paid_in for r in result.rows if r.paid_in is not None]
    paid_out = [r.paid_out for r in result.rows if r.paid_out is not None]
    for what, stated, rows in (
        ("money in", result.money_in, paid_in),
        ("money out", result.money_out, paid_out),
    ):
        if stated is None:
            continue
        total = sum(rows, Decimal(0))
        if total != stated:
            raise FileFormatError(
                f"the statement's {what} is {stated} but its rows add up to {total}"
            )
    for what, count, rows in (
        ("in", result.payments_in, paid_in),
        ("out", result.payments_out, paid_out),
    ):
        if count is not None and count != len(rows):
            raise FileFormatError(
                f"the statement counts {count} payments {what} but has {len(rows)} rows paid {what}"
            )
    opening = _opening(result)
    closing = opening + sum((r.row.quantity for r in result.rows), Decimal(0))
    if closing != result.closing_balance:
        raise FileFormatError(
            f"the statement's opening balance {opening} and its rows give "
            f"{closing}, but its closing balance is {result.closing_balance}"
        )


def _balances_check(result: LayoutResult) -> None:
    running = [_opening(result)]
    for layout_row in result.rows:
        running.append(running[-1] + layout_row.row.quantity)
    for printed in result.balances:
        expected = running[printed.after]
        if printed.amount != expected:
            raise FileFormatError(
                f"the balance printed on page {printed.page + 1} is {printed.amount} "
                f"but the rows give {expected}"
            )


def check(result: LayoutResult) -> None:
    """Decision 15's self-checks; FileFormatError names the first that fails."""
    if result.unplaced:
        raise FileFormatError(f"words outside any column on page {result.unplaced[0].page + 1}")
    # Every layout reads the currency from the statement; without it the rows would take
    # the upload's default, and a currency account's amounts would be booked as GBP.
    if result.currency is None:
        raise FileFormatError("the statement doesn't print its currency")
    _rows_check(result)
    _summary_check(result)
    _balances_check(result)


@parsed.exact_decimals
def parse_pdf(content: bytes, config: PdfProfileConfig, local_tz: ZoneInfo) -> ParsedStatement:
    """local_tz is unused: statements print dates only, so occurred_at stays None."""
    del local_tz
    x_tolerance, layout = _LAYOUTS[config.layout]
    result = layout(visible_lines(content, x_tolerance=x_tolerance))
    if not result.rows:
        raise FileFormatError("no rows")
    if len(result.rows) > parsed.MAX_IMPORT_ROWS:
        raise FileFormatError(f"the file has more than {parsed.MAX_IMPORT_ROWS} rows")
    check(result)
    rows: list[ParsedRow | RowError | SkippedRow] = [r.row for r in result.rows]
    return ParsedStatement(
        rows=rows,
        closing_balance=result.closing_balance,
        # Only a statement that lists its last day in full closes at that day's end.
        closing_balance_date=result.period[1] if result.period_complete else None,
        period=result.period,
        period_complete=result.period_complete,
        opening_balance=result.opening_balance,
        money_in=result.money_in,
        money_out=result.money_out,
        payments_in=result.payments_in,
        payments_out=result.payments_out,
        account=result.account,
        currency=result.currency,
        statement_reference=result.statement_reference,
        document_date=result.document_date,
        rates=result.rates,
        available_balance=result.available_balance,
        available_balance_date=result.available_balance_date,
        account_opened_on=result.account_opened_on,
    )
