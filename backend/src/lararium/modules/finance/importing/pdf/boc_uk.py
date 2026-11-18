"""Bank of China (UK) statements (iTextSharp, bilingual labels). Pure: a function of the words.

Labels are printed "English/中文:value". The table sits between horizontal rules (printed
as "─" characters) with trade and value dates, details over several lines, "Paid out",
"Paid in" and a balance on every row; amounts sit under the header labels. Pages after
the last "Page N of M" are terms and conditions. A statement holds one currency; one with
several currency sections is refused until one is seen.

The details column holds "voucher/business number/purpose/summary"; the summary starts
with the payment kind. The column wraps at a fixed width: at a space, after a hyphen, or
mid-word when a single word is wider than the column.
"""

import re
from dataclasses import dataclass, field, replace
from decimal import Decimal

from lararium.modules.finance.errors import FileFormatError
from lararium.modules.finance.importing.parsed import (
    Counterparty,
    ParsedRow,
    StatementAccount,
    clean_description,
)
from lararium.modules.finance.importing.pdf.layout import (
    LayoutResult,
    LayoutRow,
    PrintedBalance,
    amount,
    last_word_of,
    mailing_block,
    nearest,
    pages,
    parse_date,
    split_amounts,
    text,
    word_of,
)
from lararium.modules.finance.importing.pdf.words import Word

X_TOLERANCE = Decimal("1.5")

# A label's Chinese part and its colon, ASCII or full-width.
_LABEL_END = r"[^:\uff1a]*[:\uff1a]"
_PAGE = re.compile(r"Page (\d+) of (\d+)\b")
_RULE = re.compile(r"─+")
_DAY = re.compile(r"\d{2}/\d{2}/\d{2}")
_PREVIOUS_PAGE = re.compile(rf"Balance Carried Over From Previous Page\s*/{_LABEL_END}(\S+)")
_NEXT_PAGE = re.compile(rf"Balance Carried Forward To Next Page\s*/{_LABEL_END}(\S+)")
_COLUMN = Decimal(15)
_MARGIN = Decimal(10)
# The details column wraps lines wider than this (measured on the bank's statements: every
# line is at most 128.9 pt wide; a word broken mid-way would have reached 129.86 pt with its
# next character, a word moved whole to the next line at most 129.45 pt).
_WRAP_WIDTH = Decimal("129.6")

# The payment kinds the statement's terms explain, longest first so "Inward Transfer" isn't
# read as "Transfer".
_KINDS = sorted(
    (
        "Pre-Authorization Completed",
        "Local Clearing And Outward Remittance",
        "Local Clearing And Inward Remittance",
        "Outward Transfer",
        "Inward Transfer",
        "Transfer",
        "International Outward Remittance",
        "International Inward Remittance",
        "International Outward Draft",
        "International Inward Draft",
        "Cash Deposit",
        "Withdrawal",
        "Loan Repayment",
        "Interest Settlement",
        "Charging",
        "Batch Charging",
        "Account Settlement",
        "Bank Cheque",
        "Correct",
    ),
    key=len,
    reverse=True,
)
_TAG = re.compile(r"/([A-Z]{3,4})/([^/]*)")
_INTERNATIONAL = re.compile(
    r"(?P<name>.*?)\s*REFED-NO:\s*(?P<reference>\S*?)\s*BUSIN-NO:\s*(?P<business>\S+)\s*(?P<rest>.*)"
)
# A sort code counts only where its meaning is printed: followed by the account number, or
# directly by the bank's name, a run of upper-case words ending in its legal form ("401234
# EXAMPLE BANK PLC"), optionally then the account number. Any other 6-digit run could be
# anything (an invoice number), so it is kept in raw only.
_BANK = r"(?:[A-Z0-9&.'()/-]+ )*?(?:PLC|LIMITED|LTD\.?)"
_LOCAL = re.compile(
    rf"(?P<name>.+?) (?P<sort_code>\d{{6}})(?: (?P<account>\d{{8}}))?"
    rf"(?: (?P<bank>{_BANK})(?: (?P<account_after>\d{{8}}))?)?(?: (?P<reference>.+))?"
)
_SIX_DIGITS = re.compile(r"(?P<name>.+?) \d{6} (?P<rest>.+)")
# Between the statement's own accounts: the name, a number whose meaning isn't printed (kept
# in raw only), then the statement's own branch.
_INTERNAL = re.compile(r"(?P<name>.+?) \d+ (?P<rest>.+)")
_TRANSFERS = frozenset({"Transfer", "Inward Transfer", "Outward Transfer"})

