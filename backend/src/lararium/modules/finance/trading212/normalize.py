"""Trading 212 raw records into the ledger (design §5.5-5.11).

Deterministic from its inputs and versioned (NORMALIZER_VERSION, stored in the connection's
config, Decision 8): when the stored version differs, the run replays every raw record the
connection holds (the latest copy of each history and metadata item, and every sighting of a
report, report_sightings), so what a new version stores is filled in for what an older one
saw, dated as it was then. A run otherwise applies the records it was given and its own
sync's sightings.

In order: the account (accounts.py); instrument and exchange metadata (reports.py); order
fills, dividends and cash movements as connector transactions, with the optional CSV export's
rows (merge.py: they enrich the API's movements, and post a deposit or withdrawal the API
lacks), then the opening adjustments (records.py); the summaries, positions, pending orders
and pies, typed per day, with reported snapshots, and prices (reports.py); reconciliation of
this sync's report, at the time its summary arrived (reconcile.py).

Every record's fields are checked first: a required one missing fails the run; an unknown one
with a value is flagged. Flags (unknown fields, unknown values of the enums that decide
postings, types of instrument the ledger does not hold, a TRANSFER) are reported for the sync
to publish, as are the instruments still without a type, which every run finds in full.
"""

from collections import defaultdict
from collections.abc import Sequence
from decimal import MAX_PREC, localcontext
from uuid import UUID

from lararium.core import models as core_models
from lararium.core.connections import get_connection, merge_cursor, update_config
from lararium.core.connectors import SyncContext
from lararium.core.json_types import JSONValue
from lararium.modules.finance import source_merge
from lararium.modules.finance.accounts import get_account
from lararium.modules.finance.connector_items import lacks, leaves
from lararium.modules.finance.report_sightings import list_sightings
from lararium.modules.finance.trading212 import accounts, merge, reconcile, records, reports
from lararium.modules.finance.trading212.fields import ENDPOINTS, MAP_ENTRIES
from lararium.modules.finance.trading212.instruments import type_flags
from lararium.modules.finance.trading212.merge import ExportRecord
from lararium.modules.finance.trading212.state import (
    NormalizeReport,
    Record,
    Report,
    Run,
    Trading212DataError,
    record_key,
    records_of,
    reports_of,
    stored,
    text_at,
)

__all__ = ["NORMALIZER_VERSION", "NormalizeReport", "Trading212DataError", "normalize"]

NORMALIZER_VERSION = "trading212-norm-1"
# The version that last normalized the connection, in its config (Decision 8).
NORMALIZER_VERSION_KEY = "normalizer_version"
# Items a key names once per connection, the ledger's (Decision 9) and the metadata's: the
# latest copy of each is what counts. The others are reports, each dated by its fetch.
_LEDGER = ("orders", "dividends", "transactions")
_KEYED = (*_LEDGER, "instruments", "exchanges")
_REPORTS = ("summary", "positions", "pending_orders", "pies", "pie")
# What each ledger key is, as an error names it.
_KEY_KIND = {"orders": "fill or order id", "dividends": "reference", "transactions": "reference"}


def _check(run: Run, record: Record) -> None:
    """Required fields fail the run when missing; unknown ones with a value are flagged."""
    spec = ENDPOINTS[record.endpoint]
    for path in sorted(spec.required):
        if lacks(record.content, path.split("."), MAP_ENTRIES):
            raise Trading212DataError(f"{spec.record_type} lacks {path}")
    for path in sorted(set(leaves(record.content, "", MAP_ENTRIES)) - spec.known):
        run.flag(f"{record.endpoint}.{path}")


async def _records(
    ctx: SyncContext, raw_record_ids: Sequence[UUID], replay: bool
) -> tuple[list[Record], list[Report]]:
    """(the keyed items, one per key; the reports). The reports are this sync's sightings;
    with `replay`, every sighting the connection has, and the latest copy of every keyed item.

    One ledger key with two contents in one run fails it, naming the record type and the
    key's kind (Decision 9): an item repeated unchanged is one raw record."""
    session = ctx.command.session
    keyed: dict[tuple[str, str], Record] = {}
    for record in sorted(
        await records_of(session, raw_record_ids), key=lambda r: (r.fetched_at, r.id)
    ):
        if record.endpoint in _REPORTS:
            continue  # a report is read from its sighting
        key = (record.endpoint, record_key(record))
        held = keyed.get(key)
        if held is not None and record.endpoint in _LEDGER and held.content != record.content:
            raise Trading212DataError(
                f"{record.record_type} has two different items with one"
                f" {_KEY_KIND[record.endpoint]}"
            )
        keyed[key] = record
    if replay:
        given = set(keyed)
        for record in await stored(session, ctx.connection.id, _KEYED):
            if (key := (record.endpoint, record_key(record))) not in given:
                keyed[key] = record  # oldest first, so the latest copy stays
    sightings = await list_sightings(
        session, ctx.connection.id, sync_id=None if replay else ctx.sync_id
    )
    return list(keyed.values()), await reports_of(session, sightings)


