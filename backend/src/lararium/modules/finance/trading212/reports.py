"""What Trading 212 reports rather than records (design §5.9-5.10): instrument and exchange
metadata; each summary, position, pending order and pie, typed per day, the summary's cash and
each position a `reported` snapshot; and prices.

A report is dated by the fetch that first stored its raw record (Record.day, its fetched_at):
a raw record is stored once per content, so that is the only time it carries, and a run that
replays every record (a new normalizer version) rebuilds each day's rows and snapshots as the
runs of that day left them. A day's row is the latest report of the day. Everything here is
written only when it differs from what is stored.
"""

from collections import defaultdict
from collections.abc import Sequence
from datetime import date
from decimal import Decimal
from uuid import UUID

from sqlalchemy import select, text

from lararium.modules.finance import models
from lararium.modules.finance.pricing import record_price
from lararium.modules.finance.trading212 import reconcile
from lararium.modules.finance.trading212.instruments import reclassify
from lararium.modules.finance.trading212.state import (
    SOURCE,
    Record,
    Run,
    flat,
    number_at,
    object_at,
    objects_at,
    stored,
    text_at,
)

_CASH = ("availableToTrade", "reservedForOrders", "inPies")


# Metadata --------------------------------------------------------------------------------


async def instruments(run: Run, records: Sequence[Record], replay: bool) -> None:
    """Every tradable instrument, typed; an instrument made without a type takes the one its
    ticker now has. One unchanged since its row was written from it is skipped (the list
    holds thousands), except when a new version replays."""
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
    if records:
        await reclassify(run.command, run.connection_id)


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


async def _summary(run: Run, record: Record) -> None:
    """The cash as a reported snapshot, and the summary typed for its day."""
    content = record.content
    currency = await run.currency(text_at(content, "currency"))
    snapshot_id = await reconcile.reported_snapshot(
        run.command, run.account, currency.id, cash(record), record.fetched_at, record.day
    )
    values = await run.typed("summary", "trading212_accounts", flat(content))
    await run.upsert(
        "trading212_accounts",
        {"connection_id": run.connection_id, "as_of_date": record.day},
        values
        | {
            "account_id": run.account,
            "account_kind": run.config.get("account_kind"),
            "snapshot_id": snapshot_id,
            "raw_record_id": record.id,
        },
    )


async def _position(run: Run, record: Record, current: bool) -> None:
    """The position as a reported snapshot and a price for its day, typed. `current`: it is
    one of this sync's positions, whose ticker is the one Trading 212 uses now."""
    content = record.content
    snapshot_id = None
    instrument_id = await run.instrument(record, object_at(content, "instrument"), current=current)
    if instrument_id is not None:
        snapshot_id = await reconcile.reported_snapshot(
            run.command,
            run.account,
            instrument_id,
            number_at(record, content, "quantity"),
            record.fetched_at,
            record.day,
        )
        price = number_at(record, content, "currentPrice")
        if price > 0:
            quote = await run.currency(text_at(content, "instrument.currency"))
            run.position_prices[(instrument_id, quote.id, record.day)] = price
    values = await run.typed("positions", "trading212_positions", flat(content))
    ticker = values.pop("instrument_ticker")
    await run.upsert(
        "trading212_positions",
        {"connection_id": run.connection_id, "as_of_date": record.day, "instrument_ticker": ticker},
        values | {"snapshot_id": snapshot_id, "raw_record_id": record.id},
    )


async def _pending_order(run: Run, record: Record) -> None:
    values = await run.typed("pending_orders", "trading212_pending_orders", flat(record.content))
    order_id = values.pop("id")
    await run.upsert(
        "trading212_pending_orders",
        {"connection_id": run.connection_id, "as_of_date": record.day, "id": order_id},
        values | {"raw_record_id": record.id},
    )


