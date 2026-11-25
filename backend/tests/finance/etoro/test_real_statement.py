"""The owner's real eToro account statement, end to end on the throwaway test database: `make
real-check ETORO_STATEMENT=<xlsx>` (README, "Verifying against your real statements"). The real
check is skipped unless LARARIUM_ETORO_STATEMENT is set, so `make check` and CI never run it;
the synthetic tests below run the same check on invented statements.

The statement must parse with nothing flagged; its own running balance must hold, per second,
under the cash rule the normalizer posts by (statement_ledger.cash; design §5.11); and its
upload, on a connection whose first sync is stood in for (its `/me` and balances, invented but
for the statement's username and currency), must refuse nothing, type every row and post
transactions that balance. The only flag allowed is the one that waits for the API: an open
position no source can place (the API's portfolio places it).

Nothing of the file is printed or kept: only counts and pass or fail. Error text has its digits
masked, and the stored document lives in pytest's temporary directory.
"""

import asyncio
import dataclasses
import os
import re
import uuid
from collections import Counter, defaultdict
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import text

from lararium.core.documents import LocalDocumentStore
from lararium.modules.finance.etoro import statement_ledger
from lararium.modules.finance.etoro import sync as etoro_sync
from lararium.modules.finance.etoro.fields import (
    ACCOUNT_ACTIVITY,
    ACCOUNT_SUMMARY,
    CLOSED_POSITIONS,
    DIVIDENDS,
    FINANCIAL_SUMMARY,
    STATEMENT_COLUMNS,
    STATEMENT_SHEETS,
)
from lararium.modules.finance.etoro.normalize import NormalizeReport
from lararium.modules.finance.etoro.statement import (
    STATEMENT_ZONE,
    EtoroStatement,
    parse_statement,
)
from lararium.modules.finance.ledger import find_unbalanced_transactions

from .fixtures import CASH_ACCOUNT, TRADING_ACCOUNT, balance_entry, balances_body, me_json
from .statement_fixtures import (
    ISIN,
    POSITION_1,
    POSITION_2,
    USERNAME,
    activity_row,
    build_statement,
    closed_row,
    dividend_row,
    summary,
)
from .test_normalize_api import Ledger, _connection

PATH = os.environ.get("LARARIUM_ETORO_STATEMENT")
# Flags that only say the API's data is missing, which this check does not have.
WITHOUT_THE_API = frozenset({statement_ledger.UNPLACED})
# Each sheet's typed table, and the column that tells a row of it typed.
TYPED = {
    ACCOUNT_ACTIVITY: ("etoro_activity", "row_key"),
    CLOSED_POSITIONS: ("etoro_trades", STATEMENT_COLUMNS["etoro_trades"]["Position ID"]),
    DIVIDENDS: ("etoro_dividends", "row_key"),
    FINANCIAL_SUMMARY: ("etoro_financial_summary", "row_key"),
}


def _mask(message: str) -> str:
    return re.sub(r"\d", "9", message)


def _activity(statement: EtoroStatement) -> list[statement_ledger.Row]:
    stored = datetime.now(UTC)
    return [
        statement_ledger.Row(
            uuid.uuid4(),
            row.sheet,
            row.key,
            row.cells,
            statement.period_from,
            statement.period_to,
            "",
            stored,
        )
        for row in statement.rows
        if row.sheet == ACCOUNT_ACTIVITY
    ]


def running_balance(statement: EtoroStatement) -> tuple[int, int, int]:
    """(seconds checked, seconds whose balance does not hold, rows without a time, cash or
    balance). Rows sharing a second are not listed in balance order, so the balance after a
    second's rows, one of their balances, must be the balance before plus their cash. The
    first second has no balance before it: each of its balances is tried as its end, and the
    one that leaves the fewest failures is taken."""
    seconds: defaultdict[datetime, list[tuple[Decimal, Decimal]]] = defaultdict(list)
    unread = 0
    for row in _activity(statement):
        try:
            when = statement_ledger.moment(row, "Date")
            cash = statement_ledger.cash(row)
            balance = statement_ledger.number(row, "Balance")
        except Exception:  # counted, never shown
            when = cash = balance = None
        if when is None or cash is None or balance is None:
            unread += 1
            continue
        seconds[when].append((cash, balance))
    groups = [seconds[when] for when in sorted(seconds)]
    if not groups:
        return 0, 0, unread

    def failures(end: Decimal) -> int:
        failed = 0
        for group in groups[1:]:
            expected = end + sum((cash for cash, _ in group), Decimal(0))
            balances = {balance for _, balance in group}
            if expected in balances:
                end = expected
            else:
                failed += 1
                end = min(balances)  # carry on from one of the statement's own balances
        return failed

    return len(groups), min(failures(end) for _, end in groups[0]), unread


