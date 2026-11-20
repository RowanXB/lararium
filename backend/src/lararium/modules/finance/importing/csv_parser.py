"""Reads a CSV statement through a CSV parse profile. Pure: no database."""

import csv
import io
import re
from collections.abc import Iterable
from datetime import date, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from lararium.modules.finance.errors import EmptyStatementError, FileFormatError
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
    parse_amount,
)
from lararium.modules.finance.importing.profiles import CsvProfileConfig

PARSER_VERSION = "csv-1"

_CURRENCY = re.compile(r"[A-Z]{3}")


type _Lines = list[list[str]]


def _trimmed(line: list[str]) -> list[str]:
    end = len(line)
    while end and not line[end - 1].strip():
        end -= 1
    return line[:end]


def _raw(header: list[str], line: list[str], used: Iterable[str]) -> dict[str, str]:
    """The row's cells by header name. Only cells the file has are kept, less the empty
    ones under an unnamed header cell (a spreadsheet pads both with commas), so the raw row
    stays as large as the line; the mapped columns a short row leaves out are empty."""
    raw = keyed(
        (name, cell)
        for name, cell in zip(header, line, strict=False)
        if name.strip() or cell.strip()
    )
    for name in used:
        raw.setdefault(name, "")
    return raw


def _read_rows(content: bytes, config: CsvProfileConfig) -> tuple[list[str], _Lines, _Lines]:
    """The header, the data rows and the footer rows as cell lists, without all-empty lines.
    Trailing empty cells are left out of the rows and the footer: a spreadsheet pads its
    lines with commas, and a short row's mapped columns read as empty anyway."""
    try:
        text = content.decode(config.encoding)
    except UnicodeDecodeError:
        raise FileFormatError(f"the file is not valid {config.encoding}") from None
    stream = io.StringIO(text, newline="")
    for _ in range(config.skip_rows):
        stream.readline()
    reader = csv.reader(stream, delimiter=config.delimiter)
    try:
        lines = [line for line in reader if any(cell.strip() for cell in line)]
    except csv.Error as error:
        raise FileFormatError(f"the file is not valid CSV: {error}") from None
    header = config.header
    if header is None:
        if not lines:
            raise FileFormatError("no rows")
        header = lines.pop(0)
    lines = [_trimmed(line) for line in lines]
    footer: _Lines = []
    if config.skip_footer_rows:
        footer = lines[-config.skip_footer_rows :]
        lines = lines[: -config.skip_footer_rows]
    missing = [name for name in config.read_columns() if name not in header]
    if missing:
        raise FileFormatError(f"columns not in the file's header: {', '.join(missing)}")
    used = dict.fromkeys(config.read_columns())
    repeated = [name for name in used if header.count(name) > 1]
    if repeated:
        raise FileFormatError(f"columns named more than once in the header: {', '.join(repeated)}")
    # Lines that are all footer go to _check_footer first: a row in a file without the footer
    # isn't an empty statement.
    if not lines and not footer:
        raise EmptyStatementError()
    if len(lines) > parsed.MAX_IMPORT_ROWS:
        raise FileFormatError(f"the file has more than {parsed.MAX_IMPORT_ROWS} rows")
    return header, lines, footer


def _optional(raw: dict[str, str], column: str | None) -> str | None:
    """The cell stripped, or None when the column isn't mapped or the cell is empty."""
    if column is None:
        return None
    return raw[column].strip() or None


def _datetime(text: str, config: CsvProfileConfig, local_tz: ZoneInfo) -> datetime:
    """The wall-clock date and time as written, in local_tz."""
    try:
        return datetime.strptime(text.strip(), config.date_format).replace(tzinfo=local_tz)
    except ValueError:
        raise ValueError(
            f"the date {text!r} doesn't match the format {config.date_format!r}"
        ) from None


def _amount(raw: dict[str, str], column: str, config: CsvProfileConfig) -> Decimal:
    try:
        return parse_amount(raw[column], config.amount_format)
    except ValueError as error:
        raise ValueError(f"{column}: {error}") from None


