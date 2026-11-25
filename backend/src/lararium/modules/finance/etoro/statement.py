"""Reads an eToro account statement (xlsx) into keyed rows (design §5.3).

Each row of each data sheet becomes a `StatementRow`, its cells exactly as text, keyed by the
column names of the sheet's header row; a label/value row of Account Summary keeps its label
under "Label" and its values under "Value", "Value 2", …. The Glossary is static text and is
not read.

A row's key (Decision 10) is the first 32 hex characters of the sha256 of a JSON array: the
sheet, the sheet's key columns (`fields.STATEMENT_SHEETS`), and the row's occurrence among
identical arrays of this statement, from 0. So the same row in two overlapping statements has
the same key. Account Summary and Financial Summary describe their statement's period, so the
period's Start Date and End Date as written follow the sheet in their key, then the row's label
(Account Summary) or Name (Financial Summary): a corrected statement of the same period gives
a changed figure the same key, so it replaces the old one.

Errors and flags name sheets, columns and fixed labels only, never a cell: Account Summary
holds the owner's name and username, and Details is free text.
"""

import hashlib
import json
import re
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from zoneinfo import ZoneInfo

from lararium.core.errors import DisplayableError
from lararium.modules.finance.connector_items import SHOWN_NAME
from lararium.modules.finance.etoro.fields import (
    ACCOUNT_SUMMARY,
    GLOSSARY,
    LABEL,
    STATEMENT_SHEETS,
    VALUE,
    StatementSheet,
)
from lararium.modules.finance.etoro.xlsx import (
    MAX_CELLS,
    SHORTER,
    Sheet,
    XlsxError,
    read_workbook,
)

# A label not in the field list has a figure: a value with a digit. A value of words only is a
# section's heading (the owner's statement has one whose label the probe did not show). The
# label's text is not named: it may not be eToro's.
UNKNOWN_LABEL = f"{ACCOUNT_SUMMARY}.<unknown label>"
_DIGIT = re.compile(r"[0-9]")

_MOMENT = re.compile(r"([0-9]{2})/([0-9]{2})/([0-9]{4}) ([0-9]{2}):([0-9]{2}):([0-9]{2})")
_CURRENCIES = re.compile(r"([A-Z]{3})(?:/([A-Z]{3}))?")
_DAY = re.compile(r"([0-9]{2})/([0-9]{2})/([0-9]{4})(?:$| )")
# Decision 11a: the statement's times are UTC.
STATEMENT_ZONE = ZoneInfo("UTC")


class StatementError(DisplayableError, ValueError):
    """The file is not an eToro account statement as known; the message names a sheet and a
    column or label only."""


@dataclass(frozen=True)
class StatementRow:
    sheet: str
    key: str
    cells: Mapping[str, str]


@dataclass(frozen=True)
class EtoroStatement:
    period_from: datetime
    period_to: datetime
    created: datetime
    username: str
    # Account Summary's "Currency" (design §5.3): `<account currency>` or
    # `<account currency>/<second currency>`, the second the one eToro also reports figures in.
    account_currency: str
    second_currency: str | None
    rows: tuple[StatementRow, ...]
    # "<sheet>.<column>", "<sheet>" for an unknown sheet with cells, or UNKNOWN_LABEL.
    flagged: frozenset[str]


def _blank(row: Sequence[str]) -> bool:
    return not any(cell.strip() for cell in row)


def _column_letter(index: int) -> str:
    letters = ""
    index += 1
    while index:
        index, rest = divmod(index - 1, 26)
        letters = chr(65 + rest) + letters
    return letters


