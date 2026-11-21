"""Revolut statements, en-US: the current account's and the savings account's. Pure:
functions of the words.

The current account's statement has a section per currency. Each page of a section is
headed "<CCY> Statement"; the section's first page has the mailing block, the account's
details and a balance summary (one row per product, then their total). Its tables follow,
each under a title: "Pending from … to …" and "Reverted from … to …" list payments left out
of the summary, "Account transactions from … to …" the completed ones and the period. A row
is a dated line with one amount, under "Money out" or "Money in" (left-aligned), and a
balance (right-aligned) for a completed one, followed by lines of details: "Reference:",
"To:" or "From:" (the other side), "Card:" (the masked card) and, for a payment in another
currency, the rates and the amount in that currency. No row prints a type code: a card
payment is told by its card. Only one section may show any money; the others must be empty.

The savings statement has one table, a row per line under "Money out" (left-aligned),
"Money in" and "Balance" (right-aligned), its description being its kind; the summary totals
each kind.
"""

import re
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from lararium.modules.finance.errors import FileFormatError
from lararium.modules.finance.importing.parsed import (
    Counterparty,
    ParsedRow,
    SkippedRow,
    StatementAccount,
    clean_description,
)
from lararium.modules.finance.importing.pdf.layout import (
    LayoutResult,
    LayoutRow,
    PrintedBalance,
    after,
    amount,
    find,
    pages,
    parse_date,
    text,
    word_of,
)
from lararium.modules.finance.importing.pdf.words import Word

X_TOLERANCE = Decimal("1.5")

# Currency symbols as en-US statements print them (JavaScript's Intl, en-US).
_SYMBOLS = {
    "£": "GBP",
    "$": "USD",
    "€": "EUR",
    "CA$": "CAD",
    "A$": "AUD",
    "NZ$": "NZD",
    "HK$": "HKD",
    "¥": "JPY",
    "CN¥": "CNY",
    "₹": "INR",
    "₩": "KRW",
    "₪": "ILS",
    "MX$": "MXN",
    "R$": "BRL",
}
# Currencies with no minor unit, whose amounts en-US prints whole ("¥1,000").
_WHOLE = {"JPY", "KRW"}
_MONEY = re.compile(r"(?P<sign>-?)(?P<symbol>[^\d\s,.-]+)(?P<number>\d[\d,]*(?:\.\d+)?)")
_SHORT = r"[A-Z][a-z]{2} \d{1,2}, \d{4}"
_LONG = r"[A-Z][a-z]+ \d{1,2}, \d{4}"
_DAY = re.compile(_SHORT)
_GENERATED = re.compile(rf"Generated on (?P<day>{_SHORT})")
_TITLE = re.compile(r"(?P<code>[A-Z]{3}) Statement")
_SECTION = re.compile(
    rf"(?P<kind>Pending|Account transactions|Reverted) from (?P<start>{_LONG}) to (?P<end>{_LONG})"
)
_RATE = re.compile(
    r"Revolut Rate (?P<base>\S+) = (?P<quote>\S+) \(ECB rate\* (?P<ecb_base>\S+) = "
    r"(?P<ecb_quote>\S+)\)"
)
# The other side as printed for a transfer: its name and its account number.
_PARTY = re.compile(r"(?P<name>.+), (?P<number>\d{8})")
_BANK = "Revolut Bank UK Ltd"
_FOOTER = "Report lost or stolen card"
_COMPLETED = "Account transactions"
_OUT, _IN, _BALANCE = "Money out", "Money in", "Balance"
_MARGIN = Decimal(5)
_ALIGN = Decimal(3)
# The right column of the mailing block's lines (the account's details) starts past this.
_RIGHT = Decimal(300)


def _is_money(printed: str) -> bool:
    """Whether a word is an amount: with a decimal point, or whole in a currency printed so."""
    match = _MONEY.fullmatch(printed)
    return match is not None and ("." in match["number"] or _SYMBOLS.get(match["symbol"]) in _WHOLE)


def _money(printed: str, what: str) -> tuple[str, Decimal]:
    """(currency, amount) of an amount as printed ("£1,234.50", "-£5.00", "$96.00", "¥1,000")."""
    match = _MONEY.fullmatch(printed)
    if match is None:
        raise FileFormatError(f"{what} is not an amount: {printed!r}")
    code = _SYMBOLS.get(match["symbol"])
    if code is None:
        raise FileFormatError(f"{what} is in a currency this layout doesn't know: {printed!r}")
    if "." not in match["number"] and code not in _WHOLE:
        raise FileFormatError(f"{what} is not an amount: {printed!r}")
    return code, amount(match["sign"] + match["number"], what)


