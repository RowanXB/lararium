"""Migration 0048's downgrade leaves profile configs the older code reads: every CSV config
is stored with family_notes (null where unset), a key the older code refuses."""

import importlib.util
from pathlib import Path
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from lararium.modules.finance import models

from .staging_helpers import Run, Stage, _csv_profile

MIGRATION = (
    Path(__file__).parents[3]
    / "migrations"
    / "versions"
    / "0048_finance_account_family_identifier.py"
)
COLUMNS = {"date": "Date", "type": "Type", "description": ["Description"], "amount": "Amount"}
FAMILY_NOTES = {
    "column": "Notes",
    "types": ["Deposit"],
    "within": [""],
    "outside": [r"Transaction ID: (\S+)"],
}


def _downgrade_steps() -> list[str]:
    spec = importlib.util.spec_from_file_location(MIGRATION.stem, MIGRATION)
    assert spec is not None and spec.loader is not None
    module: Any = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return [s for s in module.DOWNGRADE if "family_notes" in s]


async def test_the_downgrade_removes_family_notes_from_profiles_and_batches(
    session: AsyncSession, run: Run, stage: Stage
) -> None:
    plain = await _csv_profile(run, columns=COLUMNS)
    family = await _csv_profile(run, columns=COLUMNS, family_notes=FAMILY_NOTES)
    content = "Date,Type,Description,Amount,Notes\n10/11/2026,Deposit,Top up,60.00,\n"
    batches = [await stage(profile, content) for profile in (plain, family)]
    p, b = models.ImportProfile, models.ImportBatch
    ids = [plain.id, family.id]
    stored = list(await session.scalars(select(p.config).where(p.id.in_(ids))))
    assert all("family_notes" in config for config in stored)

    for statement in _downgrade_steps():
        await session.execute(text(statement))
    session.expire_all()

    for config in await session.scalars(select(p.config).where(p.id.in_(ids))):
        assert "family_notes" not in config
    for config in await session.scalars(
        select(b.profile_config).where(b.id.in_([batch.id for batch in batches]))
    ):
        assert config is not None and "family_notes" not in config
