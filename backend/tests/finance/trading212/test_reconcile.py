"""Reconciling the ledger against Trading 212's reported positions and cash at the sync's time
(design §5.11), and the warnings and notifications that follow (Decision 12)."""

from datetime import timedelta
from decimal import Decimal

import pytest
from sqlalchemy import text

from lararium.core.connections import get_connection, set_warnings
from lararium.modules.finance import connector_run

from .fixtures import (
    TICKER,
    dividend_item,
    instrument_json,
    order_item,
    position_json,
    summary_json,
    transaction_item,
)
from .test_normalize import META, NOW, Ledger, _connection, _delete_derived, _reports

DEPOSIT = ("transactions", transaction_item(amount="491.95"))
BUY = ("orders", order_item())
# 491.95 in, 241.20 out: 250.75, as summary_json reports it (200.50 + 0 + 50.25).
SUMMARY = ("summary", summary_json())
# Two days on, a sync reports a second buy the history does not show yet.
LATER = NOW + timedelta(days=2)
WARNING = "Trading 212 Invest ···0001 ZZQA differs from Trading 212's report on 2026-03-05 by -3"
CASH_WARNING = (
    "Trading 212 Invest ···0001 GBP differs from Trading 212's report on 2026-03-05 by 241.20"
)
# 491.95 - 2 x 241.20: the cash after both buys.
AFTER_TWO = (
    "summary",
    summary_json(
        cash={
            "availableToTrade": Decimal("9.55"),
            "reservedForOrders": Decimal("0"),
            "inPies": Decimal("0"),
        }
    ),
)
SIX = ("positions", position_json(quantity="6"))


def _second_buy() -> tuple[str, dict]:
    """A buy filled after the first sync: it is not in the opening."""
    item = order_item(order_id=500002, fill_id=700002)
    item["fill"]["filledAt"] = "2026-03-04T14:30:00Z"
    return ("orders", item)


async def _consistent(ledger: Ledger) -> None:
    """A first sync whose history explains its report: no opening, no warning."""
    await ledger.normalize(SUMMARY, META, DEPOSIT, BUY, ("positions", position_json()))


async def _snapshots(session) -> list[tuple]:
    rows = await session.execute(
        text(
            "select i.symbol, s.quantity, s.as_of, s.as_of_date from finance.balance_snapshots s"
            " join finance.instruments i on i.id = s.instrument_id"
            " where s.kind = 'reported' order by i.symbol, s.as_of"
        )
    )
    return [tuple(r) for r in rows]


async def test_reconcile_consistent_no_warning(session, run) -> None:
    connection = await _connection(run)
    ledger = Ledger(session, connection)
    report = await ledger.normalize(SUMMARY, META, DEPOSIT, BUY, ("positions", position_json()))
    assert report.mismatches == []
    assert (await get_connection(session, connection.id)).warnings == []
    assert ledger.notifier.messages == []

    # Each reported figure is a snapshot at the sync's time, linked from its typed row.
    assert await _snapshots(session) == [
        ("GBP", Decimal("250.75"), NOW, NOW.date()),
        ("ZZQA", Decimal(3), NOW, NOW.date()),
    ]
    linked = await session.scalar(
        text(
            "select count(*) from finance.trading212_accounts a"
            " join finance.balance_snapshots s on s.id = a.snapshot_id"
            " where a.connection_id = :c"
        ),
        {"c": connection.id},
    )
    assert linked == 1
    position = (
        await session.execute(
            text(
                "select p.quantity, s.quantity as reported from finance.trading212_positions p"
                " join finance.balance_snapshots s on s.id = p.snapshot_id"
            )
        )
    ).one()
    assert (position.quantity, position.reported) == (Decimal(3), Decimal(3))

    # A later sync the same day reporting the same figures records no new snapshot.
    await ledger.normalize(SUMMARY, ("positions", position_json()), now=NOW.replace(hour=14))
    assert len(await _snapshots(session)) == 2


async def test_missing_fill_warns_once(session, run) -> None:
    connection = await _connection(run)
    await run(lambda ctx: set_warnings(ctx, connection.id, ["Some other warning"]))
    ledger = Ledger(session, connection)
    await _consistent(ledger)
    report = await ledger.normalize(AFTER_TWO, SIX, now=LATER)
    assert report.mismatches == [CASH_WARNING, WARNING]
    warnings = (await get_connection(session, connection.id)).warnings
    assert warnings == ["Some other warning", CASH_WARNING, WARNING]
    assert ledger.notifier.messages == [f"Trading 212 Invest: {CASH_WARNING}\n{WARNING}"]

    # Still missing: the warnings stand, and nothing more is said.
    await ledger.normalize(AFTER_TWO, SIX, now=LATER.replace(hour=13))
    assert (await get_connection(session, connection.id)).warnings == warnings
    assert len(ledger.notifier.messages) == 1

    # The fill arrives: the warnings clear, and nothing more is said.
    report = await ledger.normalize(AFTER_TWO, SIX, _second_buy(), now=LATER.replace(hour=14))
    assert report.mismatches == []
    assert (await get_connection(session, connection.id)).warnings == ["Some other warning"]
    assert len(ledger.notifier.messages) == 1
    count = await session.scalar(
        text("select count(*) from finance.transactions where external_id like 'opening:%'")
    )
    assert count == 0


