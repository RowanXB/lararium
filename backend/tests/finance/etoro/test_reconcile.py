"""Reconciling eToro's accounts against what eToro reports (design §5.11), and the prompt to
upload a statement where one would explain Trading's cash (Decision 12)."""

import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import text

from lararium.core.connections import get_connection, set_warnings
from lararium.modules.finance.etoro import reconcile
from lararium.modules.finance.etoro import sync as etoro_sync
from lararium.modules.finance.etoro.reconcile import UNRECONCILED

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
from .test_normalize_api import Ledger, Notifier, _connection
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
    first = "eToro Trading: upload an eToro statement covering 2026-10-30 onward"
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
    prompt = "eToro Trading: upload an eToro statement covering 2026-11-01 onward"
    assert await _warnings(session, ledger) == [OTHER, prompt]
    assert ledger.notifier.messages[1:] == [f"eToro: {prompt}"]

    # Two more syncs on later days, the cause unchanged: the prompt's text stands (R34), and
    # nothing more is said.
    for days in (1, 2):
        await ledger.normalize(*entries, now=LATER + timedelta(days=days))
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


async def test_one_offset_over_consecutive_days_is_one_warning(session, run, ledger) -> None:
    """A difference that stands, unchanged, on consecutive snapshot days is one warning
    naming its first and last day; a change, or a gap of a day, starts another."""
    await _after_october(run, ledger)
    now = datetime(2026, 11, 7, 12, 0, tzinfo=UTC)
    history = [
        _trading(date(2026, 11, 1), "1300.00"),
        _trading(date(2026, 11, 2), "1300.00"),
        _trading(date(2026, 11, 3), "1300.00"),
        _trading(date(2026, 11, 4), "1310.00"),
        _trading(date(2026, 11, 6), "1310.00"),
    ]
    await ledger.normalize(*_sync("1260.00", history), now=now)
    assert await _warnings(session, ledger) == [
        OTHER,
        "eToro Trading USD differs from eToro's report from 2026-11-01 to 2026-11-03 by -40.00",
        "eToro Trading USD differs from eToro's report on 2026-11-04 by -50.00",
        "eToro Trading USD differs from eToro's report on 2026-11-06 by -50.00",
    ]


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
    prompt = "eToro Trading: upload an eToro statement covering 2026-11-01 onward"
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


async def test_prompt_notified_once_across_days(session, run, ledger) -> None:
    """R34: the prompt names no end, so the same cause on three days is told once."""
    await _after_october(run, ledger)
    out = cash_item("80000301", "1000.00") | {
        "direction": "debit",
        "transactionSubtype": "transferSent",
        "postedAt": "2026-11-03T10:00:00Z",
    }
    deposit = bank_item("80000302", "1000.00") | {"postedAt": "2026-11-03T09:00:00Z"}
    for day in (4, 5, 6):
        entries = _sync("2510.00", [_trading(date(2026, 11, day - 1), "2510.00")])
        await ledger.normalize(
            *entries,
            ("cash_transactions", deposit),
            ("cash_transactions", out),
            now=datetime(2026, 11, day, 12, 0, tzinfo=UTC),
        )
    prompt = "eToro Trading: upload an eToro statement covering 2026-11-01 onward"
    assert await _warnings(session, ledger) == [OTHER, prompt]
    assert ledger.notifier.messages == [f"eToro: {prompt}"]


class FailingOnce(Notifier):
    """A notifier whose first send fails."""

    def __init__(self) -> None:
        super().__init__()
        self.failed = False

    async def send(self, text: str) -> None:
        if not self.failed:
            self.failed = True
            raise RuntimeError("the notifier is down")
        await super().send(text)


