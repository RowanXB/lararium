"""HSBC UK statements (OpenText output). Pure: a function of the statement's words.

"Paid out", "Paid in" and "Balance" are told apart only by x position, measured from the
table header. A date is printed on a day's first row only, and a balance after a day's
last row only (with a trailing "D" when overdrawn). Each row starts with a type code and
ends on the line with its amount. A statement continues on the next page between
"BALANCE CARRIED FORWARD" and "BALANCE BROUGHT FORWARD"; a first statement has no
brought-forward line.
"""

import re
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from lararium.modules.finance.errors import FileFormatError
from lararium.modules.finance.importing.parsed import (
    ParsedRow,
    StatementAccount,
    StatementRate,
    clean_description,
)
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

# The details column is set tight: its words are about 2 pt apart, often with no space.
X_TOLERANCE = Decimal("1.5")

_PERIOD = re.compile(r"(\d{1,2}) ([A-Z][a-z]+)(?: (\d{4}))? to (\d{1,2}) ([A-Z][a-z]+) (\d{4})\b")
_DAY = re.compile(r"\d{2} [A-Z][a-z]{2} \d{2}")
_BALANCE_LINES = ("BALANCE BROUGHT FORWARD", "BALANCE CARRIED FORWARD")
_SUMMARY = {
    "opening": "Opening Balance",
    "in": "Payments In",
    "out": "Payments Out",
    "closing": "Closing Balance",
}
_OVERDRAWN = "D"
# How far a word may sit from its column: amounts are right-aligned under the header.
_COLUMN = Decimal(15)
# The type code starts just left of the header's "Payment"; the details start further right.
_TYPE_FROM = Decimal(10)
_DETAILS_FROM = Decimal(15)
_RATE_ROW_GAP = Decimal(20)
_BANDS = ("upto", "over")


def _period(lines: list[list[Word]]) -> tuple[date, date]:
    for line in lines:
        match = _PERIOD.match(text(line))
        if match is None:
            continue
        start_day, start_month, start_year, end_day, end_month, end_year = match.groups()
        end = parse_date(f"{end_day} {end_month} {end_year}", "%d %B %Y", "the period's end")
        if start_year:
            year = int(start_year)
        else:
            # No year: the end's, or the year before when the start falls later in the year
            # (compared in a leap year, so 29 February is a day to compare).
            day = parse_date(f"{start_day} {start_month} 2000", "%d %B %Y", "the period's start")
            later = (day.month, day.day) > (end.month, end.day)
            year = end.year - 1 if later else end.year
        start = parse_date(f"{start_day} {start_month} {year}", "%d %B %Y", "the period's start")
        return start, end
    raise FileFormatError('the statement has no period ("… to …")')


def _summary_amount(words: list[Word], label: str) -> Decimal:
    """The amount right of the label; its digits can be printed as separate words."""
    parts = [word.text for word in words]
    overdrawn = bool(parts) and parts[-1] == _OVERDRAWN
    printed = "".join(parts[:-1] if overdrawn else parts)
    value = amount(printed, f"the statement's {label}")
    return Decimal(0) - value if overdrawn else value


def _summary(lines: list[list[Word]]) -> dict[str, Decimal]:
    found: dict[str, Decimal] = {}
    for key, label in _SUMMARY.items():
        for line in lines:
            words = after(line, label)
            if words:
                found[key] = _summary_amount(words, label)
                break
        else:
            raise FileFormatError(f"the statement has no {label}")
    return found


def _below(lines: list[list[Word]], index: int, start: Decimal, end: Decimal | None) -> str | None:
    """The words of the next line from x start (up to end), or None."""
    if index + 1 >= len(lines):
        return None
    words = [w for w in lines[index + 1] if w.x0 >= start and (end is None or w.x0 < end)]
    return text(words) or None


