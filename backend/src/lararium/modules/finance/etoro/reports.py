"""What eToro reports rather than records (design §5.4, §5.9-5.10): its market data and order
lookups, typed by key; `/me`, balances, PnL, aggregate portfolio, instrument breakdown and
balance history, typed per day with `reported` snapshots; and prices.

A report is dated by the sync that saw it (report_sightings): each sighting of balances is a
snapshot of eToro Money's cash and MoneyFarm's balance (the Trading account's `balance` is its
whole value, not cash, so it has none), each of the aggregate portfolio one of every
instrument's units on the Trading account, and each of PnL one of the Trading account's cash
(`credit` plus the same sync's aggregate `accountFrozenCash`, trading_cash), at the time it
arrived. A balance-history snapshot
is one of each account's cash at the end of its `date` (design §5.8), the latest request's for
a day. A day's typed row is the day's latest sighting. Applied to every sighting in the order
seen (a replay), they come out as the syncs that saw them left them; everything is written
only when it differs.

Prices are in the asset currency (pence as pounds / 100), source `etoro`: each daily candle's
close for its day, and each day's current rate (the breakdown's `currentRate`) where no candle
is stored for it, so the official close wins whichever arrives first. A candle's day is the UTC
date of its `time` plus 12 hours (client.candle_day): eToro stamps it at midnight in Israel,
21:00 or 22:00 UTC the evening before. A replay writes every price the connection's records
give, so it also removes an `etoro` price an earlier version filed on a candle's UTC date
that none gives now, while it still holds that candle's close: prices name no connection,
and a value written there since is another eToro connection's. No FX rate is written
(design §5.10).
"""

from collections.abc import Sequence
from datetime import date
from decimal import Decimal
from uuid import UUID

from sqlalchemy import select, text

from lararium.modules.finance import models
from lararium.modules.finance.connector_items import shown_path
from lararium.modules.finance.connector_run import reported_snapshot
from lararium.modules.finance.etoro.client import CANDLE_DAY_SHIFT, candle_day
from lararium.modules.finance.etoro.fields import ENDPOINTS, OPERATION_DATA
from lararium.modules.finance.etoro.records import end_of, link_open
from lararium.modules.finance.etoro.state import (
    SOURCE,
    Item,
    Record,
    Report,
    Run,
    flat,
    id_at,
    moment_at,
    number_at,
    number_or_none,
    objects_at,
    text_at,
    value_at,
)
from lararium.modules.finance.identifiers import normalize_identifier
from lararium.modules.finance.pricing import delete_price, record_price
from lararium.modules.finance.readonly_http import loads_decimal
from lararium.modules.finance.typed_tables import Value, parse_date

# Balances entries whose `balance` is the account's cash or value, as a reported snapshot.
_SNAPSHOT_TYPES = frozenset({"Cash", "MoneyFarm"})


# Market data and lookups -----------------------------------------------------------------


async def metadata(run: Run, by_endpoint: dict[str, list[Record]]) -> None:
    """Instruments (and their images), instrument types and exchanges, by id."""
    for record in by_endpoint["instruments"]:
        endpoint = "instruments"
        values = await run.typed(endpoint, "etoro_instruments", flat(record.content))
        instrument_id = values.pop("instrument_id")
        await run.upsert(
            "etoro_instruments",
            {"connection_id": run.connection_id, "instrument_id": instrument_id},
            values | {"raw_record_id": record.id},
        )
        parent = {"connection_id": run.connection_id, "parent_instrument_id": instrument_id}
        images = objects_at(record.content, "images")
        for n, image in enumerate(images):
            values = await run.typed(endpoint, "etoro_instrument_images", flat(image), "images[].")
            await run.upsert(
                "etoro_instrument_images",
                parent | {"item_index": n},
                values | {"raw_record_id": record.id},
            )
        await run.prune("etoro_instrument_images", parent, len(images))
    for endpoint, table, key in (
        ("instrument_types", "etoro_instrument_types", "instrument_type_id"),
        ("exchanges", "etoro_exchanges", "exchange_id"),
    ):
        for record in by_endpoint[endpoint]:
            values = await run.typed(endpoint, table, flat(record.content))
            value = values.pop(key)
            await run.upsert(
                table,
                {"connection_id": run.connection_id, key: value},
                values | {"raw_record_id": record.id},
            )