async def _pie_records(run: Run) -> tuple[dict[str, list[Record]], dict[str, list[Record]]]:
    """Every stored pie list item and pie detail of the connection, by pie id, oldest first."""
    lists: defaultdict[str, list[Record]] = defaultdict(list)
    details: defaultdict[str, list[Record]] = defaultdict(list)
    for record in await stored(run.session, run.connection_id, ["pies", "pie"]):
        if record.endpoint == "pies":
            lists[text_at(record.content, "id")].append(record)
        else:
            details[text_at(record.content, "settings.id")].append(record)
    return lists, details


def _latest(records: Sequence[Record], day: date) -> Record | None:
    """The latest of the records (oldest first) stored by the end of the day."""
    found = [record for record in records if record.day <= day]
    return found[-1] if found else None


async def _pies(run: Run, records: Sequence[Record]) -> None:
    """A pie's row for each day a list item or detail of it was stored: the latest list item
    and detail by the end of that day (a pie the list did not hold yet has none). Typed only;
    no postings."""
    days = sorted(
        {
            (text_at(r.content, "id" if r.endpoint == "pies" else "settings.id"), r.day)
            for r in records
        }
    )
    if not days:
        return
    lists, details = await _pie_records(run)
    for pie, day in days:
        listed = _latest(lists[pie], day)
        if listed is None:
            continue
        detail = _latest(details[pie], day)
        values = await run.typed("pies", "trading212_pies", flat(listed.content))
        pie_id = values.pop("id")
        if detail is not None:
            values |= await run.typed("pie", "trading212_pies", flat(detail.content))
        await run.upsert(
            "trading212_pies",
            {"connection_id": run.connection_id, "id": pie_id, "as_of_date": day},
            values
            | {
                "raw_record_id": listed.id,
                "detail_raw_record_id": detail.id if detail is not None else None,
            },
        )
        if detail is not None:
            await _pie_detail(run, detail, pie_id, day)


async def _pie_detail(run: Run, record: Record, pie_id: object, day: date) -> None:
    endpoint = "pie"
    content = record.content
    parent = {"connection_id": run.connection_id, "pie_id": pie_id, "as_of_date": day}
    entries = objects_at(content, "instruments")
    for n, entry in enumerate(entries):
        values = await run.typed(
            endpoint, "trading212_pie_instruments", flat(entry), "instruments[]."
        )
        await run.upsert(
            "trading212_pie_instruments",
            parent | {"item_index": n},
            values | {"raw_record_id": record.id},
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
                values | {"raw_record_id": record.id},
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
            values | {"raw_record_id": record.id},
        )
    await run.session.execute(
        text(
            "delete from finance.trading212_pie_instrument_shares where connection_id = :c"
            " and pie_id = :p and as_of_date = :d and ticker <> all(cast(:t as text[]))"
        ),
        {"c": run.connection_id, "p": pie_id, "d": day, "t": sorted(shares)},
    )


async def apply(run: Run, records: Sequence[Record], own: set[UUID]) -> None:
    """Every report given, oldest first, so a day's row is its latest. `own`: the ids of this
    sync's records."""
    ordered = sorted(records, key=lambda r: (r.fetched_at, r.id))
    for record in ordered:
        match record.endpoint:
            case "summary":
                await _summary(run, record)
            case "positions":
                await _position(run, record, record.id in own)
            case "pending_orders":
                await _pending_order(run, record)
            case _:
                pass
    await _pies(run, [r for r in ordered if r.endpoint in ("pies", "pie")])


async def reported(run: Run, summary: Record, positions: Sequence[Record]) -> dict[UUID, Decimal]:
    """What this sync's summary and positions report: each instrument's quantity, and the
    cash in the summary's currency."""
    found: defaultdict[UUID, Decimal] = defaultdict(Decimal)
    for record in positions:
        instrument_id = await run.instrument(
            record, object_at(record.content, "instrument"), current=True
        )
        if instrument_id is not None:
            found[instrument_id] += number_at(record, record.content, "quantity")
    currency = await run.currency(text_at(summary.content, "currency"))
    found[currency.id] += cash(summary)
    return dict(found)


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