async def test_a_failed_notification_is_sent_next_run(session, run) -> None:
    ledger = Ledger(session, await _connection(run))
    ledger.notifier = FailingOnce()
    credit = cash_item("80000401", "10.00") | {"postedAt": "2026-06-01T10:00:00Z"}
    entries = [("balances", balances_body(money="25.00")), ("cash_transactions", credit)]
    await ledger.normalize(*entries)
    warning = "eToro Money GBP GBP differs from eToro's report on 2026-06-02 by -15.00"
    # The warning stands, but the owner was not told: the next run tells, once.
    assert await _warnings(session, ledger) == [warning]
    assert ledger.notifier.messages == []
    await ledger.normalize(*entries, now=datetime(2026, 6, 2, 13, 0, tzinfo=UTC))
    assert ledger.notifier.messages == [f"eToro: {warning}"]
    await ledger.normalize(*entries, now=datetime(2026, 6, 2, 14, 0, tzinfo=UTC))
    assert len(ledger.notifier.messages) == 1


async def test_no_balances_yet_cannot_reconcile(session, run) -> None:
    ledger = Ledger(session, await _connection(run))
    await run(lambda ctx: set_warnings(ctx, ledger.connection.id, [OTHER]))
    for hours in (0, 1):
        await ledger.normalize(("me", me_json()), now=FIRST + timedelta(hours=hours))
    assert await _warnings(session, ledger) == [OTHER, UNRECONCILED]
    assert ledger.notifier.messages == [f"eToro: {UNRECONCILED}"]


async def test_no_trading_account_keeps_the_last_check(session, run) -> None:
    ledger = Ledger(session, await _connection(run))
    credit = cash_item("80000501", "10.00") | {"postedAt": "2026-06-01T10:00:00Z"}
    await ledger.normalize(
        ("balances", balances_body(money="25.00")), ("cash_transactions", credit)
    )
    warning = "eToro Money GBP GBP differs from eToro's report on 2026-06-02 by -15.00"
    assert await _warnings(session, ledger) == [warning]
    # The balances stop listing a Trading account: the last check's warning stands, and the
    # owner is told the check could not run, once.
    without = balances_body(money="25.00")
    without["balances"] = [b for b in without["balances"] if b["accountType"] != "Trading"]
    for hours in (1, 2):
        await ledger.normalize(("balances", without), now=FIRST + timedelta(hours=hours))
    assert await _warnings(session, ledger) == [warning, UNRECONCILED]
    assert ledger.notifier.messages == [f"eToro: {warning}", f"eToro: {UNRECONCILED}"]
    # Once it can check again, the fixed warning clears without a word.
    await ledger.normalize(
        ("balances", balances_body(money="10.00")), now=FIRST + timedelta(hours=3)
    )
    assert await _warnings(session, ledger) == []
    assert len(ledger.notifier.messages) == 2


def _transfer_to_trading(day: int) -> list[tuple[str, Any]]:
    """eToro Money takes 1000 GBP in and sends it on to Trading on 2026-11-<day>: a cause a
    statement would explain."""
    deposit = bank_item("80000601", "1000.00") | {"postedAt": f"2026-11-0{day}T09:00:00Z"}
    out = cash_item("80000602", "1000.00") | {
        "direction": "debit",
        "transactionSubtype": "transferSent",
        "postedAt": f"2026-11-0{day}T10:00:00Z",
    }
    return [("cash_transactions", deposit), ("cash_transactions", out)]


async def test_a_difference_on_the_statements_last_day_is_ordinary(session, run, ledger) -> None:
    await _after_october(run, ledger)
    # October's statement covers 2026-10-31 in full: eToro's 40 more that day is a difference,
    # even while a transfer after it asks for the next statement.
    history = [_trading(date(2026, 10, 31), "1300.00"), _trading(date(2026, 11, 1), "2510.00")]
    await ledger.normalize(
        *_sync("2510.00", history),
        *_transfer_to_trading(1),
        now=datetime(2026, 11, 2, 12, 0, tzinfo=UTC),
    )
    assert await _warnings(session, ledger) == [
        OTHER,
        "eToro Trading USD differs from eToro's report on 2026-10-31 by -40.00",
        "eToro Trading: upload an eToro statement covering 2026-11-01 onward",
    ]


