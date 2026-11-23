"""Reconciling the ledger against Trading 212's reported positions and cash at the sync's time
(design §5.11), and the warnings and notifications that follow (Decision 12)."""

from decimal import Decimal

from sqlalchemy import text

from lararium.core.connections import get_connection, set_warnings

from .fixtures import dividend_item, order_item, position_json, summary_json, transaction_item
from .test_normalize import META, NOW, Ledger, _connection

DEPOSIT = ("transactions", transaction_item(amount="491.95"))
BUY = ("orders", order_item())
# 491.95 in, 241.20 out: 250.75, as summary_json reports it (200.50 + 0 + 50.25).
SUMMARY = ("summary", summary_json())
WARNING = "Trading 212 Invest ···0001 ZZQA differs from Trading 212's report on 2026-03-03 by -3"
CASH_WARNING = (
    "Trading 212 Invest ···0001 GBP differs from Trading 212's report on 2026-03-03 by 241.20"
)


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
    second = ("orders", order_item(order_id=500002, fill_id=700002))
    # Two buys reported (6 held, 491.95 - 2 x 241.20 = 9.55 cash); one fill in the history.
    summary = summary_json(
        cash={
            "availableToTrade": Decimal("9.55"),
            "reservedForOrders": Decimal("0"),
            "inPies": Decimal("0"),
        }
    )
    six = ("positions", position_json(quantity="6"))
    report = await ledger.normalize(("summary", summary), META, DEPOSIT, BUY, six)
    assert report.mismatches == [CASH_WARNING, WARNING]
    warnings = (await get_connection(session, connection.id)).warnings
    assert warnings == ["Some other warning", CASH_WARNING, WARNING]
    assert ledger.notifier.messages == [f"Trading 212 Invest: {CASH_WARNING}\n{WARNING}"]

    # Still missing: the warnings stand, and nothing more is said.
    await ledger.normalize(("summary", summary), six, now=NOW.replace(hour=13))
    assert (await get_connection(session, connection.id)).warnings == warnings
    assert len(ledger.notifier.messages) == 1

    # The fill arrives: the warnings clear, and nothing more is said.
    report = await ledger.normalize(("summary", summary), six, second, now=NOW.replace(hour=14))
    assert report.mismatches == []
    assert (await get_connection(session, connection.id)).warnings == ["Some other warning"]
    assert len(ledger.notifier.messages) == 1


async def test_a_renamed_account_replaces_its_warnings(session, run) -> None:
    connection = await _connection(run)
    ledger = Ledger(session, connection)
    six = ("positions", position_json(quantity="6"))
    await ledger.normalize(SUMMARY, META, DEPOSIT, BUY, six)
    await session.execute(
        text("update finance.accounts set name = 'T212' where source_connection_id = :c"),
        {"c": connection.id},
    )
    await ledger.normalize(SUMMARY, six, now=NOW.replace(hour=13))
    assert (await get_connection(session, connection.id)).warnings == [
        "T212 ZZQA differs from Trading 212's report on 2026-03-03 by -3"
    ]


async def test_reconciliation_waits_for_a_summary(session, run) -> None:
    connection = await _connection(run)
    ledger = Ledger(session, connection)
    await ledger.normalize(SUMMARY, META, DEPOSIT, BUY, ("positions", position_json(quantity="6")))
    assert (await get_connection(session, connection.id)).warnings == [WARNING]
    # A run without the account's summary checks nothing, and keeps what the last one found.
    await ledger.normalize(("dividends", dividend_item()), now=NOW.replace(hour=13))
    assert (await get_connection(session, connection.id)).warnings == [WARNING]
