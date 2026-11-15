"""Reads an OFX 1.x (SGML) or 2.x (XML) bank or credit card statement. Pure: no database.

No OFX library: a small tokenizer keeps every date as written (Decision 8).
"""

import html
import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from lararium.modules.finance.errors import FileFormatError
from lararium.modules.finance.importing import parsed
from lararium.modules.finance.importing.parsed import (
    ParsedRow,
    ParsedStatement,
    RowError,
    SkippedRow,
    StatementAccount,
    clean_description,
    keyed,
    normalise_account_number,
    normalise_sort_code,
    parse_amount,
)
from lararium.modules.finance.importing.profiles import AmountFormat, OfxProfileConfig

PARSER_VERSION = "ofx-1"

_START = re.compile(r"<OFX>", re.IGNORECASE)
_UTF8_HEADER = re.compile(rb"ENCODING:\s*UTF-8|encoding\s*=\s*[\"']UTF-8[\"']", re.IGNORECASE)
_TOKEN = re.compile(r"<(/?)([A-Za-z0-9.]+)>([^<]*)")
# YYYYMMDD[HHMMSS[.XXX]][[±H[H][.MM]][:TZ]]
_DATETIME = re.compile(
    r"(\d{4})(\d{2})(\d{2})"
    r"(?:(\d{2})(\d{2})(\d{2})(?:\.(\d{3}))?)?"
    r"(?:\[(?:([+-]?)(\d{1,2})(?:\.(\d{2}))?)?(?::[^\]]*)?\])?"
)
_CURRENCY = re.compile(r"[A-Z]{3}")
_STATEMENTS = frozenset({"STMTRS", "CCSTMTRS"})
# Real statements nest about eight deep; the bound keeps the recursive walks safe.
_MAX_DEPTH = 64


@dataclass
class _Aggregate:
    name: str
    leaves: list[tuple[str, str]] = field(default_factory=list[tuple[str, str]])
    children: list["_Aggregate"] = field(default_factory=list["_Aggregate"])

    def child(self, name: str) -> "_Aggregate | None":
        return next((child for child in self.children if child.name == name), None)

    def find(self, names: frozenset[str]) -> list["_Aggregate"]:
        """Every descendant with one of the names, in document order."""
        found: list[_Aggregate] = []
        for child in self.children:
            if child.name in names:
                found.append(child)
            found.extend(child.find(names))
        return found

    def leaf(self, name: str) -> str | None:
        """A leaf directly inside this aggregate, or None when missing or empty."""
        return next((value for leaf, value in self.leaves if leaf == name), None) or None

    def leaf_pairs(self, prefix: str = "") -> Iterator[tuple[str, str]]:
        """Every leaf, nested ones keyed "AGGREGATE/LEAF"."""
        for name, value in self.leaves:
            yield prefix + name, value
        for child in self.children:
            yield from child.leaf_pairs(f"{prefix}{child.name}/")

    def leaf_values(self) -> dict[str, str]:
        """Every leaf, nested ones keyed "AGGREGATE/LEAF"; a repeated key gets "#2", "#3"…"""
        return keyed(self.leaf_pairs())


def _decode(content: bytes) -> str:
    start = _START.search(content.decode("latin-1"))
    if start is None:
        raise FileFormatError("not an OFX file: there is no <OFX> tag")
    utf8 = _UTF8_HEADER.search(content, 0, start.start()) is not None
    encoding = "utf-8-sig" if utf8 else "cp1252"
    try:
        text = content.decode(encoding)
    except UnicodeDecodeError:
        raise FileFormatError(f"the file is not valid {encoding.removesuffix('-sig')}") from None
    found = _START.search(text)
    assert found is not None
    return text[found.start() :]


def _absorb(stack: list[_Aggregate]) -> None:
    """Close the innermost open tag that had no closing tag: it was an empty SGML leaf, so
    what was read inside it belongs to its parent."""
    node = stack.pop()
    parent = stack[-1]
    parent.children.remove(node)
    parent.leaves.append((node.name, ""))
    parent.leaves.extend(node.leaves)
    parent.children.extend(node.children)


def _tree(text: str) -> _Aggregate:
    """Leaves have their value after the opening tag (SGML leaves aren't closed);
    aggregates are opened with no value and always closed."""
    root = _Aggregate("")
    stack = [root]
    last_leaf: str | None = None
    for match in _TOKEN.finditer(text):
        closing, tag, value = match.groups()
        name = tag.upper()
        if closing:
            if name == last_leaf:  # XML closes its leaves
                last_leaf = None
                continue
            last_leaf = None
            if name not in (node.name for node in stack[1:]):
                raise FileFormatError(f"not a valid OFX file: </{name}> closes nothing")
            while stack[-1].name != name:
                _absorb(stack)
            node = stack[-1]
            if not node.leaves and not node.children:
                _absorb(stack)  # XML's empty leaf, <MEMO></MEMO>
            else:
                stack.pop()
            continue
        text_value = value.strip()
        if text_value:
            stack[-1].leaves.append((name, html.unescape(text_value)))
            last_leaf = name
        else:
            if len(stack) > _MAX_DEPTH:
                raise FileFormatError("the file is nested too deeply")
            node = _Aggregate(name)
            stack[-1].children.append(node)
            stack.append(node)
            last_leaf = None
    if len(stack) > 1:
        raise FileFormatError(f"not a valid OFX file: it ends inside <{stack[-1].name}>")
    return root


def _optional(raw: dict[str, str], key: str) -> str | None:
    return raw.get(key) or None


