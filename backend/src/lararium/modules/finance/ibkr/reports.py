"""What IBKR reports rather than records: prices, FX rates, reported positions and cash, and
the typed report tables (design 5.9, 5.10).

Everything here is written only when it differs from what is stored, so a row repeated by
overlapping windows writes nothing and emits no event.
"""

from collections.abc import Iterable
from datetime import date, datetime, time
from decimal import Decimal
from uuid import UUID

from sqlalchemy import select

from lararium.modules.finance import models
from lararium.modules.finance.ibkr import tables
from lararium.modules.finance.ibkr.state import Item, Run
from lararium.modules.finance.pricing import record_fx_rate, record_price
from lararium.modules.finance.snapshots import record_snapshot

SOURCE = "ibkr"
BASE_SUMMARY = "BASE_SUMMARY"
LOT_LEVEL = "LOT"


def as_of(day: date) -> datetime:
    """A reported figure holds at the end of its report day in New York (design 5.8)."""
    return datetime.combine(day, time(23, 59, 59, 999999), tables.NEW_YORK)


def is_lot(item: Item) -> bool:
    """A lot of an open position: a Lot element, or an OpenPosition at the lot level."""
    return item.record_type == "ibkr.position_lot" or item.text("levelOfDetail") == LOT_LEVEL


async def fx_rates(run: Run, items: Iterable[Item]) -> None:
    """Each conversion rate, to the account's base currency. IBKR's -1 (no rate), and a
    currency the ledger does not have (IBKR lists about 40), are left out."""
    r = models.FxRate
    for item in items:
        rate = item.number("rate")
        base = await run.known_currency(item.text("fromCurrency"))
        quote = await run.known_currency(item.text("toCurrency"))
        if rate <= 0 or base is None or quote is None or base.id == quote.id:
            continue
        day = item.day("reportDate")
        stored = await run.session.scalar(
            select(r.rate).where(
                r.base_instrument_id == base.id,
                r.quote_instrument_id == quote.id,
                r.as_of_date == day,
                r.source == SOURCE,
            )
        )
        if stored == rate:
            continue
        await record_fx_rate(
            run.command,
            base_instrument_id=base.id,
            quote_instrument_id=quote.id,
            rate=rate,
            as_of_date=day,
            source=SOURCE,
        )


async def _price(run: Run, item: Item, price: Decimal, day: date) -> None:
    instrument_id = await run.instrument(item)
    if instrument_id is None or price == 0:
        return
    quote = await run.currency(item.text("currency"))
    p = models.Price
    stored = await run.session.scalar(
        select(p.price).where(
            p.instrument_id == instrument_id,
            p.quote_instrument_id == quote.id,
            p.as_of_date == day,
            p.source == SOURCE,
        )
    )
    if stored == price:
        return
    await record_price(
        run.command,
        instrument_id=instrument_id,
        quote_instrument_id=quote.id,
        price=price,
        as_of_date=day,
        source=SOURCE,
    )


async def prices(run: Run, prior: Iterable[Item], positions: Iterable[Item]) -> None:
    """Prior Period Positions' price on its date, then Open Positions' mark price on the
    report date; a later write for the same date replaces an earlier one."""
    for item in prior:
        await _price(run, item, item.number("price"), item.day("date"))
    for item in positions:
        if not is_lot(item):
            await _price(run, item, item.number("markPrice"), item.day("reportDate"))


async def cash_reports(run: Run, items: Iterable[Item]) -> None:
    for item in items:
        if item.text("currency") == BASE_SUMMARY:
            continue
        currency = await run.currency(item.text("currency"))
        values = await tables.typed_values(
            run.session, "ibkr_cash_reports", item.section, item.attributes
        )
        key = {
            "account_id": run.account_id(item),
            "currency_instrument_id": currency.id,
            "from_date": values.pop("from_date"),
            "to_date": values.pop("to_date"),
        }
        await tables.upsert_cash_reports(run.command, key, values)


