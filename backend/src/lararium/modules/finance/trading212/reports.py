"""What Trading 212 reports rather than records (design §5.9-5.10): instrument and exchange
metadata; each summary, position, pending order and pie, typed per day, the summary's cash and
each instrument's positions a `reported` snapshot; and prices.

A report is dated by the sync that saw it (report_sightings): a raw record is stored once per
content, so content that comes back (A, B, A) is one record, seen each time. Each sighting of
a summary, or of an instrument's positions, is a reported snapshot at its time; a day's typed
row and price are the day's latest sighting. Applied to every sighting in the order seen (a
new normalizer version's replay), they come out as the syncs that saw them left them.
Everything here is written only when it differs from what is stored.
"""

from collections.abc import Sequence
from datetime import date
from decimal import Decimal
from itertools import groupby
from uuid import UUID

from sqlalchemy import select, text

from lararium.modules.finance import models
from lararium.modules.finance.connector_run import reported_snapshot
from lararium.modules.finance.pricing import record_price
from lararium.modules.finance.trading212.instruments import reclassify
from lararium.modules.finance.trading212.state import (
    SOURCE,
    Record,
    Report,
    Run,
    flat,
    number_at,
    object_at,
    objects_at,
    record_key,
    text_at,
)

_CASH = ("availableToTrade", "reservedForOrders", "inPies")


# Metadata --------------------------------------------------------------------------------


async def instruments(run: Run, records: Sequence[Record], replay: bool) -> list[UUID]:
    """Every tradable instrument, typed; an instrument made without a type takes the one its
    ticker now has. Returns those made without a type that the ledger does not hold. One
    unchanged since its row was written from it is skipped (the list holds thousands), except
    when a new version replays."""
    stored = {} if replay else await run.stored_records("trading212_instruments", "ticker")
    for record in records:
        ticker = text_at(record.content, "ticker")
        if stored.get(ticker) == record.id:
            continue
        values = await run.typed("instruments", "trading212_instruments", flat(record.content))
        values.pop("ticker", None)
        await run.upsert(
            "trading212_instruments",
            {"connection_id": run.connection_id, "ticker": ticker},
            values | {"raw_record_id": record.id},
        )
    return await reclassify(run.command, run.connection_id) if records else []


async def exchanges(run: Run, records: Sequence[Record], replay: bool) -> None:
    stored = {} if replay else await run.stored_records("trading212_exchanges", "id")
    endpoint = "exchanges"
    for record in records:
        content = record.content
        values = await run.typed(endpoint, "trading212_exchanges", flat(content))
        exchange_id = values.pop("id")
        if stored.get(str(exchange_id)) == record.id:
            continue
        parent = {"connection_id": run.connection_id, "exchange_id": exchange_id}
        await run.upsert(
            "trading212_exchanges",
            {"connection_id": run.connection_id, "id": exchange_id},
            values | {"raw_record_id": record.id},
        )
        schedules = objects_at(content, "workingSchedules")
        for n, schedule in enumerate(schedules):
            values = await run.typed(
                endpoint, "trading212_exchange_schedules", flat(schedule), "workingSchedules[]."
            )
            await run.upsert(
                "trading212_exchange_schedules",
                parent | {"item_index": n},
                values | {"raw_record_id": record.id},
            )
            events = objects_at(schedule, "timeEvents")
            for m, event in enumerate(events):
                values = await run.typed(
                    endpoint,
                    "trading212_exchange_time_events",
                    flat(event),
                    "workingSchedules[].timeEvents[].",
                )
                await run.upsert(
                    "trading212_exchange_time_events",
                    parent | {"schedule_index": n, "item_index": m},
                    values | {"raw_record_id": record.id},
                )
            await run.prune(
                "trading212_exchange_time_events", parent | {"schedule_index": n}, len(events)
            )
        await run.prune("trading212_exchange_schedules", parent, len(schedules))
    if records:
        # Each instrument's exchange: the one whose schedule its workingScheduleId is.
        await run.session.execute(
            text(
                "update finance.trading212_instruments i set exchange_id = s.exchange_id"
                " from finance.trading212_exchange_schedules s"
                " where i.connection_id = :c and s.connection_id = :c"
                " and s.id = i.working_schedule_id"
                " and i.exchange_id is distinct from s.exchange_id"
            ),
            {"c": run.connection_id},
        )


# What a sync reported --------------------------------------------------------------------


def cash(record: Record) -> Decimal:
    """A summary's cash: `availableToTrade + reservedForOrders + inPies` (design §5.11)."""
    content = record.content
    return sum((number_at(record, content, f"cash.{part}") for part in _CASH), Decimal(0))


async def _cash_snapshot(run: Run, report: Report) -> UUID:
    """The reported snapshot of a summary's cash, as seen."""
    currency = await run.currency(text_at(report.content, "currency"))
    return await reported_snapshot(
        run.command, run.account, currency.id, cash(report.record), report.seen_at, report.day
    )