def operation_data(run: Run, lookup: Item) -> dict[str, Value]:
    """An order lookup's `externalOperation.data` (fields.OPERATION_DATA) and its members, by
    path: all that is typed at or below it. The field is text holding a JSON object's, its
    members numbers. Anything else is flagged by path, as an unknown field is, and left out:
    a value that is not text, text that is not a JSON object's (kept as sent), a member the
    fields do not know and a known one that is not a number (the known numbers still typed)."""
    data = value_at(lookup, OPERATION_DATA)
    if data is None:
        return {}
    flagged = f"order_lookup.{OPERATION_DATA}"
    if not isinstance(data, str):
        run.flag(flagged)
        return {}
    found: dict[str, Value] = {OPERATION_DATA: data}
    if not data.strip():
        return found
    try:
        members = loads_decimal(data)
    except (ValueError, RecursionError):
        members = None
    if not isinstance(members, dict):
        run.flag(flagged)
        return found
    for name, value in members.items():
        path = f"{OPERATION_DATA}.{name}"
        if value is None:
            continue
        if path in ENDPOINTS["order_lookup"].known and isinstance(value, Decimal):
            found[path] = value
        else:
            run.flag(f"order_lookup.{shown_path(path)}")
    return found


async def lookups(run: Run, records: Sequence[Record]) -> None:
    """Each order looked up, its executions (linked to their positions' opens) and the
    positions it closes."""
    endpoint = "order_lookup"
    for record in records:
        content = record.content
        # `externalOperation.data` and what is below it come from operation_data alone.
        fields = {
            path: value
            for path, value in flat(content).items()
            if path != OPERATION_DATA and not path.startswith(f"{OPERATION_DATA}.")
        }
        values = await run.typed(endpoint, "etoro_orders", fields | operation_data(run, content))
        order_id = values.pop("order_id")
        await run.upsert(
            "etoro_orders",
            {"connection_id": run.connection_id, "order_id": order_id},
            values | {"raw_record_id": record.id},
        )
        parent = {"connection_id": run.connection_id, "parent_order_id": order_id}
        executions = objects_at(content, "positionExecutions")
        for n, execution in enumerate(executions):
            values = await run.typed(
                endpoint, "etoro_position_executions", flat(execution), "positionExecutions[]."
            )
            await run.upsert(
                "etoro_position_executions",
                parent | {"item_index": n},
                values | {"raw_record_id": record.id},
            )
        await run.prune("etoro_position_executions", parent, len(executions))
        for execution in executions:
            position_id = id_at(record, execution, "positionId")
            await link_open(run, position_id, await run.live(f"open:{position_id}"))
        to_close = content.get("positionsToClose")
        closing = to_close if isinstance(to_close, list) else []
        for n, position_id in enumerate(closing):
            values = await run.typed(
                endpoint,
                "etoro_order_positions_to_close",
                {"positionsToClose[]": position_id if isinstance(position_id, str) else None},
            )
            await run.upsert(
                "etoro_order_positions_to_close",
                parent | {"item_index": n},
                values | {"raw_record_id": record.id},
            )
        await run.prune("etoro_order_positions_to_close", parent, len(closing))


async def keyed(run: Run, by_endpoint: dict[str, list[Record]]) -> None:
    """Closed-event counts, stockback payments and daily candles (each candle's close is the
    instrument's price for its day)."""
    for record in by_endpoint["closed_events"]:
        values = await run.typed("closed_events", "etoro_closed_events", flat(record.content))
        key = {
            "connection_id": run.connection_id,
            "close_year": values.pop("close_year"),
            "asset_type": values.pop("asset_type"),
        }
        await run.upsert("etoro_closed_events", key, values | {"raw_record_id": record.id})
    for record in by_endpoint["stockback"]:
        values = await run.typed("stockback", "etoro_stockback_payments", flat(record.content))
        await run.upsert(
            "etoro_stockback_payments",
            {"connection_id": run.connection_id, "raw_record_id": record.id},
            values,
        )
    for record in by_endpoint["candles"]:
        values = await run.typed("candles", "etoro_candles", flat(record.content))
        key = {
            "connection_id": run.connection_id,
            "instrument_id": values.pop("instrument_id"),
            "time": values.pop("time"),
        }
        await run.upsert("etoro_candles", key, values | {"raw_record_id": record.id})
        await _candle_price(run, record)


async def _candle_price(run: Run, record: Record) -> None:
    etoro_id = id_at(record, record.content, "instrumentId")
    close = number_or_none(record, record.content, "close")
    code = run.asset_currencies.get(etoro_id)
    if close is None or close <= 0 or not code:
        return
    instrument_id = await run.instrument(etoro_id)
    if instrument_id is None:
        return
    day = candle_day(moment_at(record, record.content, "time"))
    quote_id, price = await run.price(code, close)
    run.candle_prices[(instrument_id, quote_id, day)] = price


