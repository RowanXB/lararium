"""Helpers shared by the staging and commit tests (synthetic Example Bank data)."""

from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID

from pydantic import TypeAdapter
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession

from lararium.modules.finance import models
from lararium.modules.finance.accounts import AccountRecord
from lararium.modules.finance.importing.profiles import (
    ImportProfileRecord,
    ProfileConfig,
    create_profile,
)
from lararium.modules.finance.importing.staging import (
    ImportBatchRecord,
    ImportRowRecord,
    ledger_balance_before,
    list_batch_rows,
)
from lararium.modules.finance.instruments import get_currency

from .synthetic_pdfs import HsbcRow

type Run = Callable[..., Awaitable[Any]]
type MakeAccount = Callable[..., Awaitable[AccountRecord]]
type Commit = Callable[[UUID], Awaitable[ImportBatchRecord]]
type Rollback = Commit

NO_BALANCES = (
    "This file gives no balance to take its opening from, so its opening balance can't be checked"
)
HEADER = "Date,Description,Amount\n"
BALANCE_HEADER = "Date,Description,Amount,Balance\n"
BALANCE_COLUMNS = {
    "date": "Date",
    "description": ["Description"],
    "amount": "Amount",
    "balance": "Balance",
}

PROFILE_CONFIG: TypeAdapter[Any] = TypeAdapter(ProfileConfig)
_names = iter(range(1_000_000))


@asynccontextmanager
async def isolated_example(session: AsyncSession) -> AsyncIterator[None]:
    """Commands commit their own savepoints. Wrap each example in one more so even a
    failing Hypothesis example leaves a clean database for the next shrink attempt."""
    assert isinstance(session.bind, AsyncConnection)
    await session.commit()
    example = await session.bind.begin_nested()
    try:
        yield
    finally:
        await session.rollback()
        await example.rollback()
        session.expunge_all()


async def _profile(run: Run, **config: Any) -> ImportProfileRecord:
    model = PROFILE_CONFIG.validate_python(config)
    return await run(create_profile, name=f"Example Bank {next(_names)}", config=model)


async def _csv_profile(run: Run, **overrides: Any) -> ImportProfileRecord:
    config: dict[str, Any] = {
        "format": "csv",
        "columns": BALANCE_COLUMNS,
        "date_format": "%d/%m/%Y",
    }
    return await _profile(run, **(config | overrides))


type Stage = Callable[..., Awaitable[ImportBatchRecord]]


async def _rows(session: AsyncSession, batch: ImportBatchRecord) -> list[ImportRowRecord]:
    return await list_batch_rows(session, batch.id)


def _statuses(rows: list[ImportRowRecord]) -> list[str]:
    return [row.status for row in rows]


def _adjustments(rows: list[ImportRowRecord]) -> list[ImportRowRecord]:
    return [row for row in rows if row.kind == "opening_adjustment"]


async def _dedupe_keys(session: AsyncSession, batch: ImportBatchRecord) -> list[str | None]:
    r = models.ImportRow
    result = await session.scalars(
        select(r.dedupe_key).where(r.batch_id == batch.id).order_by(r.position)
    )
    return list(result)


def _ofx(
    transactions: list[tuple[str, str, str]],
    *,
    start: str,
    end: str,
    extra: str = "",
    currency: str = "GBP",
) -> str:
    """transactions: (DTPOSTED, TRNAMT, FITID)."""
    body = "\n".join(
        f"<STMTTRN>\n<TRNTYPE>OTHER\n<DTPOSTED>{posted}\n<TRNAMT>{amount}\n<FITID>{fitid}\n"
        f"<NAME>Example Payee {fitid}\n</STMTTRN>"
        for posted, amount, fitid in transactions
    )
    return f"""OFXHEADER:100
DATA:OFXSGML
VERSION:102

<OFX>
<BANKMSGSRSV1>
<STMTTRNRS>
<STMTRS>
<CURDEF>{currency}
<BANKTRANLIST>
<DTSTART>{start}
<DTEND>{end}
{body}
</BANKTRANLIST>
{extra}
</STMTRS>
</STMTTRNRS>
</BANKMSGSRSV1>
</OFX>
"""


NOVEMBER = BALANCE_HEADER + "02/11/2026,Groceries,-18.00,1482.00\n03/11/2026,Bus,-2.00,1480.00\n"
DECEMBER = BALANCE_HEADER + "01/12/2026,Rent,-30.00,1450.00\n"


async def _balance_after(session: AsyncSession, account_id: UUID, day: date) -> Decimal:
    gbp = await get_currency(session, "GBP")
    return await ledger_balance_before(session, account_id, gbp.id, day + timedelta(days=1))


HSBC_ROWS = [
    HsbcRow(
        "VIS", ("EXAMPLE CAFE", "EXAMPLETOWN"), paid_out="4.20", date="08 May 27", balance="95.80"
    ),
    HsbcRow("CR", ("EXAMPLE EMPLOYER",), paid_in="1,250.00", date="10 May 27"),
    HsbcRow(")))", ("EXAMPLE SHOP",), paid_out="17.35", balance="1,328.45"),
]


def _files(root: Path) -> list[Path]:
    """The files under a LocalDocumentStore's root."""
    return [path for path in root.rglob("*") if path.is_file()]
