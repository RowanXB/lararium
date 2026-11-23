"""Reconciling eToro's accounts against what eToro reports (design §5.11), and the prompt to
upload a statement where one would explain Trading's cash (Decision 12)."""

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import text

from lararium.core.connections import get_connection, set_warnings
from lararium.modules.finance.etoro import sync as etoro_sync

from .fixtures import (
    INSTRUMENT_ID,
    TRADING_ACCOUNT,
    aggregate_body,
    balances_body,
    bank_item,
    breakdown_body,
    breakdown_position,
    cash_item,
    history_body,
    instrument_meta,
    me_json,
    pnl_body,
    pnl_position,
)
from .statement_fixtures import activity_row, build_statement, summary
from .test_normalize_api import Ledger, _connection
from .test_normalize_statement import StatementLedger, transfer_in

USERNAME = me_json()["username"]
OTHER = "Some other warning"
POSITION = 3000000301
# The October statement: 1260 USD in from eToro Money, so Trading holds 1260 at its end.
OCTOBER = build_statement(summary(username=USERNAME), [transfer_in()])
FIRST = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)
LATER = datetime(2026, 11, 6, 12, 0, tzinfo=UTC)


def _trading(day: date, cash: str) -> tuple[date, list[tuple[str, str, str, str]]]:
    return (day, [(TRADING_ACCOUNT, "Trading", "USD", cash)])


def _sync(
    credit: str,
    history: list[tuple[date, list[tuple[str, str, str, str]]]],
    *,
    positions: tuple[dict[str, Any], ...] = (),
    breakdown: tuple[dict[str, Any], ...] = (),
    units: str | None = None,
    money: str = "0",
) -> list[tuple[str, Any]]:
    """What a sync reports of Trading: its PnL credit, its balance history, its positions."""
    entries: list[tuple[str, Any]] = [
        ("me", me_json()),
        ("balances", balances_body(money=money)),
        ("instruments", instrument_meta()),
        (
            "aggregate",
            aggregate_body([(INSTRUMENT_ID, "USD", units)] if units is not None else []),
        ),
        ("pnl", pnl_body(list(positions), credit=credit)),
        ("balance_history", history_body(history)),
    ]
    if breakdown:
        entries.append(("breakdown", breakdown_body(list(breakdown))))
    return entries


async def _warnings(session, ledger: Ledger) -> list[str]:
    return (await get_connection(session, ledger.connection.id)).warnings


async def _after_october(run, ledger: StatementLedger) -> None:
    """A first sync with no figures of Trading's, then the October statement."""
    await run(lambda ctx: set_warnings(ctx, ledger.connection.id, [OTHER]))
    await ledger.normalize(("me", me_json()), ("balances", balances_body()), now=FIRST)
    await ledger.upload(OCTOBER)


@pytest.fixture
async def ledger(session, run, tmp_path, monkeypatch) -> StatementLedger:
    """A ledger that syncs and uploads statements, as test_normalize_statement's does."""
    found = StatementLedger(session, await _connection(run), tmp_path)
    normalize = etoro_sync.normalize

    async def spy(ctx, raw_record_ids):
        report = await normalize(ctx, raw_record_ids)
        found.reports.append(report)
        return report

    monkeypatch.setattr(etoro_sync, "normalize", spy)
    return found


async def test_consistent_no_warning(session, run, ledger) -> None:
    await _after_october(run, ledger)
    # A buy of 4 for 500 after the statement's end: 760 left, as eToro reports it.
    opened = "2026-11-01T09:00:00.000Z"
    now = datetime(2026, 11, 2, 12, 0, tzinfo=UTC)
    await ledger.normalize(
        *_sync(
            "760.00",
            [_trading(date(2026, 10, 31), "1260.00"), _trading(date(2026, 11, 1), "760.00")],
            positions=(pnl_position(POSITION, opened=opened),),
            breakdown=(breakdown_position(POSITION),),
            units="4",
        ),
        now=now,
    )
    assert await _warnings(session, ledger) == [OTHER]
    assert ledger.notifier.messages == []
    # Trading's cash now is a reported snapshot: PnL credit plus frozen cash, as seen.
    [snapshot] = (
        await session.execute(
            text(
                "select s.quantity, s.as_of from finance.balance_snapshots s"
                " join finance.accounts a on a.id = s.account_id"
                " join finance.instruments i on i.id = s.instrument_id"
                " where a.name = 'eToro Trading' and i.symbol = 'USD' and s.kind = 'reported'"
                " and s.as_of = :at"
            ),
            {"at": now},
        )
    ).all()
    assert tuple(snapshot) == (Decimal("760.00"), now)