def _in(currency: str, printed: str, what: str) -> Decimal:
    """An amount that must be in the statement's currency."""
    code, value = _money(printed, what)
    if code != currency:
        raise FileFormatError(f"{what} is in {code}, not the statement's {currency}")
    return value


def _short_date(printed: str, what: str) -> date:
    return parse_date(printed, "%b %d, %Y", what)


def _long_date(printed: str, what: str) -> date:
    return parse_date(printed, "%B %d, %Y", what)


def _generated(lines: list[list[Word]]) -> date | None:
    for line in lines:
        if match := _GENERATED.fullmatch(text(line)):
            return _short_date(match["day"], "the statement's date")
    return None


@dataclass(frozen=True)
class _Columns:
    description: Decimal  # x0
    paid_out: Decimal  # x0, left-aligned
    paid_in: Decimal
    balance: Decimal | None  # x1, right-aligned; None: the table prints no balance


def _table_header(line: list[Word], *, right_aligned_in: bool) -> _Columns | None:
    """The columns under a table header ("Date"/"Start date", "Description", "Money out",
    "Money in" and maybe "Balance"), or None for another line."""
    description = word_of(line, "Description")
    out_start = find(line, _OUT)
    in_start = find(line, _IN)
    if description is None or out_start is None or in_start is None:
        return None
    paid_in = line[in_start + 1].x1 if right_aligned_in else line[in_start].x0
    balance = word_of(line, _BALANCE)
    return _Columns(
        description=description.x0,
        paid_out=line[out_start].x0,
        paid_in=paid_in,
        balance=None if balance is None else balance.x1,
    )


@dataclass
class _Split:
    """A table line's words by where they stand."""

    dates: list[Word] = field(default_factory=list[Word])
    description: list[Word] = field(default_factory=list[Word])
    amounts: list[tuple[str, Word]] = field(default_factory=list[tuple[str, Word]])
    unplaced: list[Word] = field(default_factory=list[Word])


def _split(line: list[Word], columns: _Columns, *, right_aligned_in: bool) -> _Split:
    split = _Split()
    for word in line:
        if word.x0 < columns.description - _MARGIN:
            split.dates.append(word)
        elif word.x0 < columns.paid_out - _MARGIN:
            split.description.append(word)
        elif abs(word.x0 - columns.paid_out) <= _ALIGN:
            split.amounts.append((_OUT, word))
        elif (
            abs(word.x1 - columns.paid_in) <= _ALIGN
            if right_aligned_in
            else abs(word.x0 - columns.paid_in) <= _ALIGN
        ):
            split.amounts.append((_IN, word))
        elif columns.balance is not None and abs(word.x1 - columns.balance) <= _ALIGN:
            split.amounts.append((_BALANCE, word))
        else:
            split.unplaced.append(word)
    return split


def _statement_account(
    intro: list[list[Word]], labels: tuple[str, ...], account_type: str | None
) -> StatementAccount:
    """The account from the lines above the summary: the mailing block on the left (its
    first line the addressee), the labelled details on the right or on lines of their own."""
    details: dict[str, str] = {}
    block: list[str] = []
    for line in intro:
        for label in (*labels, "IBAN", "BIC"):
            words = after(line, label)
            if words:
                details[label] = words[0].text
        left = [word for word in line if word.x0 < _RIGHT]
        if left and not any(find(left, label) is not None for label in (*labels, "IBAN", "BIC")):
            block.append(text(left))
    try:
        return StatementAccount(
            sort_code=details.get(labels[0]),
            account_number=details.get(labels[1]),
            iban=details.get("IBAN"),
            bic=details.get("BIC"),
            account_type=account_type,
            addressee=block[0] if block else None,
            address=", ".join(block[1:]) or None,
        )
    except ValueError as error:
        raise FileFormatError(f"the statement's account: {error}") from None


# The current account ------------------------------------------------------------------


@dataclass
class _Row:
    kind: str  # the table's title: Pending, Account transactions or Reverted
    day: date
    day_text: str
    page: int
    description: list[str]
    amounts: list[tuple[str, Word]]
    details: dict[str, str] = field(default_factory=dict[str, str])
    rate: re.Match[str] | None = None
    original: Word | None = None  # the amount in the other currency, and its column
    original_column: str | None = None