# What a sync reported --------------------------------------------------------------------


def _account(run: Run, entry: Item) -> tuple[str, UUID | None]:
    etoro_id = normalize_identifier("etoro_account", text_at(entry, "accountId"))
    return etoro_id, run.accounts.get(etoro_id)


async def _balances_snapshots(run: Run, report: Report) -> dict[int, UUID]:
    found: dict[int, UUID] = {}
    for n, entry in enumerate(objects_at(report.content, "balances")):
        etoro_id, account_id = _account(run, entry)
        if account_id is None or run.account_types.get(etoro_id) not in _SNAPSHOT_TYPES:
            continue
        currency = await run.currency(text_at(entry, "currency").upper())
        found[n] = await reported_snapshot(
            run.command,
            account_id,
            currency.id,
            number_at(report.record, entry, "balance"),
            report.seen_at,
            report.day,
        )
    return found


async def _aggregate_snapshots(run: Run, report: Report) -> dict[int, UUID]:
    found: dict[int, UUID] = {}
    if run.trading_id is None:
        return found
    for n, item in enumerate(objects_at(report.content, "instrumentAggregates")):
        instrument_id = await run.instrument(id_at(report.record, item, "instrumentId"))
        if instrument_id is None:
            continue
        found[n] = await reported_snapshot(
            run.command,
            run.trading,
            instrument_id,
            number_at(report.record, item, "netUnits"),
            report.seen_at,
            report.day,
        )
    return found


def trading_cash(pnl: Report, aggregate: Report | None) -> Decimal:
    """The Trading account's cash, as eToro reports it now: PnL's `credit` plus the frozen
    cash of the same sync's aggregate portfolio (design §5.11)."""
    credit = number_at(pnl.record, pnl.content, "clientPortfolio.credit")
    if aggregate is None:
        return credit
    return credit + number_at(
        aggregate.record, aggregate.content, "accountTotals.accountFrozenCash"
    )


async def _cash_snapshot(run: Run, pnl: Report, aggregate: Report | None) -> None:
    if run.trading_id is None or not run.account_currency:
        return
    currency = await run.currency(run.account_currency)
    await reported_snapshot(
        run.command,
        run.trading,
        currency.id,
        trading_cash(pnl, aggregate),
        pnl.seen_at,
        pnl.day,
    )


async def _rows(
    run: Run,
    report: Report,
    table: str,
    child: str,
    under: str,
    parent: dict[str, object],
    links: dict[int, dict[str, object]],
) -> None:
    """A report's row for its day, and its array's items as child rows."""
    endpoint = report.endpoint
    values = await run.typed(endpoint, table, flat(report.content))
    await run.upsert(table, parent, values | {"raw_record_id": report.record.id})
    items = objects_at(report.content, under.removesuffix("[]."))
    for n, item in enumerate(items):
        values = await run.typed(endpoint, child, flat(item), under)
        await run.upsert(
            child,
            parent | {"item_index": n},
            values | links.get(n, {}) | {"raw_record_id": report.record.id},
        )
    await run.prune(child, parent, len(items))


async def _instrument_link(run: Run, record: Record, item: Item, path: str) -> dict[str, object]:
    return {"finance_instrument_id": await run.instrument(id_at(record, item, path))}


async def _me(run: Run, report: Report) -> None:
    values = await run.typed("me", "etoro_users", flat(report.content))
    parent = {"connection_id": run.connection_id}
    await run.upsert("etoro_users", parent, values | {"raw_record_id": report.record.id})
    scopes = report.content.get("scopes")
    listed = [s for s in scopes if isinstance(s, str)] if isinstance(scopes, list) else []
    for n, scope in enumerate(listed):
        values = await run.typed("me", "etoro_user_scopes", {"scopes[]": scope})
        await run.upsert(
            "etoro_user_scopes",
            parent | {"item_index": n},
            values | {"raw_record_id": report.record.id},
        )
    await run.prune("etoro_user_scopes", parent, len(listed))


async def _breakdown(run: Run, report: Report) -> None:
    record = report.record
    parent: dict[str, object] = {"connection_id": run.connection_id, "as_of_date": report.day}
    instruments = objects_at(report.content, "instruments")
    links = {
        n: await _instrument_link(run, record, item, "instrumentId")
        for n, item in enumerate(instruments)
    }
    await _rows(
        run,
        report,
        "etoro_breakdowns",
        "etoro_breakdown_instruments",
        "instruments[].",
        parent,
        links,
    )
    for n, instrument in enumerate(instruments):
        below = parent | {"instrument_index": n}
        items = objects_at(instrument, "positions")
        for m, item in enumerate(items):
            values = await run.typed(
                "breakdown", "etoro_breakdown_positions", flat(item), "instruments[].positions[]."
            )
            # A position without its own instrumentId is its parent's instrument's.
            source = item if text_at(item, "instrumentId") else instrument
            link = await _instrument_link(run, record, source, "instrumentId")
            await run.upsert(
                "etoro_breakdown_positions",
                below | {"item_index": m},
                values | link | {"raw_record_id": record.id},
            )
            await _current_price(run, report, item, link["finance_instrument_id"])
        await run.prune("etoro_breakdown_positions", below, len(items))