async def test_unmatched_internal_transfer_prompts_statement_once(session, run, ledger) -> None:
    # No statement yet: Trading's cash differs, so a statement is asked for, from the
    # account's earliest record to today.
    await run(lambda ctx: set_warnings(ctx, ledger.connection.id, [OTHER]))
    history = [_trading(date(2026, 10, 30), "1260.00"), _trading(date(2026, 10, 31), "1260.00")]
    await ledger.normalize(*_sync("1260.00", history), now=FIRST)
    first = "eToro Trading: upload an eToro statement covering 2026-10-30 to 2026-11-01"
    assert await _warnings(session, ledger) == [OTHER, first]
    assert ledger.notifier.messages == [f"eToro: {first}"]
    # The October statement explains it: the prompt clears, with nothing said.
    await ledger.upload(OCTOBER)
    assert await _warnings(session, ledger) == [OTHER]
    assert len(ledger.notifier.messages) == 1

    # eToro Money takes 1000 GBP in and sends it on to Trading after the statement's end:
    # Trading's cash differs until a statement brings its leg.
    deposit = bank_item("80000101", "1000.00") | {"postedAt": "2026-11-05T09:00:00Z"}
    out = cash_item("80000102", "1000.00") | {
        "direction": "debit",
        "transactionSubtype": "transferSent",
        "postedAt": "2026-11-05T10:00:00Z",
    }
    entries = [
        *_sync(
            "2510.00",
            [_trading(date(2026, 11, 4), "1260.00"), _trading(date(2026, 11, 5), "2510.00")],
        ),
        ("cash_transactions", deposit),
        ("cash_transactions", out),
    ]
    await ledger.normalize(*entries, now=LATER)
    prompt = "eToro Trading: upload an eToro statement covering 2026-11-01 to 2026-11-06"
    assert await _warnings(session, ledger) == [OTHER, prompt]
    assert ledger.notifier.messages[1:] == [f"eToro: {prompt}"]

    # Two more syncs the same day: the prompt stands, and nothing more is said.
    for hours in (1, 2):
        await ledger.normalize(*entries, now=LATER + timedelta(hours=hours))
    assert await _warnings(session, ledger) == [OTHER, prompt]
    assert len(ledger.notifier.messages) == 2

    # A statement covering those dates brings Trading's leg: the books agree, the prompt clears.
    november = build_statement(
        summary(
            username=USERNAME,
            start="01/11/2026 00:00:00",
            end="06/11/2026 23:59:59",
        ),
        [
            activity_row(
                "05/11/2026 10:00:00",
                "Transfer: GBP > USD",
                "1250.00",
                details="GBP",
                equity="2510.00",
                balance="2510.00",
            )
        ],
    )
    await ledger.upload(november)
    assert await _warnings(session, ledger) == [OTHER]
    assert len(ledger.notifier.messages) == 2


async def test_difference_without_cause_is_a_difference(session, run, ledger) -> None:
    await _after_october(run, ledger)
    now = datetime(2026, 11, 2, 12, 0, tzinfo=UTC)
    # eToro reports 40 more than the books hold, and 2 units the books do not: nothing after
    # the statement's end would explain it.
    await ledger.normalize(
        *_sync("1300.00", [_trading(date(2026, 11, 1), "1300.00")], units="2"), now=now
    )
    expected = [
        "eToro Trading USD differs from eToro's report on 2026-11-01 by -40.00",
        "eToro Trading USD differs from eToro's report on 2026-11-02 by -40.00",
        "eToro Trading ZZQA differs from eToro's report on 2026-11-02 by -2",
    ]
    assert await _warnings(session, ledger) == [OTHER, *expected]
    assert ledger.notifier.messages == ["eToro: " + "\n".join(expected)]


async def test_api_fees_after_the_statement_prompt_statement(session, run, ledger) -> None:
    await _after_october(run, ledger)
    now = datetime(2026, 11, 2, 12, 0, tzinfo=UTC)
    # A position the books bought for 500 after the statement's end; eToro has since taken
    # 0.40 of overnight fees from it, which only a statement posts (R30).
    opened = "2026-11-01T09:00:00.000Z"
    await ledger.normalize(
        *_sync(
            "759.60",
            [_trading(date(2026, 11, 1), "759.60")],
            positions=(pnl_position(POSITION, opened=opened),),
            breakdown=(breakdown_position(POSITION, overnightFeesAndDividends=Decimal("-0.40")),),
            units="4",
        ),
        now=now,
    )
    prompt = "eToro Trading: upload an eToro statement covering 2026-11-01 to 2026-11-02"
    assert await _warnings(session, ledger) == [OTHER, prompt]


async def test_money_balance_checked(session, run) -> None:
    ledger = Ledger(session, await _connection(run))
    await run(lambda ctx: set_warnings(ctx, ledger.connection.id, [OTHER]))
    credit = cash_item("80000201", "10.00") | {"postedAt": "2026-06-01T10:00:00Z"}
    await ledger.normalize(
        ("balances", balances_body(money="25.00")), ("cash_transactions", credit)
    )
    warning = "eToro Money GBP GBP differs from eToro's report on 2026-06-02 by -15.00"
    assert await _warnings(session, ledger) == [OTHER, warning]
    assert ledger.notifier.messages == [f"eToro: {warning}"]
    # The balance agrees once eToro Money's other record arrives.
    more = cash_item("80000202", "15.00") | {"postedAt": "2026-06-02T10:00:00Z"}
    await ledger.normalize(
        ("balances", balances_body(money="25.00")),
        ("cash_transactions", more),
        now=datetime(2026, 6, 2, 13, 0, tzinfo=UTC),
    )
    assert await _warnings(session, ledger) == [OTHER]
    assert len(ledger.notifier.messages) == 1


async def test_moneyfarm_not_reconciled(session, run) -> None:
    ledger = Ledger(session, await _connection(run))
    await ledger.normalize(("balances", balances_body(moneyfarm="900.50")))
    assert await _warnings(session, ledger) == []
    assert ledger.notifier.messages == []
