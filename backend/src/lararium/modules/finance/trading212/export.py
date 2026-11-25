"""Trading 212's CSV export, uploaded to the account's connection (design §3a, §5.3a).

**Optional.** Nothing depends on it: the API alone posts, reconciles and pairs every record,
and nothing asks for an export. An upload adds what the API lacks: the deposit's card
"Transaction ID" in `Notes`, and any deposit or withdrawal the API skipped (merge.py).

The owner downloads it in the app (History → export); Lararium never asks Trading 212 for one
(`POST /history/exports` is a write). The file is read as UTF-8, with or without a byte-order
mark. Its columns are checked against the audited ones (fields.py `EXPORT_*`): a required one
missing refuses it, naming the column; any other with a value is flagged by its header and
kept in the raw record. Each row is one raw record of its cells as text, keyed by `ID`; the
same row in two exports is one record. Messages name a column or a rule, never a cell's value.
"""

import asyncio
import csv
import io
import re
import threading
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy import text

from lararium.core.connections import get_connection, lock_connection, merge_cursor
from lararium.core.connectors import SyncContext
from lararium.core.documents import record_document
from lararium.core.errors import DisplayableError
from lararium.core.json_types import JSONValue
from lararium.core.raw_records import store_raw_record
from lararium.modules.finance.connector_items import SHOWN_NAME
from lararium.modules.finance.report_sightings import first_sighting
from lararium.modules.finance.trading212.accounts import mapped_id
from lararium.modules.finance.trading212.fields import (
    ENDPOINTS,
    EXPORT_KEY,
    EXPORT_KNOWN,
    EXPORT_RECORD_TYPE,
    EXPORT_REQUIRED,
)
from lararium.modules.finance.trading212.normalize import normalize
from lararium.modules.finance.trading212.sync import (
    EXPORT,
    FIELDS,
    INSTRUMENT_TYPES,
    VERSION,
    publish_flags,
)
from lararium.modules.finance.typed_tables import TypedValueError, parse_decimal

