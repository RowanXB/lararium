"""Lloyds statements (iText, tagged for screen readers). Pure: a function of the words.

A second text layer of 0.1 pt white text repeats the column labels and writes empty cells
as "blank."; visible_lines drops it. Amount columns are positioned under the table
header's "Money In (£)", "Money Out (£)" and "Balance (£)"; every row prints a balance.
"""

import re
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from lararium.modules.finance.errors import FileFormatError
from lararium.modules.finance.importing.parsed import ParsedRow, StatementAccount, clean_description
from lararium.modules.finance.importing.pdf.layout import (
    LayoutResult,
    LayoutRow,
    PrintedBalance,
    after,
    amount,
    find,
    last_word_of,
    nearest,
    pages,
    parse_date,
    split_amounts,
    text,
    word_of,
)
from lararium.modules.finance.importing.pdf.words import Word

X_TOLERANCE = Decimal("1.5")

_DATE = r"\d{1,2} [A-Z][a-z]+ \d{4}"
_PERIOD = re.compile(rf"(?:(?P<product>.+) )?(?P<start>{_DATE}) to (?P<end>{_DATE})")
_SUMMARY = re.compile(
    rf"Money (?P<way>In|Out) (?P<money>\S+) Balance on (?P<on>{_DATE}) (?P<balance>\S+)"
)
_PAGE = re.compile(r"Page \d+ of \d+")
_DAY = re.compile(r"\d{2} [A-Z][a-z]{2} \d{2}")
_IN, _OUT, _BALANCE = "Money In (£)", "Money Out (£)", "Balance (£)"
_TABLE_END = "Transaction types"
_COLUMN = Decimal(15)
_MARGIN = Decimal(5)
# Type codes are short ("FPI", "DEB"); amounts start well right of them.
_TYPE_WIDTH = Decimal(30)


def _date(printed: str) -> date:
    return parse_date(printed, "%d %B %Y", "a date of the statement")


def _document_date(lines: list[list[Word]]) -> date | None:
    for line in lines:
        start = find(line, "Page")
        if start is not None and _PAGE.fullmatch(text(line[start:])):
            before = text(line[:start])
            return _date(before) if re.fullmatch(_DATE, before) else None
    return None


def _labelled(lines: list[list[Word]], label: str) -> str | None:
    for line in lines:
        words = after(line, label)
        if words:
            return words[0].text
    return None


@dataclass(frozen=True)
class _Header:
    period: tuple[date, date]
    product: str | None
    opening: Decimal
    closing: Decimal
    money_in: Decimal
    money_out: Decimal


def _header(lines: list[list[Word]]) -> _Header:
    period: tuple[date, date] | None = None
    product: str | None = None
    found: dict[str, tuple[Decimal, date, Decimal]] = {}
    for line in lines:
        printed = text(line)
        if period is None and (match := _PERIOD.fullmatch(printed)):
            period = _date(match["start"]), _date(match["end"])
            product = match["product"]
        elif match := _SUMMARY.fullmatch(printed):
            found[match["way"]] = (
                amount(match["money"], f"the statement's money {match['way'].lower()}"),
                _date(match["on"]),
                amount(match["balance"], f"the statement's balance on {match['on']}"),
            )
    if period is None:
        raise FileFormatError('the statement has no period ("… to …")')
    if "In" not in found or "Out" not in found:
        raise FileFormatError("the statement has no Money In and Money Out summary")
    money_in, opened, opening = found["In"]
    money_out, closed, closing = found["Out"]
    if (opened, closed) != period:
        raise FileFormatError("the statement's summary balances aren't dated at its period's ends")
    return _Header(period, product, opening, closing, money_in, money_out)


@dataclass
class _Columns:
    description_from: Decimal
    type_from: Decimal
    amounts_from: Decimal
    right_edges: dict[str, Decimal]
    currency: str | None


def _columns(line: list[Word]) -> _Columns | None:
    description = word_of(line, "Description")
    type_word = word_of(line, "Type")
    edges = {label: last_word_of(line, label) for label in (_IN, _OUT, _BALANCE)}
    if description is None or type_word is None or None in edges.values():
        return None
    return _Columns(
        description_from=description.x0 - _MARGIN,
        type_from=type_word.x0 - _MARGIN,
        amounts_from=type_word.x0 + _TYPE_WIDTH,
        right_edges={label: word.x1 for label, word in edges.items() if word is not None},
        currency="GBP" if "(£)" in text(line) else None,
    )