def _account(lines: list[list[Word]]) -> StatementAccount | None:
    values: dict[str, str | None] = {}
    margin = Decimal(5)
    for index, line in enumerate(lines):
        for key, label in (
            ("iban", "International Bank Account Number"),
            ("bic", "Bank Identifier Code"),
        ):
            word = word_of(line, label)
            if word is not None:
                values[key] = _below(lines, index, word.x0 - margin, None)
        name, sort_code, number, sheet = (
            word_of(line, label)
            for label in ("Account Name", "Sortcode", "Account Number", "Sheet Number")
        )
        if name is None or sort_code is None or number is None or sheet is None:
            continue
        values["name"] = _below(lines, index, name.x0 - margin, sort_code.x0 - margin)
        values["sort_code"] = _below(lines, index, sort_code.x0 - margin, number.x0 - margin)
        values["account_number"] = _below(lines, index, number.x0 - margin, sheet.x0 - margin)
    if not values:
        return None
    try:
        return StatementAccount(
            sort_code=values.get("sort_code"),
            account_number=values.get("account_number"),
            iban=values.get("iban"),
            bic=values.get("bic"),
            name=values.get("name"),
        )
    except ValueError as error:
        raise FileFormatError(f"the statement's account: {error}") from None


def _sheet(lines: list[list[Word]]) -> str | None:
    for index, line in enumerate(lines):
        sheet = word_of(line, "Sheet Number")
        if sheet is not None:
            return _below(lines, index, sheet.x0 - Decimal(5), None)
    return None


def _percent(printed: str, page: int) -> Decimal:
    if not printed.endswith("%"):
        raise FileFormatError(f"the rate table on page {page + 1} has a row that doesn't parse")
    return amount(printed.removesuffix("%"), f"the rate table on page {page + 1}")


def _rate(words: list[Word], kind: str, ear_column: bool) -> StatementRate | None:
    """One side of a rate row: "upto 50 0.00%" or "over 50 35.00%"."""
    page = words[0].page
    if text(words) == "Credit interest is not paid":
        return None
    if len(words) != 3 or words[0].text not in _BANDS:
        raise FileFormatError(f"the rate table on page {page + 1} has a row that doesn't parse")
    band = amount(words[1].text, f"the rate table on page {page + 1}")
    printed = _percent(words[2].text, page)
    upto = words[0].text == "upto"
    return StatementRate(
        kind=kind,
        band_from=None if upto else band,
        band_to=band if upto else None,
        # The credit column prints the AER; the overdraft column only the EAR.
        rate=None if ear_column else printed,
        ear=printed if ear_column else None,
    )


def _tier(words: list[Word]) -> bool:
    """Whether the words look like a tier: "upto 50 0.00%"."""
    return len(words) == 3 and words[0].text in _BANDS and words[2].text.endswith("%")


def _rates(lines: list[list[Word]]) -> list[StatementRate]:
    """The credit and overdraft interest tiers (outside the transaction table).

    The table starts on the line holding "Credit Interest Rates" and both column headers. It
    ends before the first line more than 20 pt below the one above it, or on the next page.
    Every line inside must parse as a tier or as "Credit interest is not paid", and a line
    shaped like a tier further down the page refuses the file instead of being dropped.
    """
    rates: list[StatementRate] = []
    for index, line in enumerate(lines):
        start = find(line, "Credit Interest Rates")
        if start is None:
            continue
        page = line[0].page
        overdraft = find(line[start + 3 :], "Interest Rates")
        if overdraft is None:
            raise FileFormatError(f"the rate table on page {page + 1} has no overdraft rates")
        split = line[start + 3 + overdraft].x0 - Decimal(5)
        previous = line
        credit: list[StatementRate] = []
        overdrawn: list[StatementRate] = []
        inside = True
        for row in lines[index + 1 :]:
            if row[0].page != page:
                break
            inside = inside and row[0].top - previous[0].top <= _RATE_ROW_GAP
            previous = row
            left = [w for w in row if w.x0 < split]
            right = [w for w in row if w.x0 >= split]
            if not inside:
                if _tier(left) or _tier(right):
                    raise FileFormatError(
                        f"the rate table on page {page + 1} has a tier below its end"
                    )
                continue
            if left and (rate := _rate(left, "Credit Interest Rates", False)) is not None:
                credit.append(rate)
            if right and (rate := _rate(right, "Overdraft Interest Rates", True)) is not None:
                overdrawn.append(rate)
        rates.extend(credit + overdrawn)
    return rates