# "Label/中文:value" fields: the whole line, or one value among several on a line.
_LINE_FIELDS = {
    "account_type": "Account Type",
    "statement_reference": "Statement Reference Number",
    "start": "From",
    "end": "To",
    "customer_number": "Customer Number",
    "branch": "Account Opening Branch",
    "branch_address": "Account Opening Branch Address",
    "name": "Account Name",
    "account_number": "Account Number",
    "clearing_account_number": "UK Clearing Account Number",
    "sort_code": "Sort Code",
    "bic": "SWIFT Code",
    "iban": "International Bank Account Number(IBAN)",
}
_SUMMARY_FIELDS = {
    "opening": "Opening Balance",
    "closing": "Closing Balance",
    "out_volume": "Payments Out Volume",
    "out": "Payments Out",
    "in_volume": "Payments In Volume",
    "in": "Payments In",
}


# A line starting "Label/中文:value"; the value stops before a next "Label/" (the "To" line
# also holds the closing balance).
_LINE_PATTERNS = {
    key: re.compile(rf"^{re.escape(label)}/{_LABEL_END}(.*?)(?=\s+[A-Z][A-Za-z ]*/|$)")
    for key, label in _LINE_FIELDS.items()
}
_SUMMARY_PATTERNS = {
    key: re.compile(rf"(?:^|\s){re.escape(label)}/{_LABEL_END}(\S+)")
    for key, label in _SUMMARY_FIELDS.items()
}


def _statement_pages(lines: list[list[Word]]) -> int:
    for line in lines:
        match = _PAGE.match(text(line))
        if match is not None:
            return int(match[2])
    raise FileFormatError("this is not a BOC UK statement: it has no page numbers")


@dataclass
class _Fields:
    values: dict[str, str] = field(default_factory=dict[str, str])
    account_numbers: set[str] = field(default_factory=set[str])
    currencies: int = 0
    balance_rows: int = 0


def _fields(lines: list[list[Word]]) -> _Fields:
    found = _Fields()
    in_balances = False
    for line in lines:
        printed = text(line)
        if printed.startswith("Currency/"):
            found.currencies += 1
        if printed.startswith("Account Balance Summary/"):
            in_balances = True
        elif printed.startswith("Transaction Summary/"):
            in_balances = False
        elif in_balances and re.fullmatch(r"[A-Z]{3} \S+", printed):
            found.balance_rows += 1
        for key, pattern in _LINE_PATTERNS.items():
            match = pattern.match(printed)
            if match is None:
                continue
            value = match[1].strip()
            if not value:  # the label printed without a value: a later page may have it
                continue
            if key == "account_number":
                found.account_numbers.add(value)
            found.values.setdefault(key, value)
        for key, pattern in _SUMMARY_PATTERNS.items():
            match = pattern.search(printed)
            if match is not None:
                found.values.setdefault(key, match[1])
        if (currency := re.match(rf"Currency/{_LABEL_END}([A-Z]{{3}})$", printed)) is not None:
            found.values.setdefault("currency", currency[1])
    return found


def _account(values: dict[str, str]) -> StatementAccount:
    branch = ", ".join(
        part for part in (values.get("branch"), values.get("branch_address")) if part
    )
    try:
        return StatementAccount(
            sort_code=values.get("sort_code"),
            account_number=values.get("account_number"),
            iban=values.get("iban"),
            bic=values.get("bic"),
            name=values.get("name"),
            account_type=values.get("account_type"),
            customer_number=values.get("customer_number"),
            clearing_account_number=values.get("clearing_account_number"),
            branch=branch or None,
        )
    except ValueError as error:
        raise FileFormatError(f"the statement's account: {error}") from None


def _width(words: list[Word]) -> Decimal:
    return words[-1].x1 - words[0].x0


def _details_text(lines: list[list[Word]]) -> str:
    """The details lines joined as written: a line ending in "-" was broken after the hyphen,
    and a line of one word was broken mid-word when the next line's first character (here
    the next word's average character) would not have fitted."""
    joined = ""
    for n, line in enumerate(lines):
        piece = text(line)
        if n == 0:
            joined = piece
            continue
        previous = lines[n - 1]
        first = line[0]
        next_char = (first.x1 - first.x0) / len(first.text)
        if previous[-1].text.endswith("-") or (
            len(previous) == 1 and _width(previous) + next_char > _WRAP_WIDTH
        ):
            joined += piece
        else:
            joined += " " + piece
    return joined


@dataclass(frozen=True)
class _Payment:
    kind: str | None = None
    bank_reference: str | None = None
    counterparty: Counterparty | None = None
    purpose_code: str | None = None
    remittance_info: str | None = None


def _tags(segment: str) -> tuple[str | None, str | None]:
    """The purpose code and remittance text of "/PURP/OTHR///URI/EXAMPLE TEXT"."""
    tags = dict(_TAG.findall(segment))
    return tags.get("PURP") or None, tags.get("URI") or None


