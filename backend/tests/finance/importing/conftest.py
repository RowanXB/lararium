from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from lararium.core.documents import LocalDocumentStore
from lararium.modules.finance.accounts import AccountRecord
from lararium.modules.finance.importing.commit import commit_batch, rollback_batch
from lararium.modules.finance.importing.profiles import (
    CsvColumns,
    CsvProfileConfig,
    ImportProfileRecord,
    create_profile,
)
from lararium.modules.finance.importing.staging import ImportBatchRecord, stage_import
from lararium.modules.finance.instruments import InstrumentRecord

type Run = Callable[..., Awaitable[Any]]
type MakeAccount = Callable[..., Awaitable[AccountRecord]]


@pytest.fixture
def store(tmp_path: Path) -> LocalDocumentStore:
    return LocalDocumentStore(tmp_path)


@pytest.fixture
async def csv_profile(run: Run) -> ImportProfileRecord:
    config = CsvProfileConfig(
        format="csv",
        columns=CsvColumns(date="Date", description=["Description"], amount="Amount"),
        date_format="%d/%m/%Y",
    )
    return await run(create_profile, name="Example Bank CSV", config=config)


@pytest.fixture
async def account(make_account: MakeAccount) -> AccountRecord:
    return await make_account(name="Example Current", balance_mode="transactions")


@pytest.fixture
def stage(
    run: Run, store: LocalDocumentStore, account: AccountRecord, gbp: InstrumentRecord
) -> Callable[..., Awaitable[ImportBatchRecord]]:
    async def _stage(
        profile: ImportProfileRecord,
        content: str | bytes,
        *,
        account_id: UUID | None = None,
        default: UUID | str | None = "gbp",
        filename: str = "statement.csv",
        media_type: str = "text/csv",
    ) -> ImportBatchRecord:
        return await run(
            stage_import,
            store,
            account_id=account.id if account_id is None else account_id,
            profile_id=profile.id,
            content=content.encode() if isinstance(content, str) else content,
            filename=filename,
            media_type=media_type,
            default_instrument_id=gbp.id if default == "gbp" else default,
        )

    return _stage


@pytest.fixture
def commit(run: Run, session: AsyncSession) -> Callable[[UUID], Awaitable[ImportBatchRecord]]:
    async def _commit(batch_id: UUID) -> ImportBatchRecord:
        record = await run(commit_batch, batch_id)
        session.expunge_all()
        return record

    return _commit


@pytest.fixture
def rollback(run: Run, session: AsyncSession) -> Callable[[UUID], Awaitable[ImportBatchRecord]]:
    async def _rollback(batch_id: UUID) -> ImportBatchRecord:
        record = await run(rollback_batch, batch_id)
        session.expunge_all()
        return record

    return _rollback
