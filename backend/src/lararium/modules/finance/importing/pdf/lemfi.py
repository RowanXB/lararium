"""LemFi statements, the GBP account's and the savings account's (Flying Saucer). Pure: a
function of the words.

Both print a summary row of "GBP <amount>" figures under their labels (a long amount wraps
below its "GBP"), then one table: a date with its time of day on the line below, a
description, and the money out, money in and balance columns, each row printing a balance.
A payment's details ("<bank>, <account number>, <reference>" out, "Via <bank> *<last digits>"
in) are printed in a smaller font under its description, which may itself wrap. Every page
repeats the table's header, and a row's last lines may carry over to the next page. No row
prints a type code.

The account statement prints its account (IBAN, bank name and sort code) beside the mailing
block; the savings statement prints its account's name and interest rate on page 2, and
totals the interest it paid ("Interest accrued") apart from money in.

The summary's money in and out can each be a penny below the rows' sum on a long statement
(a sum cut down, not rounded, to the penny), while every printed balance follows from the
rows exactly; the layout says so (truncated_totals) and the closing balance is still checked
against the rows.
"""

import re
from dataclasses import dataclass, field
from datetime import date, time
from decimal import Decimal

from lararium.modules.finance.errors import FileFormatError
from lararium.modules.finance.importing.parsed import (
    Counterparty,
    ParsedRow,
    StatementAccount,
    StatementRate,
    clean_description,
    extract_fields,
)
from lararium.modules.finance.importing.pdf.layout import (
    LayoutResult,
    LayoutRow,
    PrintedBalance,
    after,
    amount,
    find,
    nearest,
    pages,
    parse_date,
    split_amounts,
    text,
    word_of,
)
from lararium.modules.finance.importing.pdf.words import Word

X_TOLERANCE = Decimal("1.5")

# What a row's Description and Details hold, as patterns whose named groups are typed
# fields (the catalog's CSV profile reads the CSV's columns with the same ones); the first
# that matches applies. A Details text that matches none refuses the statement; every
# Description matches. A payment out details the payee's bank, account number and the
# reference; a payment in, the payer's bank and the last digits of their account.
DESCRIPTION_PATTERNS = [r"(?:Sent money to|Received from) (?P<counterparty_name>.+)", r".*"]
DETAILS_PATTERNS = [
    r"(?P<counterparty_bank>[^,]+), (?P<counterparty_account_number>[0-9]+), (?P<reference>.+)",
    r"Via (?P<counterparty_bank>.+) \*(?P<counterparty_account_ending>[0-9]+)",
]
# The description of a row that pays the savings account's interest.
INTEREST_PAYOUT = "Savings Interest Payout"

_DATE = r"\d{1,2} [A-Z][a-z]+,? \d{4}"
# The account statement separates the period's days with "-", the savings one with "—".
_PERIOD = re.compile(rf"Statement period (?P<start>{_DATE}) [-\u2014] (?P<end>{_DATE})")
_GENERATED = re.compile(r"Generated on (?P<on>\d{1,2} [A-Z][a-z]{2}, \d{4})")
_TITLE = re.compile(r"(?:(?P<currency>[A-Z]{3}) Statement|Savings statement)")
_DAY = re.compile(r"\d{2} [A-Z][a-z]{2}, \d{4}")
_TIME = re.compile(r"(?P<hour>[01]\d|2[0-3]):(?P<minute>[0-5]\d)")
_MONEY = re.compile(r"(?P<currency>[A-Z]{3}) (?P<amount>\S+)")
_PAGE = re.compile(r"Page (?P<number>\d+) of (?P<total>\d+)$")
_HEADER_CURRENCY = re.compile(r"\((?P<currency>[A-Z]{3})\)")
_RATE = re.compile(
    r"(?P<aer>[0-9.]+)% AER \((?P<gross>[0-9.]+)% Gross, paid (?P<frequency>[a-z]+)\) "
    rf"from (?P<start>{_DATE}) to (?P<end>{_DATE})\."
)
_IBAN = re.compile(r"[A-Z]{2}[0-9]{2}[A-Z0-9]{11,30}")
# A UK IBAN: GB, check digits, the bank's four letters, the sort code and account number.
_UK_IBAN = re.compile(r"GB[0-9]{2}[A-Z]{4}(?P<sort_code>[0-9]{6})(?P<account_number>[0-9]{8})")