def _counterparty(
    *,
    name: str | None = None,
    sort_code: str | None = None,
    account_number: str | None = None,
    reference: str | None = None,
    bank: str | None = None,
) -> Counterparty | None:
    try:
        counterparty = Counterparty(
            name=name,
            sort_code=sort_code,
            account_number=account_number,
            reference=reference,
            bank=bank,
        )
    except ValueError:
        return None
    return None if counterparty == Counterparty() else counterparty


def _payment(details: str, branch: str | None) -> _Payment:
    # voucher/business number/purpose/summary
    head = re.fullmatch(r"([^/]*)/([^/]*)/([^/]*)/(.*)", details)
    if head is None:
        return _Payment()
    summary = head[4]
    kind = next((k for k in _KINDS if summary.lower().startswith(k.lower())), None)
    if kind is None:
        return _Payment(bank_reference=head[2] or None)
    printed_kind = summary[: len(kind)]
    rest = summary[len(kind) :].strip()
    purpose: str | None = None
    remittance: str | None = None
    tagged = rest.find("/PURP/")
    if tagged == -1:
        tagged = rest.find("/URI/")
    if tagged != -1:
        purpose, remittance = _tags(rest[tagged:])
        rest = rest[:tagged].strip()
    # Only the shapes seen on the bank's statements are typed; any other text stays in the
    # description and raw, so no free-text number becomes an account number.
    counterparty: Counterparty | None = None
    if kind.startswith("International"):
        if (international := _INTERNATIONAL.fullmatch(rest)) is not None:
            counterparty = _counterparty(
                name=international["name"] or None,
                reference=international["reference"] or None,
            )
            remittance = remittance or international["rest"] or None
    elif kind.startswith("Local Clearing"):
        local = _LOCAL.fullmatch(rest)
        if local is not None and (local["bank"] or local["account"]):
            counterparty = _counterparty(
                name=local["name"],
                sort_code=local["sort_code"],
                account_number=local["account"] or local["account_after"],
                bank=local["bank"],
                reference=local["reference"],
            )
        elif (six := _SIX_DIGITS.fullmatch(rest)) is not None:
            counterparty = _counterparty(name=six["name"], reference=six["rest"])
    elif kind in _TRANSFERS and branch:
        internal = _INTERNAL.fullmatch(rest)
        if internal is not None and internal["rest"].startswith(branch):
            counterparty = _counterparty(
                name=internal["name"],
                bank=branch,
                reference=internal["rest"][len(branch) :].strip() or None,
            )
    return _Payment(
        kind=printed_kind,
        bank_reference=head[2] or None,
        counterparty=counterparty,
        purpose_code=purpose,
        remittance_info=remittance,
    )


@dataclass
class _Columns:
    details_from: Decimal
    amounts_from: Decimal
    right_edges: dict[str, Decimal]


def _columns(line: list[Word]) -> _Columns | None:
    value = word_of(line, "Value")
    paid_out = word_of(line, "Paid out")
    edges = {
        "Paid out": last_word_of(line, "Paid out"),
        "Paid in": last_word_of(line, "Paid in"),
        "Balance": word_of(line, "Balance"),
    }
    if (
        word_of(line, "Trans.") is None
        or value is None
        or paid_out is None
        or None in edges.values()
    ):
        return None
    return _Columns(
        details_from=value.x1 + _MARGIN,
        amounts_from=paid_out.x0 - _MARGIN * 3,
        right_edges={column: word.x1 for column, word in edges.items() if word is not None},
    )


@dataclass
class _Pending:
    trade_date: str
    value_date: str
    page: int
    details: list[list[Word]] = field(default_factory=list[list[Word]])
    amounts: list[tuple[str, Word]] = field(default_factory=list[tuple[str, Word]])