@dataclass
class _Section:
    currency: str
    first_page: int
    intro: list[list[Word]] = field(default_factory=list[list[Word]])
    summary: list[tuple[str, list[Decimal]]] = field(
        default_factory=list[tuple[str, list[Decimal]]]
    )
    period: tuple[date, date] | None = None
    rows: list[_Row] = field(default_factory=list[_Row])
    unplaced: list[Word] = field(default_factory=list[Word])


def _page_currency(page: list[list[Word]]) -> str:
    for line in page[:3]:
        if match := _TITLE.fullmatch(text(line)):
            return match["code"]
    raise FileFormatError(
        f'page {page[0][0].page + 1} has no "<currency> Statement" title: this is not a '
        "Revolut current account statement"
    )


def _content(page: list[list[Word]]) -> list[list[Word]]:
    """The page's lines below its letterhead and above its footer."""
    start = next((n + 1 for n, line in enumerate(page) if text(line) == _BANK), None)
    if start is None:
        raise FileFormatError(f"page {page[0][0].page + 1} has no letterhead")
    end = next((n for n, line in enumerate(page) if find(line, _FOOTER) is not None), len(page))
    return page[start:end]


def _summary_row(line: list[Word], section: _Section) -> bool:
    """A balance summary row: a label and four amounts (opening, out, in, closing)."""
    if len(line) < 5 or _is_money(line[-5].text):
        return False
    printed = [word.text for word in line[-4:]]
    if not all(_is_money(value) for value in printed):
        return False
    what = f"the balance summary's {text(line[:-4])!r}"
    section.summary.append(
        (text(line[:-4]), [_in(section.currency, value, what) for value in printed])
    )
    return True


def _detail(row: _Row, split: _Split, line: list[Word]) -> bool:
    """Takes a row's detail line; False when it is none the layout knows."""
    printed = text(split.description)
    if split.dates or split.unplaced or not split.description:
        return False
    if match := _RATE.fullmatch(printed):
        if row.rate is not None or len(split.amounts) != 1:
            return False
        column, word = split.amounts[0]
        if column == _BALANCE:
            return False
        row.rate, row.original, row.original_column = match, word, column
        row.details["Revolut Rate"] = text(line)
        return True
    label = split.description[0].text
    if split.amounts or label not in ("Reference:", "To:", "From:", "Card:"):
        return False
    key = label.removesuffix(":")
    if key in row.details or ({"To", "From"} & row.details.keys() and key in ("To", "From")):
        return False
    value = text(split.description[1:])
    if not value:
        return False
    row.details[key] = value
    return True


@dataclass
class _Reader:
    """Reads a section's lines, over all its pages, in order."""

    section: _Section
    kind: str | None = None  # the title of the table being read
    in_summary: bool = False
    columns: _Columns | None = None
    row: _Row | None = None

    def line(self, line: list[Word]) -> None:
        section = self.section
        printed = text(line)
        if self.kind is None and not self.in_summary:
            if printed == "Balance summary":
                self.in_summary = True
            else:
                section.intro.append(line)
            return
        if match := _SECTION.fullmatch(printed):
            self.kind, self.columns, self.row = match["kind"], None, None
            if self.kind == _COMPLETED:
                if section.period is not None:
                    raise FileFormatError("the statement lists its transactions twice")
                section.period = (
                    _long_date(match["start"], "the period's start"),
                    _long_date(match["end"], "the period's end"),
                )
            return
        if self.kind is None:
            # The summary, among its column labels and the note under it.
            _summary_row(line, section)
            return
        header = _table_header(line, right_aligned_in=False)
        if header is not None:  # the table's header, repeated on each page
            self.columns, self.row = header, None
            return
        if self.columns is None:
            section.unplaced.extend(line)
            return
        split = _split(line, self.columns, right_aligned_in=False)
        if not split.dates:
            if self.row is None or not _detail(self.row, split, line):
                section.unplaced.extend(line)
            return
        day_text = text(split.dates)
        if not _DAY.fullmatch(day_text) or split.unplaced:
            section.unplaced.extend(line)
            self.row = None
            return
        self.row = _Row(
            kind=self.kind,
            day=_short_date(day_text, f"a date on page {line[0].page + 1}"),
            day_text=day_text,
            page=line[0].page,
            description=[text(split.description)],
            amounts=split.amounts,
        )
        section.rows.append(self.row)


