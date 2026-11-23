"""eToro raw records into the ledger (design §5.5-5.10): the API's side. The statement's rows
(Task 11) are passed by here, as is a refused key's record (sync.REFUSED_KEY).

Deterministic from its inputs and versioned (NORMALIZER_VERSION, stored in the connection's
config, Decision 8): when the stored version differs, the run replays every raw record the
connection holds (the latest copy of each keyed item, and every sighting of a report,
report_sightings), so what a new version stores is filled in for what an older one saw, dated
as it was then. A run otherwise applies the records it was given and its own sync's
sightings.

In order: the accounts (accounts.py, from the balances); the market data, which types the
instruments (reports.py, instruments.py); opens and closes of the positions the run's records
name, and eToro Money's transactions, as connector transactions (records.py); the order
lookups, closed events, stockback and candles; the reports, typed per day with reported
snapshots (reports.py); eToro Money's opening adjustment (records.py); and prices.

Every record's fields are checked first: a required one missing fails the run; an unknown one
with a value is flagged, as are values and types the mapping does not know. Some flags are
found in full every run, so they clear once their cause is gone: instruments still without a
type, of one the ledger does not hold, or of no known currency, and positions no source
states the settlement of.
"""

from collections import defaultdict
from collections.abc import Sequence
from decimal import MAX_PREC, localcontext
from uuid import UUID

from sqlalchemy import text

from lararium.core.connections import get_connection, update_config
from lararium.core.connectors import SyncContext
from lararium.core.json_types import JSONValue
from lararium.modules.finance.connector_items import lacks, leaves
from lararium.modules.finance.etoro import accounts, records, reports
from lararium.modules.finance.etoro.fields import ENDPOINTS
from lararium.modules.finance.etoro.instruments import currency_flags, reclassify, type_flags
from lararium.modules.finance.etoro.state import (
    EtoroDataError,
    NormalizeReport,
    Record,
    Report,
    Run,
    id_at,
    latest_sighted,
    objects_at,
    record_key,
    records_of,
    reports_of,
    stored,
    text_at,
)
from lararium.modules.finance.report_sightings import list_sightings

__all__ = ["NORMALIZER_VERSION", "EtoroDataError", "NormalizeReport", "normalize"]

NORMALIZER_VERSION = "etoro-norm-1"
# The version that last normalized the connection, in its config (Decision 8).
NORMALIZER_VERSION_KEY = "normalizer_version"
# Items a key names once per connection: the ledger's (Decision 9) and the others'. The latest
# copy of each is what counts. The rest are reports, each dated by its sighting.
_LEDGER = ("trade_history", "cash_transactions")
_KEYED = (
    *_LEDGER,
    "order_lookup",
    "instruments",
    "instrument_types",
    "exchanges",
    "closed_events",
    "stockback",
    "candles",
)
_REPORTS = ("me", "balances", "pnl", "aggregate", "breakdown", "balance_history")
_KEY_KIND = {"trade_history": "position id", "cash_transactions": "transaction id"}