async def _position_snapshots(run: Run, positions: Sequence[Report]) -> dict[str, UUID | None]:
    """Ticker → the reported snapshot its row links, of the positions one sync saw at one
    time. An instrument held under several tickers has one snapshot, of their quantities
    together (as `reported` counts it), linked from the row of the smallest ticker."""
    held: dict[UUID, list[Report]] = {}
    found: dict[str, UUID | None] = {}
    for report in positions:
        record = report.record
        instrument_id = await run.instrument(
            record, object_at(record.content, "instrument"), current=report.sync_id == run.sync_id
        )
        found[record_key(record)] = None
        if instrument_id is not None:
            held.setdefault(instrument_id, []).append(report)
    for instrument_id, group in held.items():
        quantity = sum((number_at(r.record, r.content, "quantity") for r in group), Decimal(0))
        found[min(record_key(r.record) for r in group)] = await reported_snapshot(
            run.command, run.account, instrument_id, quantity, group[0].seen_at, group[0].day
        )
    return found


async def _summary(run: Run, report: Report, snapshot_id: UUID | None) -> None:
    values = await run.typed("summary", "trading212_accounts", flat(report.content))
    await run.upsert(
        "trading212_accounts",
        {"connection_id": run.connection_id, "as_of_date": report.day},
        values
        | {
            "account_id": run.account,
            "account_kind": run.config.get("account_kind"),
            "snapshot_id": snapshot_id,
            "raw_record_id": report.record.id,
        },
    )


async def _position(run: Run, report: Report, snapshot_id: UUID | None) -> None:
    """The position typed for its day, and its price for the day."""
    record = report.record
    content = record.content
    instrument_id = await run.instrument(
        record, object_at(content, "instrument"), current=report.sync_id == run.sync_id
    )
    price = number_at(record, content, "currentPrice")
    if instrument_id is not None and price > 0:
        quote_id, in_quote = await run.price(text_at(content, "instrument.currency"), price)
        run.position_prices[(instrument_id, quote_id, report.day)] = in_quote
    values = await run.typed("positions", "trading212_positions", flat(content))
    ticker = values.pop("instrument_ticker")
    key = {
        "connection_id": run.connection_id,
        "as_of_date": report.day,
        "instrument_ticker": ticker,
    }
    if snapshot_id is not None:
        # One row links a snapshot: the latest to name it. An earlier row has the same one
        # when the instrument's quantity came back the same under another ticker.
        await run.session.execute(
            text(
                "update finance.trading212_positions set snapshot_id = null"
                " where snapshot_id = :snapshot_id and not (connection_id = :connection_id"
                " and as_of_date = :as_of_date and instrument_ticker = :instrument_ticker)"
            ),
            key | {"snapshot_id": snapshot_id},
        )
    await run.upsert(
        "trading212_positions",
        key,
        values | {"snapshot_id": snapshot_id, "raw_record_id": record.id},
    )


async def _pending_order(run: Run, report: Report) -> None:
    values = await run.typed("pending_orders", "trading212_pending_orders", flat(report.content))
    order_id = values.pop("id")
    await run.upsert(
        "trading212_pending_orders",
        {"connection_id": run.connection_id, "as_of_date": report.day, "id": order_id},
        values | {"raw_record_id": report.record.id},
    )


async def _pie(run: Run, listed: Report, detail: Report | None) -> None:
    """A pie's row for the day: the list's item, and the detail the same sync fetched (none:
    the detail's columns and rows cleared). Typed only; no postings."""
    values = await run.typed("pies", "trading212_pies", flat(listed.content))
    pie_id = values.pop("id")
    content = flat(detail.content) if detail is not None else {}
    values |= await run.typed("pie", "trading212_pies", content)
    await run.upsert(
        "trading212_pies",
        {"connection_id": run.connection_id, "id": pie_id, "as_of_date": listed.day},
        values
        | {
            "raw_record_id": listed.record.id,
            "detail_raw_record_id": detail.record.id if detail is not None else None,
        },
    )
    await _pie_detail(run, detail, pie_id, listed.day)