async def _current_price(run: Run, report: Report, item: Item, instrument_id: object) -> None:
    rate = number_or_none(report.record, item, "currentRate")
    code = text_at(item, "assetCurrency").upper()
    if not isinstance(instrument_id, UUID) or rate is None or rate <= 0 or not code:
        return
    quote_id, price = await run.price(code, rate)
    run.current_prices[(instrument_id, quote_id, report.day)] = price


async def _balance_history(run: Run, reports: Sequence[Report]) -> None:
    """Each request typed; each day's snapshot (the latest request's for the day), each
    account's line of it a reported snapshot of its cash at the end of the day."""
    days: dict[date, tuple[Report, Item]] = {}
    for report in reports:
        record = report.record
        values = await run.typed("balance_history", "etoro_balance_history", flat(record.content))
        await run.upsert(
            "etoro_balance_history",
            {"connection_id": run.connection_id, "raw_record_id": record.id},
            values,
        )
        for snapshot in objects_at(record.content, "snapshots"):
            days[
                parse_date(text_at(snapshot, "date"), f"{record.record_type} snapshots[].date")
            ] = (
                report,
                snapshot,
            )
    for day, (report, snapshot) in sorted(days.items()):
        record = report.record
        values = await run.typed(
            "balance_history", "etoro_balance_snapshots", flat(snapshot), "snapshots[]."
        )
        values.pop("date", None)
        await run.upsert(
            "etoro_balance_snapshots",
            {"connection_id": run.connection_id, "date": day},
            values | {"raw_record_id": record.id},
        )
        parent = {"connection_id": run.connection_id, "snapshot_date": day}
        lines = objects_at(snapshot, "accountSnapshots")
        for n, line in enumerate(lines):
            values = await run.typed(
                "balance_history",
                "etoro_balance_account_snapshots",
                flat(line),
                "snapshots[].accountSnapshots[].",
            )
            _, account_id = _account(run, line)
            cash = number_or_none(record, line, "cash")
            snapshot_id = None
            if account_id is not None and cash is not None:
                currency = await run.currency(text_at(line, "currency").upper())
                snapshot_id = await reported_snapshot(
                    run.command, account_id, currency.id, cash, end_of(day), day
                )
            await run.upsert(
                "etoro_balance_account_snapshots",
                parent | {"item_index": n},
                values
                | {
                    "finance_account_id": account_id,
                    "snapshot_id": snapshot_id,
                    "raw_record_id": record.id,
                },
            )
        await run.prune("etoro_balance_account_snapshots", parent, len(lines))


async def apply(run: Run, reports: Sequence[Report]) -> None:
    """Every sighting, in the order seen: each balances, aggregate and PnL report its
    snapshots; then each day's latest sighting of a report its typed rows (and current
    prices), the latest `/me` the user's row, and the balance history's days."""
    ordered = sorted(reports, key=lambda r: r.order)
    aggregates = {r.sync_id: r for r in ordered if r.endpoint == "aggregate"}
    snapshots: dict[tuple[UUID, UUID], dict[int, UUID]] = {}
    latest: dict[tuple[str, date], Report] = {}
    me: Report | None = None
    for report in ordered:
        match report.endpoint:
            case "balances":
                snapshots[(report.sync_id, report.record.id)] = await _balances_snapshots(
                    run, report
                )
            case "aggregate":
                snapshots[(report.sync_id, report.record.id)] = await _aggregate_snapshots(
                    run, report
                )
            case "pnl":
                await _cash_snapshot(run, report, aggregates.get(report.sync_id))
            case "me":
                me = report
            case _:
                pass
        latest[(report.endpoint, report.day)] = report
    if me is not None:
        await _me(run, me)
    for (endpoint, day), report in latest.items():
        record = report.record
        parent: dict[str, object] = {"connection_id": run.connection_id, "as_of_date": day}
        made = snapshots.get((report.sync_id, record.id), {})
        match endpoint:
            case "balances":
                links: dict[int, dict[str, object]] = {}
                for n, entry in enumerate(objects_at(report.content, "balances")):
                    links[n] = {
                        "finance_account_id": _account(run, entry)[1],
                        "snapshot_id": made.get(n),
                    }
                await _rows(
                    run,
                    report,
                    "etoro_balances",
                    "etoro_balance_accounts",
                    "balances[].",
                    parent,
                    links,
                )
            case "pnl":
                links = {
                    n: await _instrument_link(run, record, item, "instrumentID")
                    for n, item in enumerate(
                        objects_at(report.content, "clientPortfolio.positions")
                    )
                }
                await _rows(
                    run,
                    report,
                    "etoro_pnl",
                    "etoro_pnl_positions",
                    "clientPortfolio.positions[].",
                    parent,
                    links,
                )
            case "aggregate":
                links = {
                    n: await _instrument_link(run, record, item, "instrumentId")
                    | {"snapshot_id": made.get(n)}
                    for n, item in enumerate(objects_at(report.content, "instrumentAggregates"))
                }
                await _rows(
                    run,
                    report,
                    "etoro_aggregates",
                    "etoro_aggregate_instruments",
                    "instrumentAggregates[].",
                    parent,
                    links,
                )
            case "breakdown":
                await _breakdown(run, report)
            case _:
                pass
    await _balance_history(run, [r for r in ordered if r.endpoint == "balance_history"])