def _check(run: Run, record: Record) -> None:
    """Required fields fail the run when missing; unknown ones with a value are flagged."""
    spec = ENDPOINTS[record.endpoint]
    for path in sorted(spec.required):
        if lacks(record.content, path.split(".")):
            raise EtoroDataError(f"{spec.record_type} lacks {path}")
    for path in sorted(set(leaves(record.content)) - spec.known):
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
            raise EtoroDataError(
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


async def _latest_stored(run: Run, endpoint: str) -> dict[str, Record]:
    """The connection's latest copy of each item of a keyed endpoint, by key."""
    return {record_key(r): r for r in await stored(run.session, run.connection_id, [endpoint])}


async def _context(run: Run, seen: Sequence[Report], lookups: dict[str, Record]) -> None:
    """The Trading account's currency, and each instrument's asset currency and a symbol:
    from the run's reports, else the latest stored (and a symbol from the order lookups)."""
    for endpoint in ("aggregate", "breakdown"):
        found = [r for r in seen if r.endpoint == endpoint]
        latest = (
            found[-1] if found else await latest_sighted(run.session, run.connection_id, endpoint)
        )
        if latest is not None and not run.account_currency:
            run.account_currency = text_at(latest.content, "accountCurrency").upper()
    if not run.account_currency and run.trading_id is not None:
        balances = [r for r in seen if r.endpoint == "balances"]
        latest = (
            balances[-1]
            if balances
            else await latest_sighted(run.session, run.connection_id, "balances")
        )
        for entry in objects_at(latest.content if latest else None, "balances"):
            if text_at(entry, "accountId").lower() == run.trading_id:
                run.account_currency = text_at(entry, "currency").upper()
    for lookup in lookups.values():
        # Its symbol only: its `asset.currency` is not the asset's (the probe found USD there
        # for a position the aggregate gave in GBP).
        etoro_id = text_at(lookup.content, "asset.instrumentId")
        if etoro_id and (symbol := text_at(lookup.content, "asset.symbol")):
            run.symbols[etoro_id] = symbol
    rows = await run.session.execute(
        text(
            "select id, currency from ("
            " select instrument_id::text as id, asset_currency as currency, as_of_date"
            " from finance.etoro_aggregate_instruments"
            " where connection_id = :c and asset_currency is not null"
            " union all select coalesce(p.instrument_id, i.instrument_id)::text,"
            " p.asset_currency, p.as_of_date from finance.etoro_breakdown_positions p"
            " join finance.etoro_breakdown_instruments i on i.connection_id = p.connection_id"
            " and i.as_of_date = p.as_of_date and i.item_index = p.instrument_index"
            " where p.connection_id = :c and p.asset_currency is not null) found"
            " order by as_of_date"
        ),
        {"c": run.connection_id},
    )
    for row in rows:
        run.asset_currencies[row.id] = row.currency.upper()
    for report in sorted(seen, key=lambda r: r.order):
        if report.endpoint == "aggregate":
            for item in objects_at(report.content, "instrumentAggregates"):
                if code := text_at(item, "assetCurrency").upper():
                    run.asset_currencies[id_at(report.record, item, "instrumentId")] = code
        elif report.endpoint == "breakdown":
            for instrument in objects_at(report.content, "instruments"):
                etoro_id = id_at(report.record, instrument, "instrumentId")
                if symbol := text_at(instrument, "symbol"):
                    run.symbols[etoro_id] = symbol
                for item in objects_at(instrument, "positions"):
                    if code := text_at(item, "assetCurrency").upper():
                        run.asset_currencies[etoro_id] = code


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

    balances = [r for r in seen if r.endpoint == "balances"]
    if not balances and (
        latest := await latest_sighted(run.session, run.connection_id, "balances")
    ):
        balances = [latest]
    await accounts.accounts(run, balances)

    await reports.metadata(run, by_endpoint)
    await records.void_unsupported(run, await reclassify(run.command, run.connection_id))
    trades = {
        id_at(r, r.content, "positionId"): r
        for r in [
            *(await _latest_stored(run, "trade_history")).values(),
            *by_endpoint["trade_history"],
        ]
    }
    lookups = await _latest_stored(run, "order_lookup")
    lookups |= {record_key(r): r for r in by_endpoint["order_lookup"]}
    await _context(run, seen, lookups)
    given = {id_at(r, r.content, "positionId") for r in by_endpoint["trade_history"]}
    # Closes not posted yet (their instrument's currency unknown, say): tried again.
    given |= set(
        await run.session.scalars(
            text(
                "select position_id::text from finance.etoro_trades where connection_id = :c"
                " and transaction_id is null and raw_record_id is not null"
            ),
            {"c": run.connection_id},
        )
    )
    for lookup in by_endpoint["order_lookup"]:
        given |= {
            id_at(lookup, item, "positionId")
            for item in objects_at(lookup.content, "positionExecutions")
        }
    for position in await records.positions(run, seen, trades, lookups, given, replay):
        await records.position(run, position)
    for record in sorted(
        by_endpoint["cash_transactions"],
        key=lambda r: (text_at(r.content, "postedAt"), record_key(r)),
    ):
        await records.cash(run, record)

    await reports.lookups(run, by_endpoint["order_lookup"])
    await reports.keyed(run, by_endpoint)
    await reports.apply(run, seen)
    await records.openings(run)
    await reports.prices(run)
    await _links(run)
    report.flags_in_full = await type_flags(
        run.session, run.connection_id, sorted(run.accounts.values())
    )
    report.flags_in_full |= await currency_flags(run.session, run.connection_id)
    if unknown := await records.settlement_unknown(run, trades, lookups):
        report.flags_in_full.add(f"{records.SETTLEMENT_UNKNOWN} ({unknown})")

    # Only this normaliser's keys: others stay as they are.
    values: dict[str, JSONValue] = {
        accounts.ACCOUNT_MAP_KEY: run.account_map,
        NORMALIZER_VERSION_KEY: NORMALIZER_VERSION,
    }
    if any(connection.config.get(key) != value for key, value in values.items()):
        await update_config(ctx.command, connection.id, values)
    return report


async def _links(run: Run) -> None:
    """Link the market data and order rows to the instruments their eToro ids now name."""
    for table, column in (
        ("etoro_instruments", "instrument_id"),
        ("etoro_orders", "asset_instrument_id"),
    ):
        await run.session.execute(
            text(
                f"update finance.{table} m set finance_instrument_id = ii.instrument_id"
                " from finance.instrument_identifiers ii"
                f" where ii.scheme = 'etoro_id' and ii.value = m.{column}::text"
                " and m.connection_id = :c"
                " and m.finance_instrument_id is distinct from ii.instrument_id"
            ),
            {"c": run.connection_id},
        )