def _optional_amount(
    raw: dict[str, str], column: str | None, config: CsvProfileConfig
) -> Decimal | None:
    if column is None or not raw[column].strip():
        return None
    return _amount(raw, column, config)


def _quantity(raw: dict[str, str], config: CsvProfileConfig) -> Decimal:
    columns = config.columns
    if columns.amount is not None:
        if not raw[columns.amount].strip():
            raise ValueError(f"the amount ({columns.amount}) is empty")
        amount = _amount(raw, columns.amount, config)
        return amount if config.sign == "inflow_positive" else Decimal(0) - amount
    assert columns.inflow is not None and columns.outflow is not None
    inflow = _unsigned(raw, columns.inflow, config)
    outflow = _unsigned(raw, columns.outflow, config)
    if inflow is None and outflow is None:
        raise ValueError(f"both {columns.inflow} and {columns.outflow} are empty")
    return (inflow or Decimal(0)) - (outflow or Decimal(0))


def _unsigned(raw: dict[str, str], column: str, config: CsvProfileConfig) -> Decimal | None:
    """An inflow or outflow cell (Decision 2: the column itself is the sign). A negative
    value is an error rather than flipping the row's direction."""
    amount = _optional_amount(raw, column, config)
    if amount is not None and amount < 0:
        raise ValueError(
            f"{column}: {raw[column].strip()!r} is negative, but the column holds unsigned "
            "amounts; map a signed amount column instead"
        )
    return amount


def _currency(raw: dict[str, str], config: CsvProfileConfig) -> str | None:
    text = _optional(raw, config.columns.currency)
    if text is None:
        return None
    code = text.upper()
    if not (code.isascii() and _CURRENCY.fullmatch(code)):
        raise ValueError(f"not a currency code: {text!r}")
    return code


def _value_date(raw: dict[str, str], config: CsvProfileConfig, local_tz: ZoneInfo) -> date | None:
    text = _optional(raw, config.columns.value_date)
    return None if text is None else _datetime(text, config, local_tz).date()


def _extracted(raw: dict[str, str], config: CsvProfileConfig) -> dict[str, str]:
    """The fields the profile's extracts read out of the row's non-empty cells."""
    values: dict[str, str] = {}
    for extract in config.extract:
        cell = raw[extract.column].strip()
        if cell:
            values |= parsed.extract_fields(extract.patterns, cell, extract.column)
    return values


def _field(
    raw: dict[str, str], column: str | None, extracted: dict[str, str], name: str
) -> str | None:
    """A typed field from its mapped column, else from an extract (never both: profiles)."""
    return _optional(raw, column) or extracted.get(name)


def _counterparty(
    raw: dict[str, str], config: CsvProfileConfig, extracted: dict[str, str]
) -> Counterparty | None:
    columns = config.columns
    counterparty = Counterparty(
        name=_field(raw, columns.counterparty_name, extracted, "counterparty_name"),
        sort_code=_field(raw, columns.counterparty_sort_code, extracted, "counterparty_sort_code"),
        account_number=_field(
            raw, columns.counterparty_account_number, extracted, "counterparty_account_number"
        ),
        reference=_field(raw, columns.reference, extracted, "reference"),
        bank=extracted.get("counterparty_bank"),
        account_ending=extracted.get("counterparty_account_ending"),
    )
    return None if counterparty == Counterparty() else counterparty


def _account(raw: dict[str, str], config: CsvProfileConfig) -> StatementAccount | None:
    account = StatementAccount(
        sort_code=_optional(raw, config.columns.account_sort_code),
        account_number=_optional(raw, config.columns.account_number),
    )
    return None if account == StatementAccount() else account


