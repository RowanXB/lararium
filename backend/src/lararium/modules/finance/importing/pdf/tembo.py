"""Tembo statements (PDFium). Pure: a function of the words.

Page 1 prints the mailing block under "Personal details", an "Account summary" of two
label columns, the period ("Transactions: … - …") and the table: Date, Transaction type,
Amount and Balance, the amounts right-aligned under their labels and every one in "£".
The statement terms that follow list the transaction types.

There is no description, no opening balance and no money in or out total: the Plan value,
valued on the Valuation date, is the closing balance, which the rows' balances must reach.
The type gives a row's direction; an Interest Adjustment goes either way, as printed.

The printed period runs from the first row's date to the last's, not to the Valuation date
(two statements valued a week apart print the same one): a row missing at either end shows
as a period that doesn't match, and a missing page as a gap in the "n / N" every page
prints. The statement is made on demand and may stop partway through its last day, so it
lists in full only the days between its first and last.
"""

import re
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from itertools import pairwise

from lararium.modules.finance.errors import FileFormatError
from lararium.modules.finance.importing.parsed import ParsedRow, StatementAccount
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

_DAY = re.compile(r"\d{2}/\d{2}/\d{4}")
_PERIOD = re.compile(rf"Transactions: (?P<start>{_DAY.pattern}) - (?P<end>{_DAY.pattern})")
_PAGE = re.compile(r"\d+ / \d+")
_HEADER = "Date Transaction type Amount Balance"
_AMOUNT, _BALANCE = "Amount", "Balance"
# Where the table, and the page's content, ends.
_FOOTER = "Tembo, Tembo Savings,"
_TERMS = "Statement terms"
# The account summary's right-hand labels start past this; the left one's values end
# before it, as does the mailing block.
_RIGHT_COLUMN = Decimal(300)
_ACCOUNT_NO, _OPENED, _ACCOUNT_TYPE = "Account no:", "Account open date:", "Account type:"
_AVAILABLE, _PLAN_VALUE, _VALUATION = "Available to withdraw*:", "Plan value:", "Valuation date:"
_LABELS = (_ACCOUNT_NO, _OPENED, _ACCOUNT_TYPE, _AVAILABLE, _PLAN_VALUE, _VALUATION)
# The statement terms' transaction types, by direction.
_IN = frozenset({"Deposit", "Transfer In", "Interest"})
_OUT = frozenset({"Withdrawal", "Transfer Out"})
_EITHER = frozenset({"Interest Adjustment"})
_COLUMN = Decimal(15)
_MARGIN = Decimal(5)


def _date(printed: str, what: str) -> date:
    return parse_date(printed, "%d/%m/%Y", what)


def _pounds(printed: str, what: str) -> Decimal:
    """An amount in "£", as every amount on the statement is: one without it would take
    the statement's currency on trust."""
    if printed.lstrip("+-")[:1] != "£":
        raise FileFormatError(f"{what} isn't in pounds: {printed!r}")
    return amount(printed, what)


def _summary(lines: list[list[Word]]) -> dict[str, str]:
    """The account summary's values by label, from either column of each line; a label
    printed with no value is not stated."""
    found: dict[str, str] = {}
    seen: set[str] = set()
    for line in lines:
        left = [word for word in line if word.x0 < _RIGHT_COLUMN]
        right = [word for word in line if word.x0 >= _RIGHT_COLUMN]
        for half in (left, right):
            for label in _LABELS:
                words = after(half, label)
                if words is None or find(half, label) != 0:
                    continue
                if label in seen:
                    raise FileFormatError(f'the statement prints "{label}" twice')
                seen.add(label)
                if words:
                    found[label] = text(words)
    return found


def _between(lines: list[list[Word]], first: str, last: str) -> list[list[Word]]:
    """The lines after the one that is `first` and before the one that is `last`."""
    starts = [n for n, line in enumerate(lines) if text(line) == first]
    ends = [n for n, line in enumerate(lines) if text(line) == last]
    if not starts or not ends or ends[0] < starts[0]:
        return []
    return lines[starts[0] + 1 : ends[0]]


def _mailing(lines: list[list[Word]]) -> tuple[str | None, str | None]:
    """(addressee, address) from the lines between "Personal details" and "Account
    summary": the first line is the addressee, the rest (joined with ", ") the address."""
    block = [
        text(words)
        for line in _between(lines, "Personal details", "Account summary")
        if (words := [word for word in line if word.x0 < _RIGHT_COLUMN])
    ]
    if not block:
        return None, None
    return block[0], ", ".join(block[1:]) or None