async def nav(run: Run, items: Iterable[Item]) -> None:
    for item in items:
        values = await tables.typed_values(run.session, "ibkr_nav", item.section, item.attributes)
        key = {"account_id": run.account_id(item), "report_date": values.pop("report_date")}
        await tables.upsert_nav(run.command, key, values)


async def trade_details(run: Run, items: Iterable[Item]) -> None:
    """Every level of Trades but executions: orders, closed lots, wash sales, summaries."""
    for item in items:
        values = await tables.typed_values(
            run.session, "ibkr_trade_details", item.section, item.attributes
        )
        values.pop("level_of_detail", None)
        related = (
            await run.instrument(item, flag=False)
            if item.text("conid") and item.text("assetCategory") not in ("", "CASH")
            else None
        )
        key = {
            "account_id": run.account_id(item),
            "level_of_detail": item.text("levelOfDetail"),
            "row_key": tables.trade_detail_key(item.attributes),
        }
        await tables.upsert_trade_details(run.command, key, values | {"instrument_id": related})


async def _snapshot(
    run: Run, account_id: UUID, instrument_id: UUID, quantity: Decimal, day: date
) -> UUID:
    """The reported snapshot at the end of the day, recorded unless one with the same
    quantity is there already."""
    s = models.BalanceSnapshot
    moment = as_of(day)
    stored = (
        await run.session.execute(
            select(s.id, s.quantity)
            .where(
                s.account_id == account_id,
                s.instrument_id == instrument_id,
                s.as_of == moment,
                s.kind == "reported",
            )
            .order_by(s.recorded_at.desc(), s.id.desc())
            .limit(1)
        )
    ).one_or_none()
    if stored is not None and stored.quantity == quantity:
        return stored.id
    record = await record_snapshot(
        run.command,
        account_id=account_id,
        instrument_id=instrument_id,
        quantity=quantity,
        as_of=moment,
        as_of_date=day,
        kind="reported",
    )
    return record.id


async def snapshots(run: Run, positions: Iterable[Item], cash: Iterable[Item]) -> None:
    """Reported positions (with ibkr_positions and their lots) and each currency's ending
    cash (not the BASE_SUMMARY row) as reported snapshots."""
    positions = list(positions)
    held: dict[tuple[str, str, date], UUID] = {}
    for item in positions:
        if is_lot(item):
            continue
        instrument_id = await run.instrument(item)
        if instrument_id is None:
            continue
        account_id = run.account_id(item)
        day = item.day("reportDate")
        snapshot_id = await _snapshot(run, account_id, instrument_id, item.number("position"), day)
        values = await tables.typed_values(
            run.session, "ibkr_positions", item.section, item.attributes
        )
        await tables.upsert_positions(
            run.command, {"snapshot_id": snapshot_id}, values | {"account_id": account_id}
        )
        held[(item.account, item.text("conid"), day)] = snapshot_id
    for item in positions:
        if not is_lot(item):
            continue
        reported = item.optional_day("reportDate")
        snapshot_id = held.get((item.account, item.text("conid"), reported)) if reported else None
        if snapshot_id is None:
            # A lot of a position this run reports no summary of.
            run.flag(f"{item.section}.unposted")
            continue
        values = await tables.typed_values(
            run.session, "ibkr_position_lots", item.section, item.attributes
        )
        values.pop("originating_transaction_id", None)
        key = {
            "snapshot_id": snapshot_id,
            "originating_transaction_id": tables.lot_key(item.attributes),
        }
        await tables.upsert_position_lots(
            run.command, key, values | {"account_id": run.account_id(item)}
        )
    for item in cash:
        if item.text("currency") == BASE_SUMMARY:
            continue
        currency = await run.currency(item.text("currency"))
        await _snapshot(
            run, run.account_id(item), currency.id, item.number("endingCash"), item.day("toDate")
        )
