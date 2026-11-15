"""Reads a CSV statement through a CSV parse profile. Pure: no database."""

import csv
import io
import re
from datetime import date, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

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
    parse_amount,
)
from lararium.modules.finance.importing.profiles import CsvProfileConfig

PARSER_VERSION = "csv-1"

_CURRENCY = re.compile(r"[A-Z]{3}")


def _read_rows(content: bytes, config: CsvProfileConfig) -> tuple[list[str], list[list[str]]]:
    """The header and the data rows as cell lists, without all-empty lines and the footer."""
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
    if config.skip_footer_rows:
        lines = lines[: -config.skip_footer_rows]
    missing = [
        name for name in [*config.columns.names(), *config.include_only] if name not in header
    ]
    if missing:
        raise FileFormatError(f"columns not in the file's header: {', '.join(missing)}")
    if not lines:
        raise FileFormatError("no rows")
    if len(lines) > parsed.MAX_IMPORT_ROWS:
        raise FileFormatError(f"the file has more than {parsed.MAX_IMPORT_ROWS} rows")
    return header, lines


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
    inflow = _optional_amount(raw, columns.inflow, config)
    outflow = _optional_amount(raw, columns.outflow, config)
    if inflow is None and outflow is None:
        raise ValueError(f"both {columns.inflow} and {columns.outflow} are empty")
    return (inflow or Decimal(0)) - (outflow or Decimal(0))


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


def _counterparty(raw: dict[str, str], config: CsvProfileConfig) -> Counterparty | None:
    columns = config.columns
    counterparty = Counterparty(
        name=_optional(raw, columns.counterparty_name),
        sort_code=_optional(raw, columns.counterparty_sort_code),
        account_number=_optional(raw, columns.counterparty_account_number),
        reference=_optional(raw, columns.reference),
    )
    return None if counterparty == Counterparty() else counterparty


def _account(raw: dict[str, str], config: CsvProfileConfig) -> StatementAccount | None:
    account = StatementAccount(
        sort_code=_optional(raw, config.columns.account_sort_code),
        account_number=_optional(raw, config.columns.account_number),
    )
    return None if account == StatementAccount(None, None) else account


def _row(
    position: int, raw: dict[str, str], config: CsvProfileConfig, local_tz: ZoneInfo
) -> tuple[ParsedRow, StatementAccount | None]:
    """Raises ValueError with a message for the row's error."""
    columns = config.columns
    when = _datetime(raw[columns.date], config, local_tz)
    row = ParsedRow(
        position=position,
        booking_date=when.date(),
        occurred_at=when if config.has_time else None,
        description=clean_description([raw[column] for column in columns.description]),
        quantity=_quantity(raw, config),
        fee=_optional_amount(raw, columns.fee, config) or Decimal(0),
        currency=_currency(raw, config),
        balance=_optional_amount(raw, columns.balance, config),
        external_id=_optional(raw, columns.external_id),
        bank_type=_optional(raw, columns.type),
        raw=raw,
        value_date=_value_date(raw, config, local_tz),
        payee=_optional(raw, columns.payee),
        memo=_optional(raw, columns.memo),
        check_number=_optional(raw, columns.check_number),
        counterparty=_counterparty(raw, config),
    )
    return row, _account(raw, config)


def _excluded(raw: dict[str, str], config: CsvProfileConfig) -> str | None:
    """Why the include_only filter leaves the row out, or None when it keeps it."""
    for column, allowed in config.include_only.items():
        value = raw[column].strip()
        if value not in allowed:
            return f"{column} is {value}"
    return None


def parse_csv(content: bytes, config: CsvProfileConfig, local_tz: ZoneInfo) -> ParsedStatement:
    """local_tz: the profile's timezone when set; the caller resolves it."""
    header, lines = _read_rows(content, config)
    rows: list[ParsedRow | RowError | SkippedRow] = []
    accounts: set[StatementAccount] = set()
    for position, line in enumerate(lines):
        # Short rows are padded: a trailing empty cell is often left out.
        raw = dict(zip(header, line + [""] * (len(header) - len(line)), strict=False))
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
    return ParsedStatement(rows=rows, account=next(iter(accounts), None))