async def _pie_detail(run: Run, report: Report | None, pie_id: object, day: date) -> None:
    endpoint = "pie"
    content = report.content if report is not None else {}
    raw_record_id = report.record.id if report is not None else None
    parent = {"connection_id": run.connection_id, "pie_id": pie_id, "as_of_date": day}
    entries = objects_at(content, "instruments")
    for n, entry in enumerate(entries):
        values = await run.typed(
            endpoint, "trading212_pie_instruments", flat(entry), "instruments[]."
        )
        await run.upsert(
            "trading212_pie_instruments",
            parent | {"item_index": n},
            values | {"raw_record_id": raw_record_id},
        )
        issues = objects_at(entry, "issues")
        for m, issue in enumerate(issues):
            values = await run.typed(
                endpoint,
                "trading212_pie_instrument_issues",
                flat(issue),
                "instruments[].issues[].",
            )
            await run.upsert(
                "trading212_pie_instrument_issues",
                parent | {"instrument_index": n, "item_index": m},
                values | {"raw_record_id": raw_record_id},
            )
        await run.prune(
            "trading212_pie_instrument_issues", parent | {"instrument_index": n}, len(issues)
        )
    await run.prune("trading212_pie_instruments", parent, len(entries))
    shares = object_at(content, "settings.instrumentShares") or {}
    for ticker, share in shares.items():
        values = await run.typed(
            endpoint,
            "trading212_pie_instrument_shares",
            {"share": share if isinstance(share, str) else None},
            "settings.instrumentShares{}.",
        )
        await run.upsert(
            "trading212_pie_instrument_shares",
            parent | {"ticker": ticker},
            values | {"raw_record_id": raw_record_id},
        )
    await run.session.execute(
        text(
            "delete from finance.trading212_pie_instrument_shares where connection_id = :c"
            " and pie_id = :p and as_of_date = :d and ticker <> all(cast(:t as text[]))"
        ),
        {"c": run.connection_id, "p": pie_id, "d": day, "t": sorted(shares)},
    )


async def apply(run: Run, reports: Sequence[Report]) -> None:
    """Every sighting, in the order seen: each summary, and each instrument's positions, a
    snapshot; then each day's latest sighting of an item its typed row (and price), also in
    the order seen."""
    ordered = sorted(reports, key=lambda r: r.order)
    # (sync, endpoint, the item's key) → the snapshot the item's row links.
    snapshots: dict[tuple[UUID, str, str], UUID | None] = {}
    latest: dict[tuple[str, date, str], Report] = {}
    for (_, sync_id), group in groupby(ordered, key=lambda r: (r.seen_at, r.sync_id)):
        sighting = list(group)
        for report in sighting:
            key = record_key(report.record)
            if report.endpoint == "summary":
                snapshots[(sync_id, "summary", key)] = await _cash_snapshot(run, report)
            latest[(report.endpoint, report.day, key)] = report
        positions = [r for r in sighting if r.endpoint == "positions"]
        for ticker, snapshot_id in (await _position_snapshots(run, positions)).items():
            snapshots[(sync_id, "positions", ticker)] = snapshot_id
    details = {(r.sync_id, record_key(r.record)): r for r in ordered if r.endpoint == "pie"}
    # In the order seen, a snapshot several rows name linked from the last of them only: as
    # the syncs that wrote them one by one left it (_position), so a replay writes nothing.
    rows = sorted(latest.items(), key=lambda item: item[1].order)
    linked = {snapshots.get((r.sync_id, e, k)): (e, day, k) for (e, day, k), r in rows}
    for row, report in rows:
        endpoint, _, key = row
        snapshot_id = snapshots.get((report.sync_id, endpoint, key))
        if linked[snapshot_id] != row:
            snapshot_id = None
        match endpoint:
            case "summary":
                await _summary(run, report, snapshot_id)
            case "positions":
                await _position(run, report, snapshot_id)
            case "pending_orders":
                await _pending_order(run, report)
            case "pies":
                await _pie(run, report, details.get((report.sync_id, key)))
            case _:
                pass


async def reported(
    run: Run, summary: Record, positions: Sequence[Record], *, current: bool = True
) -> dict[UUID, Decimal]:
    """What a sync's summary and positions report (`current`: this sync's): each
    instrument's quantity, and the cash in the summary's currency."""
    found: dict[UUID, Decimal] = {}
    for record in positions:
        instrument_id = await run.instrument(
            record, object_at(record.content, "instrument"), current=current
        )
        if instrument_id is not None:
            quantity = number_at(record, record.content, "quantity")
            found[instrument_id] = found.get(instrument_id, Decimal(0)) + quantity
    currency = await run.currency(text_at(summary.content, "currency"))
    found[currency.id] = found.get(currency.id, Decimal(0)) + cash(summary)
    return found


async def prices(run: Run) -> None:
    """Each position's price for its day; a fill's price on its day when no price of
    Trading 212's is there for it yet (a later write for a day replaces an earlier one)."""
    p = models.Price
    wanted = [(k, v, True) for k, v in run.position_prices.items()]
    wanted += [(k, v, False) for k, v in run.fill_prices.items() if k not in run.position_prices]
    for (instrument_id, quote_id, day), price, replaces in wanted:
        stored = await run.session.scalar(
            select(p.price).where(
                p.instrument_id == instrument_id,
                p.quote_instrument_id == quote_id,
                p.as_of_date == day,
                p.source == SOURCE,
            )
        )
        if stored == price or (stored is not None and not replaces):
            continue
        await record_price(
            run.command,
            instrument_id=instrument_id,
            quote_instrument_id=quote_id,
            price=price,
            as_of_date=day,
            source=SOURCE,
        )