_SUMMARY_LABELS = {
    "Opening balance": "opening",
    "Money out": "money_out",
    "Money in": "money_in",
    "Interest accrued": "interest",
    "Closing balance": "closing",
}
_ACCOUNT_LABELS = {
    "Account number": "account_number",
    "Bank name": "bank",
    "Sort code": "sort_code",
}
_OUT, _IN, _BALANCE = "Money out", "Money in", "Balance"
_TABLE_ENDS = ("©", "LemFi is a", "LemFi is the")
_COLUMN = Decimal(15)
_MARGIN = Decimal(5)


def _long_date(printed: str, what: str) -> date:
    """ "01 October, 2026" or "1 October 2026"."""
    return parse_date(printed.replace(",", ""), "%d %B %Y", what)


def _first(lines: list[list[Word]], phrase: str) -> int | None:
    return next((n for n, line in enumerate(lines) if find(line, phrase) is not None), None)


@dataclass
class _Columns:
    description_from: Decimal
    amounts_from: Decimal
    right_edges: dict[str, Decimal]
    currency: str
    labels: dict[str, str]  # each column's printed label, as raw keys


def _columns(line: list[Word]) -> _Columns | None:
    description = word_of(line, "DESCRIPTION")
    starts = [find(line, label) for label in ("DATE", "MONEY OUT", "MONEY IN", "BALANCE")]
    if description is None or None in starts:
        return None
    _, out_at, in_at, balance_at = (start for start in starts if start is not None)
    # Each amount column's label ends with its currency: "MONEY OUT (GBP)".
    ends = {_OUT: out_at + 2, _IN: in_at + 2, _BALANCE: balance_at + 1}
    if any(end >= len(line) for end in ends.values()):
        return None
    printed = [_HEADER_CURRENCY.fullmatch(line[end].text) for end in ends.values()]
    codes = {match["currency"] for match in printed if match is not None}
    if None in printed or len(codes) != 1:
        raise FileFormatError("the transaction table's columns don't print one currency")
    currency = codes.pop()
    return _Columns(
        description_from=description.x0 - _MARGIN,
        amounts_from=line[out_at].x0 - _MARGIN,
        right_edges={column: line[end].x1 for column, end in ends.items()},
        currency=currency,
        labels={
            _OUT: f"Money out ({currency})",
            _IN: f"Money in ({currency})",
            _BALANCE: f"Balance ({currency})",
        },
    )


@dataclass(frozen=True)
class _Summary:
    opening: Decimal
    money_out: Decimal
    money_in: Decimal
    interest: Decimal | None
    closing: Decimal


def _summary(lines: list[list[Word]], currency: str) -> _Summary:
    """The figures under the summary's labels, each "<currency> <amount>" over its lines."""
    labelled = _first(lines, "Opening balance")
    end = _first(lines, "Your transactions")
    if labelled is None or end is None or end < labelled:
        raise FileFormatError("the statement has no summary of its balances")
    line = lines[labelled]
    starts = sorted(
        (word.x0, key)
        for label, key in _SUMMARY_LABELS.items()
        if (word := word_of(line, label)) is not None
    )
    labels_words = sum(
        len(label.split()) for label, _ in _SUMMARY_LABELS.items() if find(line, label) is not None
    )
    if labels_words != len(line):
        raise FileFormatError("the statement's summary has a label it doesn't know")
    printed: dict[str, list[str]] = {key: [] for _, key in starts}
    for below in lines[labelled + 1 : end]:
        for word in below:
            column = [key for x0, key in starts if x0 - _MARGIN <= word.x0]
            if not column:
                raise FileFormatError("the statement's summary has a figure under no label")
            printed[column[-1]].append(word.text)
    values: dict[str, Decimal] = {}
    for key, words in printed.items():
        match = _MONEY.fullmatch(" ".join(words))
        what = f"the statement's {key.replace('_', ' ')}"
        if match is None:
            raise FileFormatError(f"{what} is not a {currency} amount")
        if match["currency"] != currency:
            raise FileFormatError(f"{what} is in {match['currency']}, its rows in {currency}")
        values[key] = amount(match["amount"], what)
    missing = {"opening", "money_out", "money_in", "closing"} - set(values)
    if missing:
        raise FileFormatError("the statement's summary lacks its balances or money in and out")
    return _Summary(
        values["opening"],
        values["money_out"],
        values["money_in"],
        values.get("interest"),
        values["closing"],
    )