def _row(
    position: int, raw: dict[str, str], config: CsvProfileConfig, local_tz: ZoneInfo
) -> tuple[ParsedRow, StatementAccount | None]:
    """Raises ValueError with a message for the row's error."""
    columns = config.columns
    when = _datetime(raw[columns.date], config, local_tz)
    extracted = _extracted(raw, config)
    row = ParsedRow(
        position=position,
        booking_date=when.date(),
        occurred_at=when if config.has_time else None,
        description=clean_description([raw[column] for column in columns.description]),
        quantity=_quantity(raw, config),
        fee=_optional_amount(raw, columns.fee, config) or Decimal(0),
        currency=_currency(raw, config) or config.currency,
        balance=_optional_amount(raw, columns.balance, config),
        external_id=_optional(raw, columns.external_id),
        bank_type=_optional(raw, columns.type),
        raw=raw,
        value_date=_value_date(raw, config, local_tz),
        payee=_field(raw, columns.payee, extracted, "payee"),
        memo=_field(raw, columns.memo, extracted, "memo"),
        check_number=_field(raw, columns.check_number, extracted, "check_number"),
        counterparty=_counterparty(raw, config, extracted),
    )
    return row, _account(raw, config)


def _excluded(raw: dict[str, str], config: CsvProfileConfig) -> str | None:
    """Why the include_only filter leaves the row out, or None when it keeps it."""
    for column, allowed in config.include_only.items():
        value = raw[column].strip()
        if value not in allowed:
            return f"{column} is {value}"
    return None


def _check_footer(
    header: list[str],
    footer: _Lines,
    used: list[str],
    config: CsvProfileConfig,
    local_tz: ZoneInfo,
) -> None:
    """skip_footer_rows drops the last lines unread, so one that reads as a transaction
    means the file has no footer (an account without an overdraft line, say): refuse it
    rather than lose that transaction. A transaction whose extract doesn't match is one
    still, so extracts are left out here."""
    plain = config.model_copy(update={"extract": []})
    for line in footer:
        if len(line) > len(header):
            continue
        try:
            _row(0, _raw(header, line, used), plain, local_tz)
        except ValueError:
            continue
        raise FileFormatError(
            f"a line the profile skips as a footer ({config.skip_footer_rows} at the end) "
            "reads as a transaction: this file has no such footer"
        )


def _overdraft(
    footer: _Lines, config: CsvProfileConfig, local_tz: ZoneInfo
) -> tuple[Decimal | None, date | None]:
    """The arranged overdraft limit and its date from the labelled footer line, if any."""
    label = config.overdraft_footer_label
    if label is None:
        return None, None
    for line in footer:
        if line[0].strip() != label.strip():
            continue
        try:
            limit = parse_amount(line[-1], config.amount_format)
        except ValueError as error:
            raise FileFormatError(f"the {label!r} line has no amount: {error}") from None
        for cell in line[1:]:
            try:
                return limit, _datetime(cell, config, local_tz).date()
            except ValueError:
                continue
        raise FileFormatError(
            f"the {label!r} line has no date in the format {config.date_format!r}"
        )
    return None, None


@parsed.exact_decimals
def parse_csv(content: bytes, config: CsvProfileConfig, local_tz: ZoneInfo) -> ParsedStatement:
    """local_tz: the profile's timezone when set; the caller resolves it."""
    header, lines, footer = _read_rows(content, config)
    used = config.read_columns()
    _check_footer(header, footer, used, config, local_tz)
    if not lines:
        raise EmptyStatementError()
    overdraft_limit, overdraft_limit_date = _overdraft(footer, config, local_tz)
    rows: list[ParsedRow | RowError | SkippedRow] = []
    accounts: set[StatementAccount] = set()
    for position, line in enumerate(lines):
        # A short row is read as it is (see _read_rows). A repeated header name keeps every
        # column ("Note", "Note#2").
        raw = _raw(header, line, used)
        if len(line) > len(header):
            message = f"the row has {len(line)} cells, the header {len(header)}"
            rows.append(RowError(position=position, message=message, raw=raw))
            continue
        reason = _excluded(raw, config)
        if reason is not None:
            rows.append(SkippedRow(position=position, reason=reason, raw=raw))
            continue
        try:
            row, account = _row(position, raw, config, local_tz)
        except ValueError as error:
            rows.append(RowError(position=position, message=str(error), raw=raw))
            continue
        rows.append(row)
        if account is not None:
            accounts.add(account)
    if len(accounts) > 1:
        raise FileFormatError("export one account per file")
    return ParsedStatement(
        rows=rows,
        account=next(iter(accounts), None),
        overdraft_limit=overdraft_limit,
        overdraft_limit_date=overdraft_limit_date,
    )