async def _movements(
    run: Run, given: list[Record], raw_record_ids: Sequence[UUID], replay: bool
) -> tuple[list[Record], dict[str, ExportRecord]]:
    """The API's cash movements to apply, and the export's rows (merge), by reference.

    The movements given, the TRANSFERs to name their sibling in, and the latest stored copy
    of each one an export row given (or an owner's decision) concerns; the export rows given,
    and the stored one of each movement applied. A replay has every stored record of both."""
    keys = {record_key(r) for r in given}
    movements = given + [
        r for r in await records.unidentified_transfers(run) if record_key(r) not in keys
    ]
    if replay:
        return movements, await merge.stored(run, None)
    exports = await merge.given(run, raw_record_ids)
    references = {text_at(r.content, "reference") for r in movements}
    decided = {
        key.removeprefix(merge.TRANSACTION)
        for key in await source_merge.decided_keys(
            run.session, run.connection_id, merge.TRANSACTION
        )
    }
    wanted = (set(exports) | decided) - references
    if wanted:
        latest: dict[str, Record] = {}
        for record in await stored(
            run.session,
            run.connection_id,
            ["transactions"],
            core_models.RawRecord.content["reference"].astext.in_(sorted(wanted)),
        ):
            latest[text_at(record.content, "reference")] = record
        movements += list(latest.values())
        references |= set(latest)
    exports |= await merge.stored(run, (references | decided) - set(exports))
    return movements, exports


def _tickers(by_endpoint: dict[str, list[Record]], reports_: Sequence[Report]) -> set[str]:
    found = {text_at(r.content, "order.instrument.ticker") for r in by_endpoint["orders"]}
    found |= {text_at(r.content, "instrument.ticker") for r in by_endpoint["dividends"]}
    found |= {
        text_at(r.content, "instrument.ticker") for r in reports_ if r.endpoint == "positions"
    }
    return found - {""}


async def normalize(ctx: SyncContext, raw_record_ids: Sequence[UUID]) -> NormalizeReport:
    """Apply raw records, and this sync's sightings of reports (report_sightings), to the
    ledger.

    Its arithmetic on amounts is exact, as the ledger's balance check is: the default context
    rounds to 28 digits, which could leave postings that do not balance."""
    with localcontext(prec=MAX_PREC):
        return await _normalize(ctx, list(dict.fromkeys(raw_record_ids)))


async def _normalize(ctx: SyncContext, raw_record_ids: list[UUID]) -> NormalizeReport:
    connection = await get_connection(ctx.command.session, ctx.connection.id)
    replay = connection.config.get(NORMALIZER_VERSION_KEY) != NORMALIZER_VERSION
    keyed, seen = await _records(ctx, raw_record_ids, replay)
    report = NormalizeReport(replayed=replay)
    run = Run(ctx, connection, report)
    by_endpoint: defaultdict[str, list[Record]] = defaultdict(list)
    for record in sorted(keyed, key=lambda r: (record_key(r), r.fetched_at, r.id)):
        _check(run, record)
        by_endpoint[record.endpoint].append(record)
    for record in {r.record.id: r.record for r in seen}.values():
        _check(run, record)

    own = [r for r in seen if r.sync_id == ctx.sync_id]
    summaries = [r for r in own if r.endpoint == "summary"]
    summary = summaries[-1] if summaries else None
    latest = [r for r in seen if r.endpoint == "summary"]
    await accounts.account(run, (summary or latest[-1]).record if summary or latest else None)

    unsupported = await reports.instruments(run, by_endpoint["instruments"], replay)
    await reports.exchanges(run, by_endpoint["exchanges"], replay)
    await records.void_unsupported(run, unsupported)
    await run.load_metadata(_tickers(by_endpoint, seen))
    for record in sorted(by_endpoint["orders"], key=records.order_time):
        await records.order(run, record)
    for record in sorted(
        by_endpoint["dividends"], key=lambda r: (text_at(r.content, "paidOn"), record_key(r))
    ):
        await records.dividend(run, record)
    movements, exports = await _movements(run, by_endpoint["transactions"], raw_record_ids, replay)
    for record in sorted(movements, key=lambda r: (text_at(r.content, "dateTime"), record_key(r))):
        await records.cash_movement(run, record, exports.get(text_at(record.content, "reference")))
    by_api = {text_at(r.content, "reference") for r in movements}
    alone = [e for reference, e in exports.items() if reference not in by_api]
    for export in sorted(alone, key=lambda e: (e.cells.get("Time (UTC)", ""), e.reference)):
        await records.export_movement(run, export)
    await records.export_rows(run, sorted(exports.values(), key=lambda e: e.reference))
    await records.openings(run)

    await reports.apply(run, seen)
    await reports.prices(run)

    # Reconciliation (design §5.11), at the time this sync's summary arrived: only a run with
    # one has what Trading 212 reports now; another keeps the warnings the last check left.
    mismatches: list[reconcile.Mismatch] | None = None
    if summary is not None:
        positions = [r.record for r in own if r.endpoint == "positions"]
        account = await get_account(run.session, run.account)
        mismatches = await reconcile.check(
            run.session,
            run.account,
            account.name,
            summary.seen_at,
            summary.day,
            await reports.reported(run, summary.record, positions),
        )
        report.mismatches = [m.warning for m in mismatches]
    # The cursor records what was published; the sync also returns it (its cursor replaces
    # the row's).
    report.cursor = await reconcile.publish(ctx, mismatches)
    kept = connection.cursor if isinstance(connection.cursor, dict) else {}
    if any(kept.get(key) != value for key, value in report.cursor.items()):
        await merge_cursor(ctx.command, connection.id, report.cursor)
    report.instrument_flags = await type_flags(run.session, run.connection_id, run.account)
    await merge.notify(ctx, connection.display_name, run.reviews)

    # Only this normaliser's keys: others (the account kind) stay as they are.
    values: dict[str, JSONValue] = {
        accounts.ACCOUNT_MAP_KEY: run.account_map,
        NORMALIZER_VERSION_KEY: NORMALIZER_VERSION,
    }
    if any(connection.config.get(key) != value for key, value in values.items()):
        await update_config(ctx.command, connection.id, values)
    return report