@dataclass(frozen=True)
class _Columns:
    type_from: Decimal
    amounts_from: Decimal
    right_edges: dict[str, Decimal]


def _columns(line: list[Word]) -> _Columns | None:
    if text(line) != _HEADER:
        return None
    type_word = word_of(line, "Transaction type")
    type_end = last_word_of(line, "Transaction type")
    amount_word = word_of(line, _AMOUNT)
    balance_word = word_of(line, _BALANCE)
    assert type_word and type_end and amount_word and balance_word
    return _Columns(
        type_from=type_word.x0 - _MARGIN,
        # Halfway between the type's label and the amount's: types are short words read
        # from the left, amounts right-aligned.
        amounts_from=(type_end.x1 + amount_word.x0) / 2,
        right_edges={_AMOUNT: amount_word.x1, _BALANCE: balance_word.x1},
    )


@dataclass
class _Entry:
    """A row as read, before the rows are put in date order."""

    day: date
    day_text: str
    page: int
    types: list[str] = field(default_factory=list[str])
    amounts: list[tuple[str, Word]] = field(default_factory=list[tuple[str, Word]])


@dataclass
class _Table:
    entries: list[_Entry] = field(default_factory=list[_Entry])
    unplaced: list[Word] = field(default_factory=list[Word])
    pending: _Entry | None = None

    def close(self) -> None:
        if self.pending is not None:
            self.entries.append(self.pending)
            self.pending = None

    def line(self, line: list[Word], columns: _Columns) -> None:
        dates: list[Word] = []
        types: list[Word] = []
        amounts: list[tuple[str, Word]] = []
        for word in line:
            if word.x0 >= columns.amounts_from:
                column = nearest(word, columns.right_edges, _COLUMN)
                if column is None:
                    self.unplaced.append(word)
                else:
                    amounts.append((column, word))
            elif word.x0 >= columns.type_from:
                types.append(word)
            else:
                dates.append(word)
        if dates:
            if not _DAY.fullmatch(text(dates)):
                self.unplaced.extend(line)
                return
            self.close()
            day_text = text(dates)
            self.pending = _Entry(
                _date(day_text, f"the date on page {line[0].page + 1}"), day_text, line[0].page
            )
        pending = self.pending
        if pending is None:
            self.unplaced.extend(line)
            return
        if types:
            pending.types.append(text(types))
        pending.amounts.extend(amounts)


def _quantity(kind: str, printed: Decimal, where: str) -> Decimal:
    if kind in _IN:
        if printed < 0:
            raise FileFormatError(f"{where} is a {kind} printed as money out")
        return printed
    if kind in _OUT:
        return -abs(printed)
    if kind in _EITHER:
        return printed
    raise FileFormatError(f"{where} has a transaction type the statement terms don't list")


def _rows(entries: list[_Entry]) -> tuple[list[LayoutRow], list[PrintedBalance]]:
    """The rows oldest first, as the balance check reads them: a statement printed newest
    first (its dates never rise) is turned round."""
    days = [entry.day for entry in entries]
    rising = all(a <= b for a, b in pairwise(days))
    falling = all(a >= b for a, b in pairwise(days))
    if not rising and not falling:
        raise FileFormatError("the statement's rows aren't in date order")
    if not rising:
        entries = entries[::-1]
    rows: list[LayoutRow] = []
    balances: list[PrintedBalance] = []
    for position, entry in enumerate(entries):
        where = f"row {position + 1} (page {entry.page + 1})"
        kind = " ".join(" ".join(entry.types).split())
        for column, word in entry.amounts:
            _pounds(word.text, f"{where}'s {column.lower()}")
        split = split_amounts(entry.amounts, _BALANCE, where)
        printed = split.paid.get(_AMOUNT)
        quantity = Decimal(0)
        if printed is not None and kind:
            quantity = _quantity(kind, printed, where)
        balance = (
            None if split.balance is None else amount(split.balance.text, f"{where}'s balance")
        )
        raw = {"Date": entry.day_text, "Transaction type": kind}
        for column, word in entry.amounts:
            raw[column] = word.text
        rows.append(
            LayoutRow(
                row=ParsedRow(
                    position=position,
                    booking_date=entry.day,
                    occurred_at=None,
                    # The type is the only text a row prints.
                    description=kind,
                    quantity=quantity,
                    fee=Decimal(0),
                    currency="GBP",
                    balance=balance,
                    external_id=None,
                    bank_type=kind or None,
                    raw=raw,
                ),
                page=entry.page,
                paid_in=quantity if quantity >= 0 else None,
                paid_out=-quantity if quantity < 0 else None,
                amounts=split.moves,
            )
        )
        if balance is not None:
            balances.append(PrintedBalance(len(rows), balance, entry.page))
    return rows, balances