def _key(parts: Sequence[str | int]) -> str:
    text = json.dumps(list(parts), ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(text.encode()).hexdigest()[:32]


def _label_rows(sheet: Sheet, spec: StatementSheet, flagged: set[str]) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    for row in sheet.rows:
        if _blank(row):
            continue
        label, *values = row
        cells = {LABEL: label, VALUE: values[0] if values else ""}
        cells.update((f"{VALUE} {n}", value) for n, value in enumerate(values[1:], start=2))
        if label.strip() not in spec.known and any(_DIGIT.search(v) for v in values):
            flagged.add(UNKNOWN_LABEL)
        out.append(cells)
    return out


def _table_rows(sheet: Sheet, spec: StatementSheet, flagged: set[str]) -> list[dict[str, str]]:
    rows = [row for row in sheet.rows if not _blank(row)]
    header = [name.strip() for name in rows[0]] if rows else []
    while header and not header[-1]:
        header.pop()
    for column in spec.required:
        if column not in header:
            raise StatementError(f"{sheet.name}.{column} is missing")
    counts = Counter(name for name in header if name)
    for name in header:
        if counts[name] > 1:
            # Named only when known: an unknown header is the file's text.
            if name in spec.known or name in spec.required:
                raise StatementError(f"{sheet.name}.{name} appears twice")
            raise StatementError(f"{sheet.name} has a column twice")
    # Every row is padded to the header, so the reader's cap is checked again on the padding.
    if len(header) * (len(rows) - 1) > MAX_CELLS:
        raise StatementError(f"{sheet.name} has more than {MAX_CELLS} cells{SHORTER}")

    def name_of(index: int) -> str:
        name = header[index] if index < len(header) else ""
        return name or f"column {_column_letter(index)}"

    # A cell past the header or under an empty header is flagged by its column's letter.
    flaggable = {
        index
        for index, name in enumerate(header)
        if not name or not (spec.open_columns or name in spec.known)
    }
    out: list[dict[str, str]] = []
    unshown: set[str] = set()
    for row in rows[1:]:
        cells = {name_of(index): "" for index in range(len(header))}
        for index, value in enumerate(row):
            if index < len(header) or value:
                cells[name_of(index)] = value
            if value.strip() and (index >= len(header) or index in flaggable):
                name = name_of(index)
                if SHOWN_NAME.fullmatch(name):
                    flagged.add(f"{sheet.name}.{name}")
                else:
                    unshown.add(name)
        out.append(cells)
    if unshown:
        flagged.add(f"{sheet.name}.<unknown column> ({len(unshown)})")
    return out


def read_moment(value: str, label: str, zone: ZoneInfo, sheet: str = ACCOUNT_SUMMARY) -> datetime:
    """A statement date-time (`dd/mm/yyyy HH:MM:SS`) in `zone`; the sheet and `label` (its
    label or column) name it in the error, never its value."""
    match = _MOMENT.fullmatch(value.strip())
    if match is not None:
        day, month, year, hour, minute, second = (int(part) for part in match.groups())
        try:
            return datetime(year, month, day, hour, minute, second, tzinfo=zone)
        except ValueError:
            pass
    raise StatementError(f"{sheet}.{label} is not a date-time (dd/mm/yyyy HH:MM:SS)")


def read_currencies(value: str) -> tuple[str, str | None]:
    """Account Summary's "Currency" (design §5.3): the Trading account's currency, and the
    second one eToro reports figures in, when the label names two (`USD/GBP`). Any other shape
    is refused, naming the label, never its value."""
    match = _CURRENCIES.fullmatch(value.strip().upper())
    if match is None:
        raise StatementError(
            f"{ACCOUNT_SUMMARY}.Currency is not a currency code, or two joined by a slash"
        )
    return match.group(1), match.group(2)


def read_day(value: str, label: str, sheet: str) -> date:
    """A statement date (`dd/mm/yyyy`, a time after it ignored)."""
    match = _DAY.match(value.strip())
    if match is not None:
        day, month, year = (int(part) for part in match.groups())
        try:
            return date(year, month, day)
        except ValueError:
            pass
    raise StatementError(f"{sheet}.{label} is not a date (dd/mm/yyyy)")


def _labels(rows: list[dict[str, str]], spec: StatementSheet) -> dict[str, str]:
    """The required labels' values, each present once and not empty."""
    found: dict[str, list[str]] = {}
    for cells in rows:
        found.setdefault(cells[LABEL].strip(), []).append(cells[VALUE].strip())
    values: dict[str, str] = {}
    for label in spec.required:
        given = found.get(label, [])
        if not given:
            raise StatementError(f"{ACCOUNT_SUMMARY}.{label} is missing")
        if len(given) > 1:
            raise StatementError(f"{ACCOUNT_SUMMARY}.{label} appears twice")
        if not given[0]:
            raise StatementError(f"{ACCOUNT_SUMMARY}.{label} is empty")
        values[label] = given[0]
    return values


def parse_statement(body: bytes, zone: ZoneInfo) -> EtoroStatement:
    """The statement's rows, keyed, and its period; its times are read in `zone`."""
    try:
        sheets = read_workbook(body)
    except XlsxError as error:
        raise StatementError(str(error)) from None

    flagged: set[str] = set()
    by_name: dict[str, Sheet] = {}
    unshown = 0
    for sheet in sheets:
        if sheet.name in STATEMENT_SHEETS:
            if sheet.name in by_name:
                raise StatementError(f"the statement has two {sheet.name} sheets")
            by_name[sheet.name] = sheet
        elif sheet.name != GLOSSARY and any(not _blank(row) for row in sheet.rows):
            if SHOWN_NAME.fullmatch(sheet.name):
                flagged.add(sheet.name)
            else:
                unshown += 1
    if unshown:
        flagged.add(f"<unknown sheet> ({unshown})")
    for name in STATEMENT_SHEETS:
        if name not in by_name:
            raise StatementError(f"the statement has no {name} sheet")

    cells_by_sheet: dict[str, list[dict[str, str]]] = {}
    for name, spec in STATEMENT_SHEETS.items():
        read = _label_rows if spec.layout == "labels" else _table_rows
        cells_by_sheet[name] = read(by_name[name], spec, flagged)

    labels = _labels(cells_by_sheet[ACCOUNT_SUMMARY], STATEMENT_SHEETS[ACCOUNT_SUMMARY])
    period_from = read_moment(labels["Start Date"], "Start Date", zone)
    period_to = read_moment(labels["End Date"], "End Date", zone)
    if period_from > period_to:
        raise StatementError(f"{ACCOUNT_SUMMARY}'s Start Date is after its End Date")
    created = read_moment(labels["Date Created"], "Date Created", zone)
    account_currency, second_currency = read_currencies(labels["Currency"])

    rows: list[StatementRow] = []
    seen: Counter[str] = Counter()
    for sheet in sheets:
        spec = STATEMENT_SHEETS.get(sheet.name)
        if spec is None:
            continue
        for cells in cells_by_sheet[sheet.name]:
            identity: list[str | int] = [sheet.name]
            if spec.per_period:
                identity.extend((labels["Start Date"], labels["End Date"]))
            # A period sheet's label or Name is stripped, as `_labels` reads it, so a re-issue
            # differing only in surrounding spaces keeps its key.
            identity.extend(
                cells[column].strip() if spec.per_period else cells[column] for column in spec.key
            )
            same = _key(identity)
            rows.append(StatementRow(sheet.name, _key([*identity, seen[same]]), cells))
            seen[same] += 1

    return EtoroStatement(
        period_from=period_from,
        period_to=period_to,
        created=created,
        username=labels["Username"],
        account_currency=account_currency,
        second_currency=second_currency,
        rows=tuple(rows),
        flagged=frozenset(flagged),
    )
