"""Model-based checks of overlapping statement imports on a transactions account."""

from collections import Counter
from datetime import date, timedelta
from decimal import Decimal

import pytest
from hypothesis import event, example, given
from hypothesis import strategies as st
from sqlalchemy.ext.asyncio import AsyncSession

from lararium.modules.finance.accounts import AccountRecord
from lararium.modules.finance.errors import ImportBatchStateError
from lararium.modules.finance.importing.profiles import ImportProfileRecord
from lararium.modules.finance.importing.staging import ImportBatchRecord
from lararium.modules.finance.ledger import find_unbalanced_transactions
from lararium.modules.finance.valuation import net_worth_series

from .staging_helpers import (
    BALANCE_HEADER,
    Commit,
    Rollback,
    Run,
    Stage,
    _balance_after,
    _csv_profile,
    _ofx,
    _profile,
    _rows,
    isolated_example,
)

# A small pool deliberately generates multiple unrelated movements of the same amount.
CENTS = st.one_of(st.sampled_from([-100, -1, 1, 100]), st.integers(1, 9999), st.integers(-9999, -1))
DAY = timedelta(days=1)


@st.composite
def overlapping_days(draw: st.DrawFn) -> tuple[list[list[int]], int]:
    daily = draw(st.lists(st.lists(CENTS, min_size=1, max_size=3), min_size=3, max_size=6))
    return daily, draw(st.integers(1, len(daily) - 2))


@pytest.mark.parametrize("export_format", ["ofx", "qif"])
@example(start=date(2025, 1, 1), opening_cents=0, history=([[-1, 1, -1], [100], [100]], 1))
@given(
    start=st.dates(min_value=date(2025, 1, 1), max_value=date(2027, 12, 31)),
    opening_cents=st.integers(-100_000, 100_000),
    history=overlapping_days(),
)
async def test_overlapping_exports_preserve_the_ledger(
    session: AsyncSession,
    account: AccountRecord,
    run: Run,
    stage: Stage,
    commit: Commit,
    rollback: Rollback,
    export_format: str,
    start: date,
    opening_cents: int,
    history: tuple[list[list[int]], int],
) -> None:
    # The balance-bearing CSV omits the last day. The other export starts later and
    # overlaps it by one or more whole days, with different descriptions and row keys.
    daily_cents, cut = history
    balance = Decimal(opening_cents).scaleb(-2)
    balances = {}
    movements = []
    csv_rows = []
    ofx_rows = []
    qif = "!Type:Bank\n"
    for i, amounts in enumerate(daily_cents):
        day = start + i * DAY
        for cents in amounts:
            amount = Decimal(cents).scaleb(-2)
            balance += amount
            movements.append((day, amount))
            if i < len(daily_cents) - 1:
                csv_rows.append(f"{day:%d/%m/%Y},CSV payee,{amount},{balance}\n")
            if i >= cut:
                ofx_rows.append((f"{day:%Y%m%d}", str(amount), f"id-{len(movements)}"))
                qif += f"D{day:%d/%m/%Y}\nT{amount}\nPExport payee\n^\n"
        balances[day] = balance

    end = start + (len(daily_cents) - 1) * DAY
    first_export_day = start + cut * DAY
    async with isolated_example(session):
        csv_profile = await _csv_profile(run)
        csv = BALANCE_HEADER + "".join(csv_rows)
        # A debit, its reversal and the same debit can have identical printed fields: each
        # is a row of its own, by its occurrence in the file (owner ruling 2026-11-19).
        if len(csv_rows) != len(set(csv_rows)):
            event("repeated balance key")
        if export_format == "ofx":
            export_profile = await _profile(run, format="ofx")
            content = _ofx(
                ofx_rows,
                start=f"{first_export_day:%Y%m%d}",
                end=f"{end:%Y%m%d}",
                extra=f"<LEDGERBAL>\n<BALAMT>{balance}\n<DTASOF>{end:%Y%m%d}\n</LEDGERBAL>",
            )
        else:
            export_profile = await _profile(run, format="qif", date_format="%d/%m/%Y")
            content = qif

        async def worth() -> list[Decimal]:
            return [point.value for point in await net_worth_series(session, start, end)]

        type File = tuple[ImportProfileRecord, str, str]
        earlier: File = (csv_profile, csv, "history.csv")
        later: File = (export_profile, content, f"export.{export_format}")

        async def post(file: File) -> ImportBatchRecord:
            profile, contents, filename = file
            return await commit((await stage(profile, contents, filename=filename)).id)

        for first, second in ((earlier, later), (later, earlier)):
            before = await worth()
            assert before == [0] * len(balances)
            first_batch = await post(first)
            first_only = await worth()
            closings = list(balances.values())
            if first == earlier:
                assert first_only[:-1] == closings[:-1]
            else:
                # OFX supplies an anchored closing. QIF supplies movements only.
                baseline = 0 if export_format == "ofx" else closings[cut - 1]
                assert first_only[cut:] == [value - baseline for value in closings[cut:]]
            second_batch = await post(second)
            posted = Counter(
                [
                    (row.booking_date, row.quantity)
                    for batch in (first_batch, second_batch)
                    for row in await _rows(session, batch)
                    if row.kind == "statement" and row.status == "committed"
                ]
            )
            assert posted == Counter(movements)
            assert await worth() == closings
            for day, expected in balances.items():
                assert await _balance_after(session, account.id, day) == expected
            assert await find_unbalanced_transactions(session) == []

            # The overlapping rows in the second batch still depend on the first.
            with pytest.raises(ImportBatchStateError):
                await rollback(first_batch.id)
            replay = await post(first)
            rows = [r for r in await _rows(session, replay) if r.kind == "statement"]
            expected_days = daily_cents[:-1] if first == earlier else daily_cents[cut:]
            assert len(rows) == sum(map(len, expected_days))
            assert all(r.status == "duplicate" for r in rows)
            assert await worth() == closings
            await rollback(replay.id)
            await rollback(second_batch.id)
            assert await worth() == first_only
            await rollback(first_batch.id)
            assert await worth() == before
            assert await find_unbalanced_transactions(session) == []