async def test_ordinary_differences_stand_beside_the_prompt(session, run, ledger) -> None:
    await _after_october(run, ledger)
    # The prompt stands (a transfer after the statement's end), and still: a day the statement
    # covers differs, and eToro reports 2 units the books do not hold.
    history = [_trading(date(2026, 10, 30), "1250.00"), _trading(date(2026, 11, 1), "2510.00")]
    await ledger.normalize(
        *_sync("2510.00", history, units="2"),
        *_transfer_to_trading(1),
        now=datetime(2026, 11, 2, 12, 0, tzinfo=UTC),
    )
    assert await _warnings(session, ledger) == [
        OTHER,
        "eToro Trading USD differs from eToro's report on 2026-10-30 by 10.00",
        "eToro Trading ZZQA differs from eToro's report on 2026-11-02 by -2",
        "eToro Trading: upload an eToro statement covering 2026-11-01 onward",
    ]


async def test_data_reconciliation_cannot_read_does_not_fail(session, run, caplog) -> None:
    ledger = Ledger(session, await _connection(run))
    await ledger.normalize(("balances", balances_body()))
    # An account the map names that is gone: reconciliation cannot run, and says so.
    ctx = await ledger.context()
    gone = {TRADING_ACCOUNT: uuid.uuid4()}
    assert await reconcile.check(ctx, gone) is None
    assert [r.getMessage() for r in caplog.records if "reconciliation" in r.getMessage()] == [
        "eToro reconciliation could not run (EtoroDataError)"
    ]
    await reconcile.publish(ctx, None)
    assert await _warnings(session, ledger) == [UNRECONCILED]


async def test_a_fee_change_after_a_summer_statement_counts(session, run, ledger) -> None:
    """In BST a statement ending 23:59:59 UTC ends at 00:59:59 London the next day: a positions
    report sighted after it that London day is after the end, though stamped with the end's
    London date."""
    await run(lambda ctx: set_warnings(ctx, ledger.connection.id, [OTHER]))
    await ledger.normalize(
        ("me", me_json()),
        ("balances", balances_body()),
        now=datetime(2026, 6, 30, 12, 0, tzinfo=UTC),
    )
    june = build_statement(
        summary(
            username=USERNAME,
            start="01/06/2026 00:00:00",
            end="30/06/2026 23:59:59",
            created="01/06/2026 09:15:00",
        ),
        [transfer_in("01/06/2026 10:00:00")],
    )
    await ledger.upload(june)
    # 00:30 UTC is 01:30 London on 1 July: a buy of 4 for 500, and 0.40 of fees since.
    opened = "2026-07-01T00:10:00.000Z"
    await ledger.normalize(
        *_sync(
            "759.60",
            [_trading(date(2026, 6, 30), "1260.00")],
            positions=(pnl_position(POSITION, opened=opened),),
            breakdown=(breakdown_position(POSITION, overnightFeesAndDividends=Decimal("-0.40")),),
            units="4",
        ),
        now=datetime(2026, 7, 1, 0, 30, tzinfo=UTC),
    )
    assert await _warnings(session, ledger) == [
        OTHER,
        "eToro Trading: upload an eToro statement covering 2026-07-01 onward",
    ]


async def test_a_statement_ending_mid_day_leaves_that_day_uncovered(session, run, ledger) -> None:
    """Downloaded on its end day, a statement ends mid-day: the rest of that day is not
    covered, so a difference on it is one a statement explains, and the prompt starts there.
    (A statement ending 23:59:59 covers its last day:
    test_a_difference_on_the_statements_last_day_is_ordinary.)"""
    await run(lambda ctx: set_warnings(ctx, ledger.connection.id, [OTHER]))
    await ledger.normalize(("me", me_json()), ("balances", balances_body()), now=FIRST)
    partial = build_statement(
        summary(username=USERNAME, end="31/10/2026 14:30:00"), [transfer_in()]
    )
    await ledger.upload(partial)
    history = [_trading(date(2026, 10, 31), "1300.00"), _trading(date(2026, 11, 1), "2510.00")]
    await ledger.normalize(
        *_sync("2510.00", history),
        *_transfer_to_trading(1),
        now=datetime(2026, 11, 2, 12, 0, tzinfo=UTC),
    )
    assert await _warnings(session, ledger) == [
        OTHER,
        "eToro Trading: upload an eToro statement covering 2026-10-31 onward",
    ]