@dataclass
class _Table:
    currency: str | None
    branch: str | None
    rows: list[LayoutRow] = field(default_factory=list[LayoutRow])
    balances: list[PrintedBalance] = field(default_factory=list[PrintedBalance])
    unplaced: list[Word] = field(default_factory=list[Word])
    pending: _Pending | None = None

    def close(self) -> None:
        pending, self.pending = self.pending, None
        if pending is None:
            return
        position = len(self.rows)
        details = _details_text(pending.details) if pending.details else ""
        raw = {
            "Trans. Date": pending.trade_date,
            "Value Date": pending.value_date,
            "Details": details,
        }
        where = f"row {position + 1} (page {pending.page + 1})"
        split = split_amounts(pending.amounts, "Balance", where)
        for column, word in pending.amounts:
            raw[column] = word.text
        balance = (
            None if split.balance is None else amount(split.balance.text, f"{where}'s balance")
        )
        paid_in, paid_out = split.paid.get("Paid in"), split.paid.get("Paid out")
        payment = _payment(details, self.branch)
        self.rows.append(
            LayoutRow(
                row=ParsedRow(
                    position=position,
                    booking_date=parse_date(
                        pending.trade_date, "%d/%m/%y", f"row {position + 1}'s date"
                    ),
                    occurred_at=None,
                    description=clean_description([details]),
                    quantity=(paid_in or Decimal(0)) - (paid_out or Decimal(0)),
                    fee=Decimal(0),
                    currency=self.currency,
                    balance=balance,
                    external_id=None,
                    bank_type=payment.kind,
                    raw=raw,
                    value_date=parse_date(
                        pending.value_date, "%d/%m/%y", f"row {position + 1}'s value date"
                    ),
                    counterparty=payment.counterparty,
                    payment_kind=payment.kind,
                    bank_reference=payment.bank_reference,
                    purpose_code=payment.purpose_code,
                    remittance_info=payment.remittance_info,
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
    printed = text(line)
    carried = _PREVIOUS_PAGE.fullmatch(printed) or _NEXT_PAGE.fullmatch(printed)
    if carried is not None:
        table.close()
        value = amount(carried[1], f"the balance carried on page {line[0].page + 1}")
        table.balances.append(PrintedBalance(len(table.rows), value, line[0].page))
        return
    left = [w for w in line if w.x1 <= columns.amounts_from]
    dates = [w for w in left if w.x0 < columns.details_from]
    details = [w for w in left if w.x0 >= columns.details_from]
    if dates:
        if len(dates) != 2 or not all(_DAY.fullmatch(w.text) for w in dates):
            table.unplaced.extend(line)
            return
        table.close()
        table.pending = _Pending(dates[0].text, dates[1].text, line[0].page)
    pending = table.pending
    if pending is None:
        table.unplaced.extend(line)
        return
    if details:
        pending.details.append(details)
    for word in line:
        if word.x1 > columns.amounts_from:
            column = nearest(word, columns.right_edges, _COLUMN)
            if column is None:
                table.unplaced.append(word)
            else:
                pending.amounts.append((column, word))


def _read_page(page_lines: list[list[Word]], table: _Table) -> None:
    header = next((n for n, line in enumerate(page_lines) if _columns(line) is not None), None)
    if header is None:
        return
    columns = _columns(page_lines[header])
    assert columns is not None
    rules = [n for n, line in enumerate(page_lines) if n > header and _RULE.fullmatch(text(line))]
    if len(rules) < 2:
        raise FileFormatError(
            f"the transaction table on page {page_lines[0][0].page + 1} isn't closed"
        )
    for line in page_lines[rules[0] + 1 : rules[1]]:
        _line(line, columns, table)
    table.close()


def parse(lines: list[list[Word]]) -> LayoutResult:
    statement_pages = _statement_pages(lines)
    lines = [line for line in lines if line[0].page < statement_pages]  # then terms
    found = _fields(lines)
    if found.currencies > 1 or found.balance_rows > 1:
        raise FileFormatError(
            "the statement has sections for several currencies; only one-currency statements "
            "are supported"
        )
    if len(found.account_numbers) > 1:
        raise FileFormatError("the statement names two accounts: export one account per file")
    values = found.values
    missing = [
        label
        for key, label in (_SUMMARY_FIELDS | {"start": "From", "end": "To"}).items()
        if key not in values
    ]
    if missing:
        raise FileFormatError(f"the statement has no {', '.join(missing)}")
    period = (
        parse_date(values["start"], "%d/%m/%Y", "the statement's start"),
        parse_date(values["end"], "%d/%m/%Y", "the statement's end"),
    )
    # The mailing block opens page 1.
    addressee, address = (
        mailing_block(lines, 0, Decimal(300)) if lines and lines[0][0].page == 0 else (None, None)
    )
    account = replace(_account(values), addressee=addressee, address=address)
    table = _Table(currency=values.get("currency"), branch=values.get("branch"))
    by_page = pages(lines)
    for number in sorted(by_page):
        _read_page(by_page[number], table)
    try:
        payments_in = int(values["in_volume"])
        payments_out = int(values["out_volume"])
    except ValueError:
        raise FileFormatError("the statement's payment counts aren't numbers") from None
    return LayoutResult(
        rows=table.rows,
        period=period,
        opening_balance=amount(values["opening"], "the statement's opening balance"),
        money_in=amount(values["in"], "the statement's payments in"),
        money_out=amount(values["out"], "the statement's payments out"),
        closing_balance=amount(values["closing"], "the statement's closing balance"),
        balances=table.balances,
        unplaced=table.unplaced,
        payments_in=payments_in,
        payments_out=payments_out,
        account=account,
        currency=values.get("currency"),
        statement_reference=values.get("statement_reference"),
    )