def _account_box(lines: list[list[Word]]) -> dict[str, str]:
    """The labels and values beside the mailing block ("Account number" over two lines)."""
    entries: list[tuple[list[str], str]] = []
    start = next((word for line in lines if (word := word_of(line, "Account")) is not None), None)
    if start is None:
        return {}
    value_x: Decimal | None = None
    for line in lines:
        box = [word for word in line if word.x0 >= start.x0 - _MARGIN]
        if not box:
            continue
        if value_x is None:
            value_x = box[1].x0 - _MARGIN if len(box) > 1 else None
            if value_x is None:
                raise FileFormatError("the statement's account has a label without a value")
        label = [word.text for word in box if word.x0 < value_x]
        value = [word.text for word in box if word.x0 >= value_x]
        if value:
            entries.append((label, " ".join(value)))
        elif entries:
            entries[-1][0].extend(label)
        else:
            raise FileFormatError("the statement's account has a label without a value")
    values: dict[str, str] = {}
    for label, value in entries:
        key = _ACCOUNT_LABELS.get(" ".join(label))
        if key is None or key in values:
            raise FileFormatError("the statement's account has a label it doesn't know")
        values[key] = value
    return values


def _identifiers(values: dict[str, str]) -> tuple[str | None, str | None, str | None]:
    """(sort code, account number, IBAN); a UK IBAN gives the account number and must hold
    the printed sort code."""
    sort_code = values.get("sort_code")
    printed = values.get("account_number")
    if printed is None or not _IBAN.fullmatch(printed):
        return sort_code, printed, None
    uk = _UK_IBAN.fullmatch(printed)
    if uk is None:
        return sort_code, None, printed
    if sort_code is not None and sort_code != uk["sort_code"]:
        raise FileFormatError("the statement's sort code isn't the one in its IBAN")
    return sort_code, uk["account_number"], printed


def _mailing(lines: list[list[Word]], x_limit: Decimal) -> tuple[str | None, str | None]:
    """(addressee, address): the first line left of x_limit, then the others, each without
    its trailing comma, joined with ", "."""
    block = [
        text(left).rstrip(",").strip()
        for line in lines
        if (left := [word for word in line if word.x0 < x_limit])
    ]
    if not block:
        return None, None
    return block[0], ", ".join(block[1:]) or None


def _value(lines: list[list[Word]], label: str) -> str | None:
    """The text right of a label that starts a line (page 2's account information), over
    the lines below it that start right of the label's column, up to the next label."""
    at = next((n for n, line in enumerate(lines) if find(line, label) == 0), None)
    if at is None:
        return None
    words = after(lines[at], label)
    if not words:
        raise FileFormatError(f"the statement's {label.lower()} is empty")
    value_x = words[0].x0 - _MARGIN
    parts = [text(words)]
    for line in lines[at + 1 :]:
        if line[0].x0 < value_x:
            break
        parts.append(text(line))
    return " ".join(parts)


def _rates(lines: list[list[Word]]) -> list[StatementRate]:
    printed = _value(lines, "Interest rate")
    if printed is None:
        return []
    match = _RATE.match(printed)
    if match is None:
        raise FileFormatError("the statement's interest rate doesn't read as AER and gross")
    what = "the statement's interest rate"
    try:
        return [
            StatementRate(
                kind="Interest rate",
                band_from=None,
                band_to=None,
                rate=amount(match["gross"], what),
                ear=amount(match["aer"], what),
                valid_from=_long_date(match["start"], what),
                valid_to=_long_date(match["end"], what),
                frequency=match["frequency"],
            )
        ]
    except ValueError as error:
        raise FileFormatError(f"{what}: {error}") from None


@dataclass
class _Pending:
    day: date
    day_text: str
    page: int
    size: Decimal  # the date's font size; the details print smaller
    time_text: str | None = None
    description: list[str] = field(default_factory=list[str])
    details: list[str] = field(default_factory=list[str])
    amounts: list[tuple[str, Word]] = field(default_factory=list[tuple[str, Word]])