@dataclass
class _Columns:
    type_from: Decimal
    details_from: Decimal
    amounts_from: Decimal
    right_edges: dict[str, Decimal]
    currency: str | None


@dataclass
class _Pending:
    day: date | None
    day_text: str | None
    type_code: str
    page: int
    details: list[str] = field(default_factory=list[str])
    amounts: list[tuple[str, Word]] = field(default_factory=list[tuple[str, Word]])
    balance: tuple[Decimal, str] | None = None


@dataclass
class _Table:
    rows: list[LayoutRow] = field(default_factory=list[LayoutRow])
    balances: list[PrintedBalance] = field(default_factory=list[PrintedBalance])
    unplaced: list[Word] = field(default_factory=list[Word])
    day: date | None = None
    day_text: str | None = None
    pending: _Pending | None = None
    currency: str | None = None

    def close(self) -> None:
        pending, self.pending = self.pending, None
        if pending is None:
            return
        position = len(self.rows)
        if pending.day is None:
            raise FileFormatError(f"row {position + 1} (page {pending.page + 1}) has no date")
        raw: dict[str, str] = {}
        if pending.day_text is not None:
            raw["Date"] = pending.day_text
        raw["Payment type"] = pending.type_code
        description = clean_description(pending.details)
        raw["Details"] = description
        split = split_amounts(pending.amounts, "Balance", f"row {position + 1}")
        for column, word in pending.amounts:
            raw[column] = word.text
        balance = None
        if pending.balance is not None:
            balance, raw["Balance"] = pending.balance
        paid_in, paid_out = split.paid.get("Paid in"), split.paid.get("Paid out")
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
                    bank_type=pending.type_code,
                    raw=raw,
                ),
                page=pending.page,
                paid_in=paid_in,
                paid_out=paid_out,
                amounts=split.moves,
            )
        )


def _columns(header: list[Word]) -> _Columns | None:
    payment = word_of(header, "Payment type and details")
    paid_out = word_of(header, "Paid out")
    out_edge = last_word_of(header, "Paid out")
    in_edge = last_word_of(header, "Paid in")
    balance = word_of(header, "Balance")
    if (
        payment is None
        or paid_out is None
        or out_edge is None
        or in_edge is None
        or balance is None
    ):
        return None
    return _Columns(
        type_from=payment.x0 - _TYPE_FROM,
        details_from=payment.x0 + _DETAILS_FROM,
        amounts_from=paid_out.x0 - _COLUMN,
        right_edges={"Paid out": out_edge.x1, "Paid in": in_edge.x1, "Balance": balance.x1},
        currency="GBP" if any(w.text == "£" for w in header) else None,
    )


def _amounts(words: list[Word], columns: _Columns, table: _Table) -> list[tuple[str, Word, bool]]:
    """Amount words with their column, and whether a balance is overdrawn."""
    placed: list[tuple[str, Word, bool]] = []
    for word in words:
        if word.text == _OVERDRAWN and placed and placed[-1][0] == "Balance" and not placed[-1][2]:
            column, balance, _ = placed[-1]
            placed[-1] = (column, balance, True)
            continue
        column = nearest(word, columns.right_edges, _COLUMN)
        if column is None:
            table.unplaced.append(word)
        else:
            placed.append((column, word, False))
    return placed