async def _counts(session, sql: str, connection_id: uuid.UUID) -> Counter[str]:
    rows = await session.execute(text(sql), {"c": connection_id})
    return Counter({str(kind): int(count) for kind, count in rows.all()})


async def check_statement(session, run, monkeypatch, documents: Path, body: bytes) -> list[str]:
    """Run the check on one statement; print its counts and return its failures (counts and
    fixed text only)."""
    failures: list[str] = []
    try:
        statement = parse_statement(body, STATEMENT_ZONE)
    except Exception as error:  # reported, masked, as a failure
        kind = f"{type(error).__name__}: {_mask(str(error))}"
        failures.append(f"the statement doesn't parse ({kind})")
        print("\neToro statement: not read\neToro statement: fail")
        return failures
    read = Counter(row.sheet for row in statement.rows)
    if statement.flagged:
        failures.append(f"the reader flagged {len(statement.flagged)} columns, labels or sheets")

    checked, unbalanced_seconds, unread = running_balance(statement)
    if unbalanced_seconds:
        failures.append(
            f"{unbalanced_seconds} of {checked} seconds don't hold the statement's balance"
        )
    if unread:
        failures.append(f"{unread} activity rows have no time, cash or balance")

    ledger = Ledger(session, await _connection(run))
    currency = statement.account_currency
    balances = balances_body() | {
        "balances": [
            balance_entry(TRADING_ACCOUNT, "Trading", currency, "0"),
            balance_entry(CASH_ACCOUNT, "Cash", "GBP", "0"),
        ]
    }
    me = me_json() | {"username": statement.username}
    await ledger.normalize(("me", me), ("balances", balances), now=datetime.now(UTC))

    reports: list[NormalizeReport] = []
    normalize = etoro_sync.normalize

    async def spy(ctx, raw_record_ids):
        report = await normalize(ctx, raw_record_ids)
        reports.append(report)
        return report

    monkeypatch.setattr(etoro_sync, "normalize", spy)
    ctx = dataclasses.replace(await ledger.context(), documents=LocalDocumentStore(documents))
    try:
        await etoro_sync.ingest_statement(ctx, body, "statement.xlsx", statement)
    except Exception as error:  # reported, masked, as a failure
        failures.append(f"the upload is refused ({type(error).__name__}: {_mask(str(error))})")
        print(f"\neToro statement: rows read {dict(sorted(read.items()))}; upload refused")
        print("eToro statement: fail")
        return failures
    [report] = reports

    connection_id = ledger.connection.id
    stored = await _counts(
        session,
        "select record_type, count(*) from core.raw_records"
        " where connection_id = :c and record_type like 'etoro.statement.%' group by 1",
        connection_id,
    )
    typed: Counter[str] = Counter()
    for sheet, (table, column) in TYPED.items():
        typed[sheet] = int(
            await session.scalar(
                text(
                    f"select count(*) from finance.{table}"
                    f" where connection_id = :c and {column} is not null"
                ),
                {"c": connection_id},
            )
        )
    typed[ACCOUNT_SUMMARY] = read[ACCOUNT_SUMMARY] * int(
        await session.scalar(
            text("select count(*) from finance.etoro_statements where connection_id = :c"),
            {"c": connection_id},
        )
        == 1
    )
    for sheet in STATEMENT_SHEETS:
        if stored[STATEMENT_SHEETS[sheet].record_type] != read[sheet]:
            failures.append(f"{sheet}: not every row is stored")
        if typed[sheet] != read[sheet]:
            failures.append(f"{sheet}: {read[sheet] - typed[sheet]} rows aren't typed")
    transactions = await _counts(
        session,
        "select split_part(external_id, ':', 1), count(*) from finance.transactions"
        " where connection_id = :c and status <> 'void' group by 1",
        connection_id,
    )
    unbalanced = len(await find_unbalanced_transactions(session))
    if unbalanced:
        failures.append(f"{unbalanced} transactions don't balance")
    if report.flagged:
        failures.append(f"the normalizer flagged {len(report.flagged)} fields, values or types")
    waiting = report.flags_in_full & WITHOUT_THE_API
    if other := report.flags_in_full - WITHOUT_THE_API:
        failures.append(f"the normalizer flagged {len(other)} instruments or positions")

    print(
        f"\neToro statement: rows read {dict(sorted(read.items()))}; "
        f"seconds holding their balance {checked - unbalanced_seconds}/{checked}; "
        f"rows typed {sum(typed.values())}/{sum(read.values())}; "
        f"transactions {dict(sorted(transactions.items()))}, unbalanced {unbalanced}; "
        f"flags: reader {len(statement.flagged)}, normalizer {len(report.flagged)}, "
        f"instruments or positions {len(other)}, waiting for the API {len(waiting)}"
    )
    print(f"eToro statement: {'fail' if failures else 'pass'}")
    return failures