def counterparty(description: str, details: str | None) -> Counterparty | None:
    """The other side, from a row's Description and Details; ValueError when the Details
    match none of DETAILS_PATTERNS."""
    values = extract_fields(DESCRIPTION_PATTERNS, description, "the description")
    if details is not None:
        values |= extract_fields(DETAILS_PATTERNS, details, "the details")
    found = Counterparty(
        name=values.get("counterparty_name"),
        account_number=values.get("counterparty_account_number"),
        account_ending=values.get("counterparty_account_ending"),
        reference=values.get("reference"),
        bank=values.get("counterparty_bank"),
    )
    return None if found == Counterparty() else found


@dataclass
class _Table:
    columns: _Columns
    rows: list[LayoutRow] = field(default_factory=list[LayoutRow])
    balances: list[PrintedBalance] = field(default_factory=list[PrintedBalance])
    unplaced: list[Word] = field(default_factory=list[Word])
    pending: _Pending | None = None

    def close(self) -> None:
        pending, self.pending = self.pending, None
        if pending is None:
            return
        position = len(self.rows)
        where = f"row {position + 1} (page {pending.page + 1})"
        description = clean_description(pending.description)
        details = clean_description(pending.details) or None
        labels = self.columns.labels
        when = (
            pending.day_text
            if pending.time_text is None
            else f"{pending.day_text} {pending.time_text}"
        )
        raw = {"Date": when, "Description": description, "Details": details or ""}
        split = split_amounts(pending.amounts, _BALANCE, where)
        for column, word in pending.amounts:
            raw[labels[column]] = word.text
        balance = (
            None if split.balance is None else amount(split.balance.text, f"{where}'s balance")
        )
        try:
            other = counterparty(description, details)
        except ValueError as error:
            raise FileFormatError(f"{where}: {error}") from None
        paid_in, paid_out = split.paid.get(_IN), split.paid.get(_OUT)
        moment = None
        if pending.time_text is not None and (match := _TIME.fullmatch(pending.time_text)):
            moment = time(int(match["hour"]), int(match["minute"]))
        self.rows.append(
            LayoutRow(
                row=ParsedRow(
                    position=position,
                    booking_date=pending.day,
                    occurred_at=None,
                    description=description,
                    quantity=(paid_in or Decimal(0)) - (paid_out or Decimal(0)),
                    fee=Decimal(0),
                    currency=self.columns.currency,
                    balance=balance,
                    external_id=None,
                    bank_type=None,
                    raw=raw,
                    counterparty=other,
                ),
                page=pending.page,
                paid_in=paid_in,
                paid_out=paid_out,
                amounts=split.moves,
                time_of_day=moment,
                interest=description == INTEREST_PAYOUT,
            )
        )
        if balance is not None:
            self.balances.append(PrintedBalance(len(self.rows), balance, pending.page))

    def line(self, line: list[Word]) -> None:
        columns = self.columns
        dates: list[Word] = []
        description: list[Word] = []
        amounts: list[tuple[str, Word]] = []
        for word in line:
            if word.x1 > columns.amounts_from:
                column = nearest(word, columns.right_edges, _COLUMN)
                if column is None:
                    self.unplaced.append(word)
                else:
                    amounts.append((column, word))
            elif word.x0 >= columns.description_from:
                description.append(word)
            else:
                dates.append(word)
        if dates:
            printed = text(dates)
            if _DAY.fullmatch(printed):
                self.close()
                self.pending = _Pending(
                    parse_date(printed, "%d %b, %Y", f"the date on page {line[0].page + 1}"),
                    printed,
                    line[0].page,
                    dates[0].size,
                )
            elif (
                _TIME.fullmatch(printed)
                and self.pending is not None
                and self.pending.time_text is None
            ):
                self.pending.time_text = printed
            else:
                self.unplaced.extend(dates)
        pending = self.pending
        if pending is None:
            self.unplaced.extend(description + [word for _, word in amounts])
            return
        pending.amounts.extend(amounts)
        small = [word for word in description if word.size < pending.size]
        large = [word for word in description if word.size >= pending.size]
        # The description comes first, the details after it; a description word below them
        # belongs to no field.
        if large and pending.details:
            self.unplaced.extend(large)
        elif large:
            pending.description.append(text(large))
        if small:
            pending.details.append(text(small))


