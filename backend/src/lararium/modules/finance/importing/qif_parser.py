"""Reads a QIF statement for one bank, card or cash account. Pure: no database."""

import re
from datetime import date, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from lararium.modules.finance.errors import FileFormatError
from lararium.modules.finance.importing import parsed
from lararium.modules.finance.importing.parsed import (
    ParsedRow,
    ParsedStatement,
    RowError,
    SkippedRow,
    clean_description,
    parse_amount,
)
from lararium.modules.finance.importing.profiles import AmountFormat, QifProfileConfig

PARSER_VERSION = "qif-1"

_HEADER = re.compile(r"!type: ?(bank|ccard|cash)", re.IGNORECASE)
# QIF writes amounts like "-1,234.56"; a "," is only ever a thousands separator.
_AMOUNT_FORMAT = AmountFormat(thousands_separator=",")

type _Record = list[tuple[str, str]]


def _records(content: bytes, config: QifProfileConfig) -> list[_Record]:
    """Each record's (tag, value) lines, without the header and empty records."""
    try:
        text = content.decode(config.encoding)
    except UnicodeDecodeError:
        raise FileFormatError(f"the file is not valid {config.encoding}") from None
    lines = [line.strip() for line in text.replace("\r", "").split("\n")]
    lines = [line for line in lines if line]
    if not lines or not _HEADER.fullmatch(lines[0]):
        raise FileFormatError("not a QIF file for a bank, card or cash account (!Type:Bank)")
    records: list[_Record] = []
    record: _Record = []
    for line in lines[1:]:
        if line.startswith("!"):
            raise FileFormatError(f"the file has another section ({line}): one account per file")
        if line == "^":
            if record:
                records.append(record)
            record = []
        else:
            record.append((line[0], line[1:].strip()))
    if record:  # the last record's "^" may be missing
        records.append(record)
    if not records:
        raise FileFormatError("no rows")
    if len(records) > parsed.MAX_IMPORT_ROWS:
        raise FileFormatError(f"the file has more than {parsed.MAX_IMPORT_ROWS} rows")
    return records


def _raw(record: _Record) -> dict[str, str]:
    """Every tag; a repeated one (address lines, splits) gets "#2", "#3"…"""
    raw: dict[str, str] = {}
    for tag, value in record:
        key, n = tag, 1
        while key in raw:
            n += 1
            key = f"{tag}#{n}"
        raw[key] = value
    return raw


def _optional(raw: dict[str, str], tag: str) -> str | None:
    return raw.get(tag) or None


def _date(text: str, config: QifProfileConfig, local_tz: ZoneInfo) -> date:
    try:
        return datetime.strptime(text, config.date_format).replace(tzinfo=local_tz).date()
    except ValueError:
        raise ValueError(
            f"the date {text!r} doesn't match the format {config.date_format!r}"
        ) from None


def _row(
    position: int, raw: dict[str, str], config: QifProfileConfig, local_tz: ZoneInfo
) -> ParsedRow:
    """Raises ValueError with a message for the row's error."""
    written_date = _optional(raw, "D")
    if written_date is None:
        raise ValueError("the record has no date (D)")
    tag = "T" if _optional(raw, "T") is not None else "U"
    amount = _optional(raw, tag)
    if amount is None:
        raise ValueError("the record has no amount (T)")
    booking_date = _date(written_date, config, local_tz)
    try:
        value = parse_amount(amount, _AMOUNT_FORMAT)
    except ValueError as error:
        raise ValueError(f"{tag}: {error}") from None
    payee, memo = _optional(raw, "P"), _optional(raw, "M")
    return ParsedRow(
        position=position,
        booking_date=booking_date,
        occurred_at=None,
        description=clean_description([part for part in (payee, memo) if part is not None]),
        quantity=value if config.sign == "inflow_positive" else Decimal(0) - value,
        fee=Decimal(0),
        currency=None,
        balance=None,
        external_id=None,
        bank_type=None,
        raw=raw,
        payee=payee,
        memo=memo,
        check_number=_optional(raw, "N"),
    )


def parse_qif(content: bytes, config: QifProfileConfig, local_tz: ZoneInfo) -> ParsedStatement:
    """local_tz: the profile's timezone when set; the caller resolves it."""
    rows: list[ParsedRow | RowError | SkippedRow] = []
    for position, record in enumerate(_records(content, config)):
        raw = _raw(record)
        try:
            rows.append(_row(position, raw, config, local_tz))
        except ValueError as error:
            rows.append(RowError(position=position, message=str(error), raw=raw))
    return ParsedStatement(rows=rows)