@pytest.mark.skipif(not PATH, reason="set LARARIUM_ETORO_STATEMENT (make real-check)")
async def test_real_statement(session, run, monkeypatch, tmp_path) -> None:
    assert PATH is not None
    body = await asyncio.to_thread(Path(PATH).read_bytes)
    failures = await check_statement(session, run, monkeypatch, tmp_path, body)
    assert not failures, "\n".join(failures)


# The same check on synthetic statements ------------------------------------------------------

OPENED = "02/10/2026 14:30:05"
CLOSED = "20/10/2026 15:01:44"


def _activity_rows(close_balance: str = "1280.85") -> list[dict[str, str]]:
    """A transfer in with its fee, one position opened, paid a dividend and closed, and one
    position opened and still held."""
    return [
        activity_row(
            "01/10/2026 10:00:00",
            "Transfer: GBP > USD",
            "1260.00",
            details="GBP",
            equity="1260.00",
            balance="1260.00",
        ),
        activity_row(
            "01/10/2026 10:00:00",
            "Deposit Conversion Fee",
            "-6.30",
            change="0",
            equity="1260.00",
            balance="1260.00",
        ),
        activity_row(
            OPENED,
            "Open Position",
            "500",
            details="ZZQA/USD",
            units="4",
            position_id=POSITION_1,
            change="0",
            balance="760.00",
            asset_type="Stocks",
        ),
        activity_row(
            "15/10/2026 12:00:00",
            "Dividend",
            "0.85",
            details="ZZQA/USD",
            position_id=POSITION_1,
            balance="760.85",
            asset_type="Stocks",
        ),
        activity_row(
            CLOSED,
            "Position closed",
            "520.00",
            details="ZZQA/USD",
            units="4",
            position_id=POSITION_1,
            change="20.00",
            equity="1280.85",
            balance=close_balance,
            asset_type="Stocks",
        ),
        activity_row(
            "25/10/2026 09:00:00",
            "Open Position",
            "100",
            details="ZZQB/USD",
            units="2",
            position_id=POSITION_2,
            change="0",
            balance="1180.85",
            asset_type="Stocks",
        ),
    ]


def _statement(activity: list[dict[str, str]]) -> bytes:
    return build_statement(
        summary(),
        activity,
        [closed_row()],
        [dividend_row()],
        financial=[
            {
                "Name": "Dividends",
                "01/10/2026 - 31/10/2026": "0.85",
                "Year to date": "0.85",
                "Total": "0.85",
            }
        ],
    )


# What the synthetic statement holds that a printed line must not.
_VALUES = [
    USERNAME,
    "Synthetic",
    POSITION_1,
    POSITION_2,
    ISIN,
    "ZZQA",
    "ZZQB",
    "1260",
    "1280.85",
    "1180.85",
    "760",
    "0.85",
    "2026",
    "/10/",
]


def _assert_counts_only(output: str) -> None:
    for value in _VALUES:
        assert value not in output, value


async def test_synthetic_statement_passes_with_counts_only(
    session, run, monkeypatch, tmp_path, capsys
) -> None:
    failures = await check_statement(
        session, run, monkeypatch, tmp_path, _statement(_activity_rows())
    )
    output = capsys.readouterr().out
    assert failures == []
    assert "seconds holding their balance 5/5" in output
    assert "rows typed 21/21" in output
    assert "transactions {'activity': 2, 'close': 1, 'open': 1}, unbalanced 0" in output
    assert "waiting for the API 1" in output
    assert "eToro statement: pass" in output
    _assert_counts_only(output)


async def test_synthetic_statement_whose_balance_breaks_fails_with_counts_only(
    session, run, monkeypatch, tmp_path, capsys
) -> None:
    failures = await check_statement(
        session, run, monkeypatch, tmp_path, _statement(_activity_rows(close_balance="1290.85"))
    )
    output = capsys.readouterr().out + "\n".join(failures)
    assert failures == ["2 of 5 seconds don't hold the statement's balance"]
    assert "eToro statement: fail" in output
    _assert_counts_only(output)


async def test_unreadable_statement_fails_with_counts_only(
    session, run, monkeypatch, tmp_path, capsys
) -> None:
    failures = await check_statement(session, run, monkeypatch, tmp_path, b"not a workbook")
    output = capsys.readouterr().out + "\n".join(failures)
    assert len(failures) == 1 and "doesn't parse" in failures[0]
    _assert_counts_only(output)


async def test_refused_upload_fails_with_counts_only(
    session, run, monkeypatch, tmp_path, capsys
) -> None:
    # A first statement that starts after the account's opening is refused, naming that day.
    body = build_statement(summary(start="05/10/2026 00:00:00"), _activity_rows()[2:])
    failures = await check_statement(session, run, monkeypatch, tmp_path, body)
    output = capsys.readouterr().out + "\n".join(failures)
    assert len(failures) == 1 and "the upload is refused (StatementRejected" in failures[0]
    assert "eToro statement: fail" in output
    _assert_counts_only(output)