def _table_ends(line: list[Word], columns: _Columns) -> bool:
    """The page's footer or the closing notice: in the date column, not a row's details."""
    return line[0].x0 < columns.description_from and text(line).startswith(_TABLE_ENDS)


def _check_pages(by_page: dict[int, list[list[Word]]]) -> None:
    """Every page's footer, its last line, ends "Page n of N", for all N pages: a statement
    missing one (a row's last lines, say) would still add up."""
    total = len(by_page)
    for number in sorted(by_page):
        printed = _PAGE.search(text(by_page[number][-1]))
        if printed is None or (int(printed["number"]), int(printed["total"])) != (
            number + 1,
            total,
        ):
            raise FileFormatError(
                f"page {number + 1} isn't numbered {number + 1} of {total}: "
                "the statement may be missing a page"
            )


def parse(lines: list[list[Word]]) -> LayoutResult:
    by_page = pages(lines)
    _check_pages(by_page)
    first = by_page.get(0, [])
    header = next((n for n, line in enumerate(first) if _columns(line) is not None), None)
    titled = next((match for line in first[:3] if (match := _TITLE.search(text(line)))), None)
    if header is None or titled is None:
        raise FileFormatError("this is not a LemFi statement: page 1 has no transaction table")
    columns = _columns(first[header])
    assert columns is not None
    currency = columns.currency
    if titled["currency"] is not None and titled["currency"] != currency:
        raise FileFormatError("the statement's title and its columns name different currencies")
    above = first[:header]
    period_at = _first(above, "Statement period")
    if period_at is None or (period := _PERIOD.fullmatch(text(above[period_at]))) is None:
        raise FileFormatError('the statement has no period ("Statement period … - …")')
    start = _long_date(period["start"], "the statement period's start")
    end = _long_date(period["end"], "the statement period's end")
    if start > end:
        raise FileFormatError("the statement period ends before it starts")
    generated_at = _first(above, "Generated on")
    generated = None if generated_at is None else _GENERATED.search(text(above[generated_at]))
    document_date = (
        None
        if generated is None
        else parse_date(generated["on"], "%d %b, %Y", "the statement's date")
    )
    summary = _summary(above, currency)

    # The mailing block and the account box, between the title and the period.
    block = above[(generated_at or 0) + 1 : period_at]
    box_word = next(
        (word for line in block if (word := word_of(line, "Account")) is not None), None
    )
    x_limit = box_word.x0 - _MARGIN if box_word is not None else Decimal("Infinity")
    addressee, address = _mailing(block, x_limit)
    values = (
        _account_box([[w for w in line if w.x0 >= x_limit] for line in block]) if box_word else {}
    )
    sort_code, account_number, iban = _identifiers(values)
    information = [line for number in sorted(by_page) if number > 0 for line in by_page[number]]
    try:
        account = StatementAccount(
            sort_code=sort_code,
            account_number=account_number,
            iban=iban,
            name=_value(information, "Name of account"),
            bank=values.get("bank"),
            addressee=addressee,
            address=address,
        )
    except ValueError as error:
        raise FileFormatError(f"the statement's account: {error}") from None

    table = _Table(columns)
    for number in sorted(by_page):
        page_lines = by_page[number]
        at = next((n for n, line in enumerate(page_lines) if _columns(line) is not None), None)
        if at is None:
            continue
        found = _columns(page_lines[at])
        assert found is not None
        if found.currency != currency:
            raise FileFormatError("the statement's pages name different currencies")
        table.columns = found
        for line in page_lines[at + 1 :]:
            if _table_ends(line, table.columns):
                break
            table.line(line)
    # Not at each page's end: a row's time and wrapped lines may carry over to the next page.
    table.close()
    return LayoutResult(
        rows=table.rows,
        period=(start, end),
        opening_balance=summary.opening,
        money_in=summary.money_in,
        money_out=summary.money_out,
        closing_balance=summary.closing,
        balances=table.balances,
        unplaced=table.unplaced,
        type_codes=False,
        interest=summary.interest,
        truncated_totals=True,
        account=account,
        currency=currency,
        document_date=document_date,
        rates=_rates(information),
    )
