"""Reads an OFX 1.x (SGML) or 2.x (XML) bank or credit card statement. Pure: no database.

No OFX library: a small tokenizer keeps every date as written (Decision 8).
"""

import html
import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal

from lararium.modules.finance.errors import FileFormatError
from lararium.modules.finance.importing import parsed
from lararium.modules.finance.importing.parsed import (
    Counterparty,
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
# OFX 1.x writes UTF-8 as ENCODING:UNICODE or ENCODING:UTF-8; 2.x has an XML declaration.
_UTF8_HEADER = re.compile(
    rb"ENCODING:\s*(?:UTF-8|UNICODE)|encoding\s*=\s*[\"']UTF-8[\"']", re.IGNORECASE
)
# An encoding stated in the header: SGML's ENCODING:, or an XML declaration's encoding=.
_STATED_ENCODING = re.compile(rb"ENCODING:|\bencoding\s*=", re.IGNORECASE)
# OFX 2.x is XML (an <?xml ...?> or <?OFX ...?> processing instruction).
_XML_INSTRUCTION = re.compile(rb"<\?(?:xml|OFX)\b", re.IGNORECASE)
# Only a well-formed reference is one: SGML OFX writes a bare "&" too ("B&Q"), which
# html.unescape would read as a legacy entity ("&LT" -> "<").
_ENTITY = re.compile(r"&(?:#[0-9]+|#[xX][0-9a-fA-F]+|[A-Za-z][A-Za-z0-9]*);")
_TOKEN = re.compile(r"<(/?)([A-Za-z0-9.]+)>([^<]*)")
# XML OFX may wrap a value in CDATA; the tokenizer reads text only up to the next "<".
_CDATA_OPEN, _CDATA_CLOSE = "<![CDATA[", "]]>"
# YYYYMMDD[HHMMSS[.XXX]][[±H[H][.MM]][:TZ]]
_DATETIME = re.compile(
    r"(\d{4})(\d{2})(\d{2})"
    r"(?:(\d{2})(\d{2})(\d{2})(?:\.(\d{3}))?)?"
    r"(?:\[(?:([+-]?)(\d{1,2})(?:\.(\d{2}))?)?(?::[^\]]*)?\])?"
)
_CURRENCY = re.compile(r"[A-Z]{3}")
_STATEMENTS = frozenset({"STMTRS", "CCSTMTRS"})
# The OFX aggregates on the way to what is read, and those read. The spec closes every
# aggregate, so one left open is a broken file: read as an empty leaf, its contents would
# move into its parent, and a transaction or a balance would silently vanish.
_AGGREGATES = _STATEMENTS | {
    "OFX",
    "SIGNONMSGSRSV1",
    "SONRS",
    "STATUS",
    "BANKMSGSRSV1",
    "CREDITCARDMSGSRSV1",
    "STMTTRNRS",
    "CCSTMTTRNRS",
    "BANKACCTFROM",
    "CCACCTFROM",
    "BANKTRANLIST",
    "STMTTRN",
    "CURRENCY",
    "ORIGCURRENCY",
    "LEDGERBAL",
    "AVAILBAL",
    "PAYEE",
    "BANKACCTTO",
    "CCACCTTO",
}
# Real statements nest about eight deep; the bound keeps the recursive walks safe.
_MAX_DEPTH = 64


@dataclass
class _Aggregate:
    name: str
    leaves: list[tuple[str, str]] = field(default_factory=list[tuple[str, str]])
    children: list["_Aggregate"] = field(default_factory=list["_Aggregate"])

    def child(self, name: str) -> "_Aggregate | None":
        """The aggregate directly inside this one; one it has twice refuses the file, as a
        repeated leaf does (leaf)."""
        found = [child for child in self.children if child.name == name]
        if len(found) > 1:
            raise FileFormatError(f"<{self.name}> has <{name}> more than once")
        return found[0] if found else None

    def find(self, names: frozenset[str]) -> list["_Aggregate"]:
        """Every descendant with one of the names, in document order."""
        found: list[_Aggregate] = []
        for child in self.children:
            if child.name in names:
                found.append(child)
            found.extend(child.find(names))
        return found

    def leaf(self, name: str) -> str | None:
        """A leaf directly inside this aggregate, or None when missing or empty; one it has
        twice refuses the file (see parsed.single)."""
        values = [value for leaf, value in self.leaves if leaf == name]
        if len(values) > 1:
            raise FileFormatError(f"<{self.name}> gives {name} more than once")
        return (values[0] if values else None) or None

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
    head = content[: start.start()]
    # UTF-8 when the header says so, when a BOM opens the file, or when it is XML that
    # states no encoding (XML's default); otherwise SGML OFX's usual cp1252.
    utf8 = (
        _UTF8_HEADER.search(head) is not None
        or head.startswith(b"\xef\xbb\xbf")
        or (_XML_INSTRUCTION.search(head) is not None and _STATED_ENCODING.search(head) is None)
    )
    encoding = "utf-8-sig" if utf8 else "cp1252"
    try:
        text = content.decode(encoding)
    except UnicodeDecodeError:
        raise FileFormatError(f"the file is not valid {encoding.removesuffix('-sig')}") from None
    found = _START.search(text)
    assert found is not None
    return _without_cdata(text[found.start() :])


def _without_cdata(text: str) -> str:
    """Each CDATA section's text, escaped as ordinary text (values are unescaped once
    read), in one pass; one never closed refuses the file."""
    parts: list[str] = []
    position = 0
    while (opened := text.find(_CDATA_OPEN, position)) != -1:
        closed = text.find(_CDATA_CLOSE, opened)
        if closed == -1:
            raise FileFormatError("not a valid OFX file: a CDATA section is never closed")
        parts += [
            text[position:opened],
            html.escape(text[opened + len(_CDATA_OPEN) : closed], quote=False),
        ]
        position = closed + len(_CDATA_CLOSE)
    return "".join([*parts, text[position:]])


def _absorb(stack: list[_Aggregate]) -> None:
    """Close the innermost open tag that had no closing tag: it was an empty SGML leaf, so
    what was read inside it belongs to its parent."""
    node = stack.pop()
    if node.name in _AGGREGATES:
        raise FileFormatError(f"not a valid OFX file: <{node.name}> is never closed")
    parent = stack[-1]
    parent.children.pop()  # node: nothing is added to the parent while a child is open
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
            if not node.leaves and not node.children and node.name not in _AGGREGATES:
                _absorb(stack)  # XML's empty leaf, <MEMO></MEMO>
            else:
                stack.pop()
            continue
        text_value = value.strip()
        if text_value:
            unescaped = _ENTITY.sub(lambda entity: html.unescape(entity[0]), text_value)
            stack[-1].leaves.append((name, unescaped))
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
    """A transaction's one value for key (a repeated one is the row's error)."""
    return parsed.single(raw, key)


def _stated(values: dict[str, str], key: str, where: str) -> str | None:
    """A statement-level aggregate's one value for key (a repeated one refuses the file)."""
    try:
        return parsed.single(values, key)
    except ValueError as error:
        raise FileFormatError(f"{where}: {error}") from None


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
    corrected = _optional(raw, "CORRECTFITID")
    if corrected is not None or _optional(raw, "CORRECTACTION") is not None:
        # A correction deletes or replaces another transaction, perhaps one an earlier file
        # imported; booking it as a transaction of its own would count the money twice.
        raise ValueError(
            f"the transaction corrects transaction {corrected or '(unnamed)'}, and corrections "
            "aren't supported: export the account again once the bank has settled it"
        )
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
    # CURRENCY: the amounts are in that currency. ORIGCURRENCY only records the currency
    # TRNAMT was converted from, so the row stays in CURDEF (kept in raw).
    own_currency = _optional(raw, "CURRENCY/CURSYM")
    original = _optional(raw, "ORIGCURRENCY/CURSYM")
    if own_currency is not None and original is not None:
        raise ValueError("the transaction has both CURRENCY and ORIGCURRENCY")
    rate_key = "CURRENCY/CURRATE" if own_currency is not None else "ORIGCURRENCY/CURRATE"
    rate_text = _optional(raw, rate_key)
    rate = None if rate_text is None else _rate(rate_text, rate_key)
    # NAME, or the PAYEE aggregate the spec allows instead (its address stays in raw).
    payee = _optional(raw, "NAME") or _optional(raw, "PAYEE/NAME")
    memo = _optional(raw, "MEMO")
    return ParsedRow(
        position=position,
        booking_date=booking_date,
        occurred_at=occurred_at,
        time_unit="day" if occurred_at is None else "second",
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
        counterparty=_counterparty(raw),
        original_currency=None if original is None else _currency(original),
        exchange_rate=rate,
    )


def _rate(text: str, key: str) -> Decimal:
    try:
        rate = parse_amount(text, AmountFormat())
    except ValueError as error:
        raise ValueError(f"{key}: {error}") from None
    if rate <= 0:
        raise ValueError(f"{key}: {text!r} is not a rate")
    return rate


def _counterparty(raw: dict[str, str]) -> Counterparty | None:
    """The account paid to (BANKACCTTO, or CCACCTTO for a card), normalised as M4's
    registry expects. A value that isn't a UK sort code or account number has no typed
    home and stays in raw, as for BANKACCTFROM."""
    prefix = "BANKACCTTO" if any(key.startswith("BANKACCTTO/") for key in raw) else "CCACCTTO"
    sort_code: str | None = None
    account_number: str | None = None
    try:
        sort_code = normalise_sort_code(_optional(raw, f"{prefix}/BANKID"))
    except ValueError:
        pass
    try:
        account_number = normalise_account_number(_optional(raw, f"{prefix}/ACCTID"))
    except ValueError:
        pass
    counterparty = Counterparty(sort_code=sort_code, account_number=account_number)
    return None if counterparty == Counterparty() else counterparty


def _account(statement: _Aggregate) -> StatementAccount | None:
    source = statement.child("BANKACCTFROM") or statement.child("CCACCTFROM")
    if source is None:
        return None
    values = source.leaf_values()
    bank_id = _stated(values, "BANKID", source.name)
    account_id = _stated(values, "ACCTID", source.name)
    sort_code: str | None = None
    account_number: str | None = None
    try:
        # A UK BANKID is the sort code; other countries' bank IDs aren't six digits.
        sort_code = normalise_sort_code(bank_id)
    except ValueError:
        pass
    try:
        # Masked card numbers ("XXXX1234") have no typed home.
        account_number = normalise_account_number(account_id)
    except ValueError:
        pass
    if (
        sort_code is not None
        and account_number is not None
        and len(account_number) == 14
        and account_number.startswith(sort_code)
    ):
        # HSBC UK writes ACCTID as the sort code followed by the 8-digit account number.
        account_number = account_number[len(sort_code) :]
    account = StatementAccount(
        sort_code=sort_code,
        account_number=account_number,
        account_type=_stated(values, "ACCTTYPE", source.name),
    )
    return None if account == StatementAccount() else account


def _balance(
    statement: _Aggregate, name: str, config: OfxProfileConfig
) -> tuple[Decimal | None, date | None]:
    """LEDGERBAL or AVAILBAL: the amount in ledger sign and the written date of DTASOF."""
    aggregate = statement.child(name)
    if aggregate is None:
        return None, None
    values = aggregate.leaf_values()
    amount, as_of = _stated(values, "BALAMT", name), _stated(values, "DTASOF", name)
    if amount is None or as_of is None:
        raise FileFormatError(f"{name} needs both BALAMT and DTASOF")
    try:
        balance = _signed(parse_amount(amount, AmountFormat()), config)
        as_of_date, _ = _datetime(as_of, "DTASOF")
    except ValueError as error:
        raise FileFormatError(f"{name}: {error}") from None
    return balance, as_of_date


def _period(statement: _Aggregate) -> tuple[tuple[date, date] | None, bool]:
    """BANKTRANLIST's DTSTART and DTEND by their written dates, when it states both, and
    whether DTSTART gives a time of day other than midnight in its own offset (an
    incremental download starting partway through a day)."""
    transactions = statement.child("BANKTRANLIST")
    if transactions is None:
        return None, False
    start, end = transactions.leaf("DTSTART"), transactions.leaf("DTEND")
    if start is None or end is None:
        return None, False
    try:
        (first, starts_at), (last, _) = _datetime(start, "DTSTART"), _datetime(end, "DTEND")
    except ValueError as error:
        raise FileFormatError(f"BANKTRANLIST: {error}") from None
    if first > last:
        raise FileFormatError(f"BANKTRANLIST: DTSTART {start!r} is after DTEND {end!r}")
    return (first, last), starts_at is not None and starts_at.time() != time()


def _check_status(tree: _Aggregate) -> None:
    """Every STATUS (the sign-on's and the statement response's) must be CODE 0: anything
    else is the bank saying the response failed or is incomplete."""
    for status in tree.find(frozenset({"STATUS"})):
        code = status.leaf("CODE")
        if code != "0":
            severity = status.leaf("SEVERITY") or "no severity"
            raise FileFormatError(
                f"the bank's response has status {code or 'without a code'} ({severity}): "
                "export the statement again"
            )


def _generated_on(tree: _Aggregate) -> date | None:
    """The day the bank made the file (SONRS's DTSERVER), as written."""
    signon = tree.find(frozenset({"SONRS"}))
    written = signon[0].leaf("DTSERVER") if signon else None
    if written is None:
        return None
    try:
        return _datetime(written, "DTSERVER")[0]
    except ValueError as error:
        raise FileFormatError(f"SONRS: {error}") from None


@parsed.exact_decimals
def parse_ofx(content: bytes, config: OfxProfileConfig) -> ParsedStatement:
    tree = _tree(_decode(content))
    _check_status(tree)
    statements = tree.find(_STATEMENTS)
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
    closing_balance, closing_balance_date = _balance(statement, "LEDGERBAL", config)
    available_balance, available_balance_date = _balance(statement, "AVAILBAL", config)
    period, starts_mid_day = _period(statement)
    return ParsedStatement(
        rows=rows,
        closing_balance=closing_balance,
        closing_balance_date=closing_balance_date,
        period=period,
        period_starts_mid_day=starts_mid_day,
        available_balance=available_balance,
        available_balance_date=available_balance_date,
        account=_account(statement),
        currency=currency,
        document_date=_generated_on(tree),
    )