def _line(line: list[Word], columns: _Columns, table: _Table) -> None:
    dates = [w for w in line if w.x0 < columns.type_from and w.x1 <= columns.amounts_from]
    types = [
        w
        for w in line
        if columns.type_from <= w.x0 < columns.details_from and w.x1 <= columns.amounts_from
    ]
    details = [w for w in line if w.x0 >= columns.details_from and w.x1 <= columns.amounts_from]
    amounts = _amounts([w for w in line if w.x1 > columns.amounts_from], columns, table)
    day_text = text(dates)
    if dates and not _DAY.fullmatch(day_text):
        table.unplaced.extend(dates)
        dates = []
    details_text = text(details)
    if details_text in _BALANCE_LINES and not types:
        table.close()
        if dates:
            table.day = parse_date(day_text, "%d %b %y", f"the date on page {line[0].page + 1}")
            table.day_text = day_text
        for column, word, overdrawn in amounts:
            if column != "Balance":
                table.unplaced.append(word)
                continue
            value = amount(word.text, f"the balance on page {word.page + 1}")
            table.balances.append(
                PrintedBalance(
                    len(table.rows), Decimal(0) - value if overdrawn else value, word.page
                )
            )
        return
    if types:
        table.close()
        if dates:
            table.day = parse_date(day_text, "%d %b %y", f"the date on page {line[0].page + 1}")
            table.day_text = day_text
        table.unplaced.extend(types[1:])
        table.pending = _Pending(table.day, table.day_text, types[0].text, line[0].page)
    elif dates:
        table.unplaced.extend(dates)
    pending = table.pending
    if pending is None:
        table.unplaced.extend(details)
        table.unplaced.extend(word for _, word, _ in amounts)
        return
    if details_text:
        pending.details.append(details_text)
    where = f"the row on page {line[0].page + 1}"
    split = split_amounts([(column, word) for column, word, _ in amounts], "Balance", where)
    balance: tuple[Decimal, str] | None = None
    for column, word, overdrawn in amounts:
        if word is split.balance:
            value = amount(word.text, f"the balance on page {word.page + 1}")
            printed = f"{word.text} {_OVERDRAWN}" if overdrawn else word.text
            balance = (Decimal(0) - value if overdrawn else value, printed)
        elif column != "Balance":
            pending.amounts.append((column, word))
    if not pending.amounts:
        if balance is not None:
            table.unplaced.extend(word for column, word, _ in amounts if column == "Balance")
        return
    pending.balance = balance
    table.close()
    if balance is not None:
        table.balances.append(PrintedBalance(len(table.rows), balance[0], line[0].page))


def _read_page(page_lines: list[list[Word]], table: _Table) -> None:
    header = next((n for n, line in enumerate(page_lines) if _columns(line) is not None), None)
    if header is None:
        return  # no transaction table on this page
    columns = _columns(page_lines[header])
    assert columns is not None
    table.currency = table.currency or columns.currency
    for line in page_lines[header + 1 :]:
        _line(line, columns, table)
        if find(line, "BALANCE CARRIED FORWARD") is not None:
            table.close()
            return
    raise FileFormatError(
        f"the transaction table on page {page_lines[header][0].page + 1} has no "
        "BALANCE CARRIED FORWARD line"
    )


def parse(lines: list[list[Word]]) -> LayoutResult:
    by_page = pages(lines)
    first = by_page.get(0, [])
    header = next((n for n, line in enumerate(first) if _columns(line) is not None), None)
    if header is None:
        raise FileFormatError("this is not an HSBC UK statement: page 1 has no transaction table")
    above = first[:header]
    summary = _summary(above)
    table = _Table()
    for number in sorted(by_page):
        _read_page(by_page[number], table)
    return LayoutResult(
        rows=table.rows,
        period=_period(above),
        opening_balance=summary["opening"],
        money_in=summary["in"],
        money_out=summary["out"],
        closing_balance=summary["closing"],
        balances=table.balances,
        unplaced=table.unplaced,
        account=_account(above),
        currency=table.currency,
        statement_reference=_sheet(above),
        rates=_rates(lines),
    )