def _ends_table(line: list[Word]) -> bool:
    printed = text(line)
    return printed.startswith(_FOOTER) or printed == _TERMS or bool(_PAGE.fullmatch(printed))


def _page_numbers(by_page: dict[int, list[list[Word]]]) -> None:
    """Every page prints "n / N": a page left out of the file shows as a gap or a count
    that differs from the file's."""
    count = max(by_page) + 1
    for number in range(count):
        printed = [text(line) for line in by_page.get(number, []) if _PAGE.fullmatch(text(line))]
        if printed != [f"{number + 1} / {count}"]:
            raise FileFormatError(
                f"page {number + 1} of {count} doesn't print that page number: "
                "the file may be missing a page"
            )


def parse(lines: list[list[Word]]) -> LayoutResult:
    by_page = pages(lines)
    first = by_page.get(0, [])
    header = next((n for n, line in enumerate(first) if _columns(line) is not None), None)
    if header is None:
        raise FileFormatError("this is not a Tembo statement: page 1 has no transaction table")
    _page_numbers(by_page)
    above = first[:header]
    period = next(
        (
            (_date(m["start"], "the period's start"), _date(m["end"], "the period's end"))
            for line in above
            if (m := _PERIOD.fullmatch(text(line)))
        ),
        None,
    )
    if period is None:
        raise FileFormatError('the statement has no period ("Transactions: … - …")')
    summary = _summary(above)
    if _PLAN_VALUE not in summary:
        raise FileFormatError('the statement has no "Plan value"')
    closing = _pounds(summary[_PLAN_VALUE], "the statement's plan value")
    available = summary.get(_AVAILABLE)
    valuation = summary.get(_VALUATION)
    opened = summary.get(_OPENED)
    valued_on = None if valuation is None else _date(valuation, "the valuation date")
    addressee, address = _mailing(above)
    try:
        account = StatementAccount(
            account_reference=summary.get(_ACCOUNT_NO),
            account_type=summary.get(_ACCOUNT_TYPE),
            addressee=addressee,
            address=address,
        )
    except ValueError as error:
        raise FileFormatError(f"the statement's account: {error}") from None

    table = _Table()
    terms = False
    for number in sorted(by_page):
        page_lines = by_page[number]
        start = next((n for n, line in enumerate(page_lines) if _columns(line) is not None), None)
        terms = terms or any(text(line) == _TERMS for line in page_lines)
        if start is None and not terms:
            # Before the terms every page holds the table: one whose table can't be read
            # (an image, say) would leave its rows out unseen.
            raise FileFormatError(
                f"page {number + 1} has no transaction table and is not the statement terms"
            )
        if start is None:
            # A page without the table's header holds no rows: a dated line there would
            # be one, read by nothing.
            for line in page_lines:
                if _DAY.fullmatch(line[0].text):
                    table.unplaced.extend(line)
            continue
        columns = _columns(page_lines[start])
        assert columns is not None
        before = len(table.entries)
        for line in page_lines[start + 1 :]:
            if _ends_table(line):
                break
            table.line(line, columns)
        table.close()
        if len(table.entries) == before:
            # Tembo prints a table only over rows: one with none read has them unseen.
            raise FileFormatError(f"page {number + 1}'s transaction table has no row it can read")
    rows, balances = _rows(table.entries)
    if rows and (rows[0].row.booking_date, rows[-1].row.booking_date) != period:
        raise FileFormatError(
            "the statement's period doesn't run from its first row's date to its last's: "
            "a row may be missing"
        )
    return LayoutResult(
        rows=rows,
        period=period,
        opening_balance=None,
        money_in=None,
        money_out=None,
        closing_balance=closing,
        balances=balances,
        unplaced=table.unplaced,
        account=account,
        currency="GBP",
        document_date=valued_on,
        available_balance=(
            None if available is None else _pounds(available, "the amount available to withdraw")
        ),
        available_balance_date=valued_on if available is not None else None,
        account_opened_on=None if opened is None else _date(opened, "the account open date"),
        period_complete=False,
    )