async def test_a_renamed_account_replaces_its_warnings(session, run) -> None:
    connection = await _connection(run)
    ledger = Ledger(session, connection)
    await _consistent(ledger)
    await ledger.normalize(AFTER_TWO, SIX, now=LATER)
    await session.execute(
        text("update finance.accounts set name = 'T212' where source_connection_id = :c"),
        {"c": connection.id},
    )
    await ledger.normalize(AFTER_TWO, SIX, now=LATER.replace(hour=13))
    # The new name's warnings replace the old name's.
    renamed = [w.replace("Trading 212 Invest ···0001", "T212") for w in (CASH_WARNING, WARNING)]
    assert (await get_connection(session, connection.id)).warnings == renamed


async def test_reconciliation_waits_for_a_summary(session, run) -> None:
    connection = await _connection(run)
    ledger = Ledger(session, connection)
    await _consistent(ledger)
    await ledger.normalize(AFTER_TWO, SIX, now=LATER)
    before = (await get_connection(session, connection.id)).warnings
    assert before == [CASH_WARNING, WARNING]
    # A run without the account's summary checks nothing, and keeps what the last one found.
    await ledger.normalize(("dividends", dividend_item()), now=LATER.replace(hour=13))
    assert (await get_connection(session, connection.id)).warnings == before


async def test_a_fill_after_the_summary_is_no_difference(session, run) -> None:
    connection = await _connection(run)
    ledger = Ledger(session, connection)
    await _consistent(ledger)
    # The summary and positions arrive at noon; a buy fills a minute later, before the
    # history is fetched: the report does not hold it yet.
    after = order_item(order_id=500002, fill_id=700002)
    after["fill"]["filledAt"] = (LATER + timedelta(minutes=1)).isoformat()
    report = await ledger.normalize(
        SUMMARY, ("positions", position_json()), ("orders", after), now=LATER
    )
    assert report.mismatches == []


# A second ticker of the fixtures' listing (the same ISIN and currency): one instrument.
OTHER = "ZZQB_US_EQ"
OTHER_META = ("instruments", instrument_json(OTHER))


def _held(**quantities: str) -> list[tuple[str, dict]]:
    tickers = {"first": TICKER, "other": OTHER}
    return [("positions", position_json(tickers[k], quantity=q)) for k, q in quantities.items()]


async def _links(session) -> list[tuple]:
    """(ticker, the quantity of the snapshot its row links) of each position row."""
    rows = await session.execute(
        text(
            "select p.instrument_ticker, s.quantity from finance.trading212_positions p"
            " left join finance.balance_snapshots s on s.id = p.snapshot_id order by 1"
        )
    )
    return [tuple(r) for r in rows]


def _written(monkeypatch) -> list[str]:
    """The typed tables written from here on, one entry per row."""
    written: list[str] = []
    upsert = connector_run.upsert_typed

    async def spy(ctx, table, key, values) -> bool:
        wrote = await upsert(ctx, table, key, values)
        if wrote:
            written.append(table)
        return wrote

    monkeypatch.setattr(connector_run, "upsert_typed", spy)
    return written


async def _writes_nothing(session, ledger: Ledger, written: list[str]) -> None:
    """Normalizing the last sync again, and replaying every record, write nothing."""
    state = await _reports(session, ledger.connection)
    for again in (ledger.again, ledger.replay):
        await again()
        assert written == []
        assert (ledger.events(), ledger.changes()) == (0, 1 if again == ledger.replay else 0)
        assert await _reports(session, ledger.connection) == state


@pytest.mark.parametrize("quantities", [("1.5", "1.5"), ("1", "2")])
async def test_two_tickers_of_one_instrument_are_one_snapshot(
    session, run, monkeypatch, quantities
) -> None:
    connection = await _connection(run)
    ledger = Ledger(session, connection)
    first, other = quantities
    report = await ledger.normalize(
        SUMMARY, META, OTHER_META, DEPOSIT, BUY, *_held(first=first, other=other)
    )
    # The instrument's quantity is the two tickers' together, as the reconciliation reads it.
    assert report.mismatches == []
    assert (await get_connection(session, connection.id)).warnings == []
    assert await _snapshots(session) == [
        ("GBP", Decimal("250.75"), NOW, NOW.date()),
        ("ZZQA", Decimal(3), NOW, NOW.date()),
    ]
    links = [(TICKER, Decimal(3)), (OTHER, None)]
    assert await _links(session) == links
    state = await _reports(session, connection)

    await _writes_nothing(session, ledger, _written(monkeypatch))
    await _delete_derived(session, connection)
    await ledger.replay()
    assert await _reports(session, connection) == state
    assert await _links(session) == links


@pytest.mark.parametrize("before", [{"first": "3"}, {"first": "1.5", "other": "1.5"}])
async def test_a_snapshot_is_linked_from_the_latest_row_to_name_it(
    session, run, monkeypatch, before
) -> None:
    """The instrument's quantity seen again the same day under its other ticker alone (one
    renamed, or one of two sold as the other bought): the same snapshot, linked once."""
    connection = await _connection(run)
    ledger = Ledger(session, connection)
    await ledger.normalize(SUMMARY, META, OTHER_META, DEPOSIT, BUY, *_held(**before))
    report = await ledger.normalize(SUMMARY, *_held(other="3"), now=NOW.replace(hour=14))
    assert report.mismatches == []
    assert await _snapshots(session) == [
        ("GBP", Decimal("250.75"), NOW, NOW.date()),
        ("ZZQA", Decimal(3), NOW, NOW.date()),
    ]
    links = [(TICKER, None), (OTHER, Decimal(3))]
    assert await _links(session) == links
    state = await _reports(session, connection)

    await _writes_nothing(session, ledger, _written(monkeypatch))
    assert await _links(session) == links
    await _delete_derived(session, connection)
    await ledger.replay()
    assert await _reports(session, connection) == state
    assert await _links(session) == links