async def prices(run: Run) -> None:
    """Each candle's close for its day; a current rate for its day where no candle of the
    connection's is stored for it; on a replay, the `etoro` prices of their instruments that
    none gives removed (module doc)."""
    p = models.Price
    wanted = list(run.candle_prices.items())
    shift = f"{int(CANDLE_DAY_SHIFT.total_seconds())} seconds"
    for key, price in run.current_prices.items():
        if key in run.candle_prices:
            continue
        instrument_id, _, day = key
        has_candle = await run.session.scalar(
            text(
                "select exists (select 1 from finance.etoro_candles c"
                " join finance.instrument_identifiers ii on ii.scheme = 'etoro_id'"
                " and ii.value = c.instrument_id::text"
                " where c.connection_id = :c and ii.instrument_id = :i"
                # A candle without a close (eToro's are nullable) prices nothing.
                " and c.close > 0"
                " and ((c.time at time zone 'UTC') + cast(:shift as interval))::date = :d)"
            ),
            {"c": run.connection_id, "i": instrument_id, "d": day, "shift": shift},
        )
        if not has_candle:
            wanted.append((key, price))
    if run.report.replayed:
        await _remove_unwritten(run, {key for key, _ in wanted})
    for (instrument_id, quote_id, day), price in wanted:
        stored = await run.session.scalar(
            select(p.price).where(
                p.instrument_id == instrument_id,
                p.quote_instrument_id == quote_id,
                p.as_of_date == day,
                p.source == SOURCE,
            )
        )
        if stored == price:
            continue
        await record_price(
            run.command,
            instrument_id=instrument_id,
            quote_instrument_id=quote_id,
            price=price,
            as_of_date=day,
            source=SOURCE,
        )


async def _remove_unwritten(run: Run, wanted: set[tuple[UUID, UUID, date]]) -> None:
    """Delete the `etoro` prices an earlier version filed on one of the connection's candles'
    UTC dates that no record of the replay gives, in an instrument and currency it prices,
    while each still holds the candle's close it filed. Prices name no connection: a value
    written there since (another eToro connection's) is not this one's to remove."""
    pairs = {(instrument_id, quote_id) for instrument_id, quote_id, _ in wanted}
    if not pairs:
        return
    rows = await run.session.execute(
        text(
            "select p.id, p.instrument_id, p.quote_instrument_id, p.as_of_date, p.price,"
            " ii.value as etoro_id, c.close"
            " from finance.prices p"
            " join finance.instrument_identifiers ii on ii.scheme = 'etoro_id'"
            " and ii.instrument_id = p.instrument_id"
            " join finance.etoro_candles c on c.connection_id = :c"
            " and c.instrument_id::text = ii.value"
            " and (c.time at time zone 'UTC')::date = p.as_of_date"
            " where p.source = :s and p.instrument_id = any(:i) and c.close > 0"
        ),
        {"c": run.connection_id, "s": SOURCE, "i": sorted({i for i, _ in pairs})},
    )
    for row in rows:
        key = (row.instrument_id, row.quote_instrument_id, row.as_of_date)
        code = run.asset_currencies.get(row.etoro_id)
        if key[:2] not in pairs or key in wanted or not code:
            continue
        if await run.price(code, row.close) == (row.quote_instrument_id, row.price):
            await delete_price(run.command, row.id, row.price)