def _sections(lines: list[list[Word]]) -> list[_Section]:
    """The statement's currency sections, each read over its pages: a section starts on a
    page with a balance summary, and its later pages carry its currency."""
    readers: list[_Reader] = []
    for number, page in sorted(pages(lines).items()):
        currency = _page_currency(page)
        content = _content(page)
        if any(text(line) == "Balance summary" for line in content):
            readers.append(_Reader(_Section(currency, number)))
        elif not readers or readers[-1].section.currency != currency:
            raise FileFormatError(f"page {number + 1} starts a section without a balance summary")
        for line in content:
            readers[-1].line(line)
    return [reader.section for reader in readers]


@dataclass(frozen=True)
class _Built:
    row: ParsedRow
    paid_in: Decimal | None
    paid_out: Decimal | None
    moves: int
    balance: Decimal | None


def _fx(
    row: _Row, currency: str, where: str, column: str | None
) -> tuple[str, Decimal, Decimal, Decimal]:
    """(original currency, original amount in ledger sign, the bank's rate, the reference
    rate) from a row's rate line; each rate is per one unit of the statement's currency."""
    assert row.rate is not None and row.original is not None
    rates: list[tuple[str, Decimal]] = []
    for base, quote in (("base", "quote"), ("ecb_base", "ecb_quote")):
        base_code, base_value = _money(row.rate[base], f"{where}'s rate")
        if base_code != currency or base_value != 1:
            raise FileFormatError(f"{where}'s rate is not quoted per one {currency}")
        rates.append(_money(row.rate[quote], f"{where}'s rate"))
    (code, rate), (reference_code, reference) = rates
    original_code, original = _money(row.original.text, f"{where}'s original amount")
    if not code == reference_code == original_code or code == currency:
        raise FileFormatError(f"{where}'s rates and original amount disagree on the currency")
    if column is None or row.original_column != column:
        raise FileFormatError(f"{where}'s original amount is not in its amount's column")
    signed = Decimal(0) - original if column == _OUT else original
    return code, signed, rate, reference


def _built(row: _Row, position: int, currency: str) -> _Built:
    where = f"row {position + 1} (page {row.page + 1})"
    raw = {"Date": row.day_text, "Description": clean_description(row.description)}
    paid: dict[str, Decimal] = {}
    balance: Decimal | None = None
    moves = 0
    for column, word in row.amounts:
        raw[column] = word.text
        value = _in(currency, word.text, f"{where}'s {column.lower()}")
        if column == _BALANCE:
            if balance is not None:
                raise FileFormatError(f"{where} prints two balances")
            balance = value
            continue
        if value < 0:
            raise FileFormatError(f"{where}'s {column.lower()} is negative")
        paid[column] = value
        moves += 1
    raw |= row.details
    card = row.details.get("Card")
    party = row.details.get("To") or row.details.get("From")
    name, number = party, None
    if party is not None and card is None and (match := _PARTY.fullmatch(party)):
        name, number = match["name"], match["number"]
    try:
        counterparty = Counterparty(
            name=name, account_number=number, reference=row.details.get("Reference")
        )
    except ValueError as error:
        raise FileFormatError(f"{where}'s other side: {error}") from None
    paid_in, paid_out = paid.get(_IN), paid.get(_OUT)
    original_currency = original_amount = fx_rate = fx_reference = None
    if row.rate is not None:
        column = _IN if paid_in is not None else _OUT if paid_out is not None else None
        original_currency, original_amount, fx_rate, fx_reference = _fx(
            row, currency, where, column
        )
    parsed = ParsedRow(
        position=position,
        booking_date=row.day,
        occurred_at=None,
        description=raw["Description"],
        quantity=(paid_in or Decimal(0)) - (paid_out or Decimal(0)),
        fee=Decimal(0),
        currency=currency,
        balance=balance,
        external_id=None,
        bank_type=None,
        raw=raw,
        counterparty=None if counterparty == Counterparty() else counterparty,
        card_number=card,
        original_currency=original_currency,
        original_amount=original_amount,
        fx_rate=fx_rate,
        fx_reference_rate=fx_reference,
    )
    return _Built(parsed, paid_in, paid_out, moves, balance)