DOCUMENT_SOURCE = "finance.trading212"
CSV_TYPE = "text/csv"
# The most rows, and the longest cell, a file may hold: a decade of daily activity is far
# fewer rows; a spreadsheet cell holds at most 32,767 characters.
MAX_ROWS = 200_000
MAX_CELL = 32_767
_TIME = re.compile(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\+00:00")
_CURRENCY = re.compile(r"[A-Z]{3}")


class ExportError(DisplayableError, ValueError):
    """The upload cannot be read as an export of this connection's account; the message names
    a column or a rule, never a value (422)."""


class ExportTooEarly(DisplayableError):
    """An export uploaded before the connection's first sync has finished (409): its checks
    need the account and records that sync stores."""


@dataclass(frozen=True)
class ExportRow:
    key: str
    cells: Mapping[str, str]


@dataclass(frozen=True)
class IngestResult:
    raw_record_ids: list[UUID]
    # The headers of the columns not known that held a value.
    flagged: frozenset[str]
    # The export's document, whose bytes the caller writes last (record_document).
    document_sha256: str


def _decode(body: bytes) -> str:
    try:
        decoded = body.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise ExportError("the export is not UTF-8 text") from None
    if "\x00" in decoded:
        raise ExportError("the export is not a readable CSV file")
    return decoded


def _cell(value: str) -> str:
    if len(value) > MAX_CELL:
        raise ExportError(f"a cell of the export is longer than {MAX_CELL} characters")
    return value


def _header(names: list[str]) -> list[str]:
    """The header's names. The required columns are checked first, and a refusal names only
    a known column: a file without a header has a data row here, whose cells are values."""
    header = [_cell(name).strip() or f"(column {n + 1})" for n, name in enumerate(names)]
    for name in EXPORT_REQUIRED:
        if name not in header:
            raise ExportError(f"the export lacks its {name} column")
    if len(set(header)) != len(header):
        raise ExportError("the export lists a column twice")
    return header


def _check_row(cells: Mapping[str, str]) -> None:
    """A row's required values are there, and those that post read as what they are."""
    for name in EXPORT_REQUIRED:
        if not cells.get(name, "").strip():
            raise ExportError(f"a row of the export lacks its {name}")
    moment = cells["Time (UTC)"].strip()
    try:
        if _TIME.fullmatch(moment) is None:
            raise ValueError
        datetime.fromisoformat(moment)
    except ValueError:
        raise ExportError("the export's Time (UTC) is not YYYY-MM-DD HH:MM:SS+00:00") from None
    try:
        parse_decimal(cells["Total"], "Total")
    except TypedValueError:
        raise ExportError("the export's Total is not a number") from None
    if _CURRENCY.fullmatch(cells["Currency (Total)"].strip()) is None:
        raise ExportError("the export's Currency (Total) is not a currency code")


def parse_export(body: bytes) -> tuple[tuple[ExportRow, ...], frozenset[str]]:
    """The export's rows, once each, and the headers of the columns not known that hold a
    value. A row repeated unchanged is one row; one `ID` on two different rows refuses the
    file (Decision 9)."""
    reader = csv.reader(io.StringIO(_decode(body), newline=""), strict=True)
    rows: dict[str, ExportRow] = {}
    flagged: set[str] = set()
    try:
        first = next(reader, None)
        if first is None:
            raise ExportError("the export has no header")
        header = _header(first)
        count = 0
        for line in reader:
            if not any(cell.strip() for cell in line):
                continue
            count += 1
            if count > MAX_ROWS:
                raise ExportError(f"the export has more than {MAX_ROWS} rows")
            if any(cell.strip() for cell in line[len(header) :]):
                raise ExportError("a row of the export has more cells than its header")
            values = [_cell(cell) for cell in line[: len(header)]]
            values += [""] * (len(header) - len(values))
            cells: dict[str, str] = {}
            for name, value in zip(header, values, strict=True):
                if name in EXPORT_KNOWN:
                    cells[name] = value
                elif value.strip():
                    cells[name] = value
                    flagged.add(name)
            _check_row(cells)
            key = cells[EXPORT_KEY].strip()
            held = rows.get(key)
            if held is not None and held.cells != cells:
                raise ExportError("the export has two different rows with one ID")
            rows[key] = ExportRow(key, cells)
    except csv.Error:
        raise ExportError("the export is not a readable CSV file") from None
    return tuple(rows.values()), frozenset(flagged)


# An export is decoded in full in memory, so it is parsed off the event loop, one at a time,
# as the eToro statement is.
_PARSING = threading.Lock()


def _parse_alone(body: bytes) -> tuple[tuple[ExportRow, ...], frozenset[str]]:
    with _PARSING:
        return parse_export(body)


async def parse_upload(body: bytes) -> tuple[tuple[ExportRow, ...], frozenset[str]]:
    """Read an uploaded export, off the event loop. Needs no database: a caller may parse
    before it takes any lock."""
    return await asyncio.to_thread(_parse_alone, body)


async def _check(ctx: SyncContext, rows: Sequence[ExportRow]) -> None:
    """The first sync has finished, and no `ID` is one the API's records place on another
    Trading 212 account only (design §5.3a): one this connection's records also hold is its
    own, as a transfer between the owner's Invest and ISA accounts may share a reference. An
    export whose IDs no record knows is accepted: it may predate the API's history."""
    session = ctx.command.session
    summary = ENDPOINTS["summary"].record_type
    if await first_sighting(session, ctx.connection.id, summary) is None:
        raise ExportTooEarly("wait for the Trading 212 connection's first sync to finish")
    references = sorted({row.key for row in rows})
    if not references:
        return
    connection = await get_connection(session, ctx.connection.id)
    elsewhere = await session.scalar(
        text(
            "select exists (select 1 from ("
            " select connection_id, reference from finance.trading212_transactions"
            " union all select connection_id, reference from finance.trading212_dividends) r"
            " join core.connections c on c.id = r.connection_id"
            " where r.connection_id <> :c and r.reference = any(cast(:refs as text[]))"
            " and not coalesce(jsonb_exists(c.config -> 'account_map', :mine), false)"
            " and not exists (select 1 from finance.trading212_transactions o"
            " where o.connection_id = :c and o.reference = r.reference)"
            " and not exists (select 1 from finance.trading212_dividends o"
            " where o.connection_id = :c and o.reference = r.reference))"
        ),
        {"c": ctx.connection.id, "refs": references, "mine": mapped_id(connection.config)},
    )
    if elsewhere:
        raise ExportError("this export is of another Trading 212 account")


async def ingest_export(
    ctx: SyncContext,
    body: bytes,
    filename: str,
    parsed: tuple[tuple[ExportRow, ...], frozenset[str]] | None = None,
) -> IngestResult:
    """Check an uploaded export, record it as a document and store one raw record per row,
    then normalise those records and publish the reader's and the normalizer's flags.
    `parsed` is `body` as `parse_upload` read it, when the caller has already. Each upload
    replaces the export column flags: they name the unknown columns of the latest upload.

    The document's bytes are not written here: the caller writes them
    (`store.put(result.document_sha256, body)`) once the rest of its command is done, just
    before it commits (record_document), so a command that fails leaves no file behind."""
    rows, flagged = parsed if parsed is not None else await parse_upload(body)
    # Locked from here until the upload commits: a sync normalizes the same connection one at
    # a time with it, and the flags read here are the ones replaced; only the flag keys are
    # merged, so no other cursor key is overwritten.
    await lock_connection(ctx.command, ctx.connection.id)
    await _check(ctx, rows)
    document = await record_document(
        ctx.command, body, filename=filename, media_type=CSV_TYPE, source=DOCUMENT_SOURCE
    )
    request: dict[str, JSONValue] = {"upload": filename, "document_sha256": document.sha256}
    ids: list[UUID] = []
    for row in rows:
        content: JSONValue = {"cells": dict(row.cells)}
        ids.append(
            await store_raw_record(
                ctx.command,
                connection_id=ctx.connection.id,
                record_type=EXPORT_RECORD_TYPE,
                request=request,
                content=content,
                connector_version=VERSION,
                sync_id=ctx.sync_id,
            )
        )
    ids = list(dict.fromkeys(ids))
    report = await normalize(ctx, ids)
    shown = {header for header in flagged if SHOWN_NAME.fullmatch(header)}
    found = {(EXPORT, f"export column {header}") for header in shown}
    if unshown := len(flagged - shown):
        found.add((EXPORT, f"export column <unknown column> ({unshown})"))
    found |= {(FIELDS, flag) for flag in report.flagged}
    found |= {(INSTRUMENT_TYPES, flag) for flag in report.instrument_flags}
    checked = {EXPORT, INSTRUMENT_TYPES} | ({FIELDS} if report.replayed else set[str]())
    flags = await publish_flags(ctx, found, checked)
    current = await get_connection(ctx.command.session, ctx.connection.id)
    kept = current.cursor if isinstance(current.cursor, dict) else {}
    if any(kept.get(key) != value for key, value in flags.items()):
        await merge_cursor(ctx.command, ctx.connection.id, flags)
    return IngestResult(ids, flagged, document.sha256)