@dataclass
class _Pending:
    day: date
    day_text: str
    page: int
    description: list[str] = field(default_factory=list[str])
    types: list[str] = field(default_factory=list[str])
    amounts: list[tuple[str, Word]] = field(default_factory=list[tuple[str, Word]])


@dataclass
class _Table:
    currency: str | None = None
    rows: list[LayoutRow] = field(default_factory=list[LayoutRow])
    balances: list[PrintedBalance] = field(default_factory=list[PrintedBalance])
    unplaced: list[Word] = field(default_factory=list[Word])
    pending: _Pending | None = None

    def close(self) -> None:
        pending, self.pending = self.pending, None
        if pending is None:
            return
        position = len(self.rows)
        description = clean_description(pending.description)
        raw = {
            "Date": pending.day_text,
            "Description": description,
            "Type": " ".join(pending.types),
        }
        where = f"row {position + 1} (page {pending.page + 1})"
        split = split_amounts(pending.amounts, _BALANCE, where)
        for column, word in pending.amounts:
            raw[column] = word.text
        balance = (
            None if split.balance is None else amount(split.balance.text, f"{where}'s balance")
        )
        paid_in, paid_out = split.paid.get(_IN), split.paid.get(_OUT)
        self.rows.append(
            LayoutRow(
                row=ParsedRow(
                    position=position,
                    booking_date=pending.day,
                    occurred_at=None,
                    description=description,
                    quantity=(paid_in or Decimal(0)) - (paid_out or Decimal(0)),
                    fee=Decimal(0),
                    currency=self.currency,
                    balance=balance,
                    external_id=None,
                    bank_type=raw["Type"] or None,
                    raw=raw,
                ),
                page=pending.page,
                paid_in=paid_in,
                paid_out=paid_out,
                amounts=split.moves,
            )
        )
        if balance is not None:
            self.balances.append(PrintedBalance(len(self.rows), balance, pending.page))


def _line(line: list[Word], columns: _Columns, table: _Table) -> None:
    dates: list[Word] = []
    description: list[Word] = []
    types: list[Word] = []
    amounts: list[tuple[str, Word]] = []
    for word in line:
        if word.x1 > columns.amounts_from:
            column = nearest(word, columns.right_edges, _COLUMN)
            if column is None:
                table.unplaced.append(word)
            else:
                amounts.append((column, word))
        elif word.x0 >= columns.type_from:
            types.append(word)
        elif word.x0 >= columns.description_from:
            description.append(word)
        else:
            dates.append(word)
    if dates:
        if not _DAY.fullmatch(text(dates)):
            table.unplaced.extend(line)
            return
        table.close()
        day_text = text(dates)
        table.pending = _Pending(
            parse_date(day_text, "%d %b %y", f"the date on page {line[0].page + 1}"),
            day_text,
            line[0].page,
        )
    pending = table.pending
    if pending is None:
        table.unplaced.extend(line)
        return
    if description:
        pending.description.append(text(description))
    pending.types.extend(word.text for word in types)
    pending.amounts.extend(amounts)


def parse(lines: list[list[Word]]) -> LayoutResult:
    by_page = pages(lines)
    first = by_page.get(0, [])
    header = next((n for n, line in enumerate(first) if _columns(line) is not None), None)
    if header is None:
        raise FileFormatError("this is not a Lloyds statement: page 1 has no transaction table")
    above = first[:header]
    summary = _header(above)
    sort_code = _labelled(above, "Sort Code")
    account_number = _labelled(above, "Account Number")
    try:
        account = StatementAccount(
            sort_code=sort_code, account_number=account_number, account_type=summary.product
        )
    except ValueError as error:
        raise FileFormatError(f"the statement's account: {error}") from None
    table = _Table()
    for number in sorted(by_page):
        page_lines = by_page[number]
        start = next((n for n, line in enumerate(page_lines) if _columns(line) is not None), None)
        if start is None:
            continue
        columns = _columns(page_lines[start])
        assert columns is not None
        table.currency = table.currency or columns.currency
        for line in page_lines[start + 1 :]:
            if find(line, _TABLE_END) is not None:
                break
            _line(line, columns, table)
        table.close()
    return LayoutResult(
        rows=table.rows,
        period=summary.period,
        opening_balance=summary.opening,
        money_in=summary.money_in,
        money_out=summary.money_out,
        closing_balance=summary.closing,
        balances=table.balances,
        unplaced=table.unplaced,
        account=account,
        currency=table.currency,
        document_date=_document_date(first),
    )