def _summary(section: _Section) -> tuple[str, list[Decimal]]:
    """The section's one product and its (opening, money out, money in, closing), which the
    total must repeat."""
    products = [(label, values) for label, values in section.summary if label != "Total"]
    totals = [values for label, values in section.summary if label == "Total"]
    if len(products) != 1 or len(totals) != 1:
        raise FileFormatError(
            f"the {section.currency} balance summary must have one product and its total"
        )
    product, values = products[0]
    if totals[0] != values:
        raise FileFormatError(
            f"the {section.currency} balance summary's total differs from its product's row"
        )
    return product, values


def parse_current(lines: list[list[Word]]) -> LayoutResult:
    sections = _sections(lines)
    if not sections:
        raise FileFormatError(
            "this is not a Revolut current account statement: it has no balance summary"
        )
    summaries = {id(section): _summary(section) for section in sections}
    active = [section for section in sections if section.rows or any(summaries[id(section)][1])]
    if len(active) > 1:
        currencies = ", ".join(section.currency for section in active)
        raise FileFormatError(
            f"the statement shows money in more than one currency ({currencies}); "
            "only one currency per statement is supported"
        )
    if not active:
        raise FileFormatError("no rows")
    section = active[0]
    product, (opening, money_out, money_in, closing) = summaries[id(section)]
    if section.period is None:
        raise FileFormatError('the statement has no "Account transactions from … to …"')
    rows: list[LayoutRow] = []
    balances: list[PrintedBalance] = []
    for row in (row for row in section.rows if row.kind == _COMPLETED):
        built = _built(row, len(rows), section.currency)
        rows.append(LayoutRow(built.row, row.page, built.paid_in, built.paid_out, built.moves))
        if built.balance is not None:
            balances.append(PrintedBalance(len(rows), built.balance, row.page))
    skipped: list[SkippedRow] = []
    for row in (row for row in section.rows if row.kind != _COMPLETED):
        position = len(rows) + len(skipped)
        built = _built(row, position, section.currency)
        reason = f"{row.kind.lower()}: not in the statement's balance"
        skipped.append(SkippedRow(position=position, reason=reason, raw=built.row.raw))
    return LayoutResult(
        rows=rows,
        period=section.period,
        opening_balance=opening,
        money_in=money_in,
        money_out=money_out,
        closing_balance=closing,
        balances=balances,
        unplaced=[word for each in sections for word in each.unplaced],
        account=_statement_account(section.intro, ("Sort Code", "Account Number"), product),
        currency=section.currency,
        document_date=_generated(lines),
        skipped=skipped,
        type_codes=False,
    )


# The savings account ------------------------------------------------------------------

_SAVINGS = "Savings Statement"
# The period's ends are joined by an en dash (or a hyphen).
_PERIOD = re.compile(rf"Period: (?P<start>{_SHORT}) [\u2013-] (?P<end>{_SHORT})")
_OPENING, _CLOSING = "Opening Balance", "Closing Balance"
# Each kind of row, the summary line totalling it, and the column it is paid in.
_KINDS = {
    "Deposit": ("Total Deposit", _IN),
    "Withdrawal": ("Total Withdrawal", _OUT),
    "Gross Interest": ("Total Interest Paid", _IN),
}
_TOTALS = {_OPENING, _CLOSING, *(label for label, _ in _KINDS.values())}


@dataclass(frozen=True)
class _SavingsHeader:
    period: tuple[date, date]
    totals: dict[str, Decimal]
    currency: str
    account: StatementAccount


def _savings_header(first: list[list[Word]]) -> tuple[_SavingsHeader, int]:
    """The first page's header, and where its table header is."""
    period: tuple[date, date] | None = None
    totals: dict[str, tuple[str, Decimal]] = {}
    summary_start: int | None = None
    last_total = 0
    for n, line in enumerate(first):
        printed = text(line)
        if match := _PERIOD.fullmatch(printed):
            period = (
                _short_date(match["start"], "the period's start"),
                _short_date(match["end"], "the period's end"),
            )
        elif printed == "Balance summary":
            summary_start = n
        elif _table_header(line, right_aligned_in=True) is not None:
            if period is None or summary_start is None or set(totals) != _TOTALS:
                break
            currencies = {code for code, _ in totals.values()}
            if len(currencies) != 1:
                raise FileFormatError("the balance summary's amounts are in several currencies")
            product = [text(line) for line in first[last_total + 1 : n]]
            account = _statement_account(
                first[:summary_start],
                ("Sort Code:", "Account number:"),
                " ".join(product) or None,
            )
            header = _SavingsHeader(
                period,
                {label: value for label, (_, value) in totals.items()},
                currencies.pop(),
                account,
            )
            return header, n
        elif summary_start is not None and len(line) > 1:
            label = text(line[:-1])
            if label in _TOTALS and _is_money(line[-1].text):
                totals[label] = _money(line[-1].text, f"the balance summary's {label}")
                last_total = n
    raise FileFormatError(
        "this is not a Revolut savings statement: page 1 has no period, balance summary "
        "or transaction table"
    )