def _datetime(text: str, what: str) -> tuple[date, datetime | None]:
    """The date as written, and the aware time when one is given (UTC without an offset)."""
    match = _DATETIME.fullmatch(text)
    if match is None:
        raise ValueError(f"{what}: not an OFX date: {text!r}")
    year, month, day, hour, minute, second, millis, sign, hours, minutes = match.groups()
    try:
        written = date(int(year), int(month), int(day))
        if hour is None:
            return written, None
        offset = timedelta(hours=int(hours or 0), minutes=int(minutes or 0))
        zone = timezone(-offset if sign == "-" else offset)
        when = datetime(
            written.year,
            written.month,
            written.day,
            int(hour),
            int(minute),
            int(second),
            int(millis or 0) * 1000,
            tzinfo=zone,
        )
    except ValueError:
        raise ValueError(f"{what}: not an OFX date: {text!r}") from None
    return written, when


def _currency(text: str) -> str:
    code = text.upper()
    if not (code.isascii() and _CURRENCY.fullmatch(code)):
        raise ValueError(f"not a currency code: {text!r}")
    return code


def _signed(amount: Decimal, config: OfxProfileConfig) -> Decimal:
    return amount if config.sign == "inflow_positive" else Decimal(0) - amount


def _row(
    position: int, raw: dict[str, str], currency: str | None, config: OfxProfileConfig
) -> ParsedRow:
    """Raises ValueError with a message for the row's error."""
    posted = _optional(raw, "DTPOSTED")
    if posted is None:
        raise ValueError("the transaction has no DTPOSTED")
    booking_date, occurred_at = _datetime(posted, "DTPOSTED")
    amount = _optional(raw, "TRNAMT")
    if amount is None:
        raise ValueError("the transaction has no TRNAMT")
    try:
        quantity = _signed(parse_amount(amount, AmountFormat()), config)
    except ValueError as error:
        raise ValueError(f"TRNAMT: {error}") from None
    user_date = _optional(raw, "DTUSER")
    own_currency = _optional(raw, "CURRENCY/CURSYM") or _optional(raw, "ORIGCURRENCY/CURSYM")
    payee = _optional(raw, "NAME")
    memo = _optional(raw, "MEMO")
    return ParsedRow(
        position=position,
        booking_date=booking_date,
        occurred_at=occurred_at,
        description=clean_description([part for part in (payee, memo) if part is not None]),
        quantity=quantity,
        fee=Decimal(0),
        currency=_currency(own_currency) if own_currency is not None else currency,
        balance=None,
        external_id=_optional(raw, "FITID"),
        bank_type=_optional(raw, "TRNTYPE"),
        raw=raw,
        value_date=None if user_date is None else _datetime(user_date, "DTUSER")[0],
        payee=payee,
        memo=memo,
        check_number=_optional(raw, "CHECKNUM"),
    )


def _account(statement: _Aggregate) -> StatementAccount | None:
    source = statement.child("BANKACCTFROM") or statement.child("CCACCTFROM")
    if source is None:
        return None
    values = source.leaf_values()
    sort_code: str | None = None
    account_number: str | None = None
    try:
        # A UK BANKID is the sort code; other countries' bank IDs aren't six digits.
        sort_code = normalise_sort_code(_optional(values, "BANKID"))
    except ValueError:
        pass
    try:
        # Masked card numbers ("XXXX1234") have no typed home.
        account_number = normalise_account_number(_optional(values, "ACCTID"))
    except ValueError:
        pass
    account = StatementAccount(
        sort_code=sort_code,
        account_number=account_number,
        account_type=_optional(values, "ACCTTYPE"),
    )
    return None if account == StatementAccount() else account


def _closing(statement: _Aggregate, config: OfxProfileConfig) -> tuple[Decimal | None, date | None]:
    ledger = statement.child("LEDGERBAL")
    if ledger is None:
        return None, None
    values = ledger.leaf_values()
    amount, as_of = _optional(values, "BALAMT"), _optional(values, "DTASOF")
    if amount is None or as_of is None:
        raise FileFormatError("LEDGERBAL needs both BALAMT and DTASOF")
    try:
        balance = _signed(parse_amount(amount, AmountFormat()), config)
        closing_date, _ = _datetime(as_of, "DTASOF")
    except ValueError as error:
        raise FileFormatError(f"LEDGERBAL: {error}") from None
    return balance, closing_date


def parse_ofx(content: bytes, config: OfxProfileConfig) -> ParsedStatement:
    statements = _tree(_decode(content)).find(_STATEMENTS)
    if len(statements) != 1:
        found = "no statement" if not statements else f"{len(statements)} statements"
        raise FileFormatError(f"the file has {found}: export one account per file")
    statement = statements[0]
    curdef = statement.leaf("CURDEF")
    try:
        currency = None if curdef is None else _currency(curdef)
    except ValueError as error:
        raise FileFormatError(f"CURDEF: {error}") from None
    transactions = statement.find(frozenset({"STMTTRN"}))
    if not transactions:
        raise FileFormatError("no rows")
    if len(transactions) > parsed.MAX_IMPORT_ROWS:
        raise FileFormatError(f"the file has more than {parsed.MAX_IMPORT_ROWS} rows")
    rows: list[ParsedRow | RowError | SkippedRow] = []
    for position, transaction in enumerate(transactions):
        raw = transaction.leaf_values()
        try:
            rows.append(_row(position, raw, currency, config))
        except ValueError as error:
            rows.append(RowError(position=position, message=str(error), raw=raw))
    closing_balance, closing_balance_date = _closing(statement, config)
    return ParsedStatement(
        rows=rows,
        closing_balance=closing_balance,
        closing_balance_date=closing_balance_date,
        account=_account(statement),
        currency=currency,
    )