def _savings_row(
    split: _Split, line: list[Word], position: int, currency: str
) -> tuple[LayoutRow, Decimal | None]:
    where = f"row {position + 1} (page {line[0].page + 1})"
    day_text = text(split.dates)
    description = clean_description([text(split.description)])
    if description not in _KINDS:
        raise FileFormatError(f"{where} is a {description!r}, which the summary doesn't total")
    _, column = _KINDS[description]
    raw = {"Date": day_text, "Description": description}
    paid: dict[str, Decimal] = {}
    balance: Decimal | None = None
    for printed_column, word in split.amounts:
        raw[printed_column] = word.text
        value = _in(currency, word.text, f"{where}'s {printed_column.lower()}")
        if printed_column == _BALANCE:
            if balance is not None:
                raise FileFormatError(f"{where} prints two balances")
            balance = value
        elif printed_column != column or value < 0:
            raise FileFormatError(f"{where}'s {description} is not in the {column} column")
        else:
            paid[printed_column] = value
    moves = sum(1 for printed_column, _ in split.amounts if printed_column != _BALANCE)
    paid_in, paid_out = paid.get(_IN), paid.get(_OUT)
    row = ParsedRow(
        position=position,
        booking_date=_short_date(day_text, f"{where}'s date"),
        occurred_at=None,
        description=description,
        quantity=(paid_in or Decimal(0)) - (paid_out or Decimal(0)),
        fee=Decimal(0),
        currency=currency,
        balance=balance,
        external_id=None,
        bank_type=description,  # the description is the row's kind
        raw=raw,
    )
    return LayoutRow(row, line[0].page, paid_in, paid_out, moves), balance


def parse_savings(lines: list[list[Word]]) -> LayoutResult:
    by_page = pages(lines)
    first = by_page.get(0, [])
    if not any(text(line) == _SAVINGS for line in first[:3]):
        raise FileFormatError("this is not a Revolut savings statement")
    header, _ = _savings_header(first)
    rows: list[LayoutRow] = []
    balances: list[PrintedBalance] = []
    unplaced: list[Word] = []
    for number in sorted(by_page):
        page = by_page[number]
        start = next(
            (
                n
                for n, line in enumerate(page)
                if _table_header(line, right_aligned_in=True) is not None
            ),
            None,
        )
        if start is None:
            continue
        columns = _table_header(page[start], right_aligned_in=True)
        assert columns is not None
        # The table runs while lines start with a date; what follows is not part of it. A
        # row it misses would leave the summary's totals unmet.
        for line in page[start + 1 :]:
            split = _split(line, columns, right_aligned_in=True)
            if not _DAY.fullmatch(text(split.dates)):
                break
            if split.unplaced or not split.description:
                unplaced.extend(line)
                continue
            row, balance = _savings_row(split, line, len(rows), header.currency)
            rows.append(row)
            if balance is not None:
                balances.append(PrintedBalance(len(rows), balance, number))
    for kind, (label, _) in _KINDS.items():
        total = sum((r.row.quantity for r in rows if r.row.description == kind), Decimal(0))
        stated = header.totals[label]
        if abs(total) != stated:
            raise FileFormatError(
                f"the statement's {label} is {stated} but its {kind} rows add up to {abs(total)}"
            )
    totals = header.totals
    return LayoutResult(
        rows=rows,
        period=header.period,
        opening_balance=totals[_OPENING],
        money_in=totals["Total Deposit"] + totals["Total Interest Paid"],
        money_out=totals["Total Withdrawal"],
        closing_balance=totals[_CLOSING],
        balances=balances,
        unplaced=unplaced,
        account=header.account,
        currency=header.currency,
        document_date=_generated(first),
    )
