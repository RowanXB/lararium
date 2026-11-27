"""Migration 0047: card payment types on stored profiles and staged batches, and the refund
mark on the transactions of committed card rows."""

import importlib.util
from pathlib import Path
from typing import Any
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from lararium.modules.finance import models
from lararium.modules.finance.importing.catalog import CATALOG
from lararium.modules.finance.importing.profiles import PdfProfileConfig

from .staging_helpers import Commit, Run, Stage, _csv_profile, _profile

MIGRATION = (
    Path(__file__).parents[3] / "migrations" / "versions" / "0047_finance_transaction_refund.py"
)
COLUMNS = {
    "date": "Date",
    "type": "Type",
    "description": ["Description"],
    "amount": "Amount",
}
HSBC_TYPES = [")))", "VIS"]


def _migration() -> Any:
    spec = importlib.util.spec_from_file_location(MIGRATION.stem, MIGRATION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def _run_data_steps(session: AsyncSession) -> None:
    """The statements after the column add, which the test database already has."""
    for statement in _migration().UPGRADE[1:]:
        await session.execute(text(statement))
    session.expire_all()


async def _profile_row(session: AsyncSession, profile_id: UUID) -> models.ImportProfile:
    return (
        await session.execute(
            select(models.ImportProfile).where(models.ImportProfile.id == profile_id)
        )
    ).scalar_one()


async def _as_stored_before(session: AsyncSession, profile_id: UUID) -> None:
    """A profile as it was stored before the types existed."""
    await session.execute(
        text(
            "UPDATE finance.import_profiles SET config = config - 'card_payment_types'"
            " WHERE id = :id"
        ),
        {"id": profile_id},
    )


async def test_a_stored_catalog_profile_gains_its_types(session: AsyncSession, run: Run) -> None:
    hsbc = next(e for e in CATALOG if (e.institution, e.label) == ("HSBC UK", "PDF statement"))
    assert isinstance(hsbc.config, PdfProfileConfig)
    stored = await _profile(run, format="pdf", layout="hsbc_uk_statement", card_types=["VIS"])
    owner = await _csv_profile(run, columns=COLUMNS, card_types=["VIS"])
    other_pdf = await _profile(run, format="pdf", layout="lloyds_statement")
    for profile in (stored, owner, other_pdf):
        await _as_stored_before(session, profile.id)
    before = {p.id: (await _profile_row(session, p.id)).version for p in (stored, owner)}

    await _run_data_steps(session)

    changed = await _profile_row(session, stored.id)
    assert changed.config["card_payment_types"] == HSBC_TYPES
    assert changed.version == before[stored.id] + 1
    untouched = await _profile_row(session, owner.id)
    assert "card_payment_types" not in untouched.config
    assert untouched.version == before[owner.id]
    lloyds = await _profile_row(session, other_pdf.id)
    assert lloyds.config["card_payment_types"] == ["DEB"]


async def test_committed_positive_card_rows_become_refunds(
    session: AsyncSession, run: Run, stage: Stage, commit: Commit
) -> None:
    profile = await _csv_profile(run, columns=COLUMNS, card_payment_types=["VIS"])
    content = (
        "Date,Type,Description,Amount\n"
        "10/11/2026,VIS,Card refund,60.00\n"
        "11/11/2026,VIS,Card sale,-100.00\n"
        "12/11/2026,CHQ,Cheque in,25.00\n"
        "13/11/2026,VIS,Voided refund,15.00\n"
    )
    batch = await commit((await stage(profile, content)).id)
    r, t = models.ImportRow, models.Transaction
    await session.execute(text("UPDATE finance.transactions SET refund = false"))
    await session.execute(
        text(
            "UPDATE finance.transactions SET status = 'void' WHERE id IN"
            " (SELECT transaction_id FROM finance.import_rows"
            "  WHERE batch_id = :b AND description = 'Voided refund')"
        ),
        {"b": batch.id},
    )

    await _run_data_steps(session)

    state = await session.execute(
        select(r.description, t.refund)
        .join(t, t.id == r.transaction_id)
        .where(r.batch_id == batch.id, r.kind == "statement")
        .order_by(r.position)
    )
    assert [tuple(x) for x in state] == [
        ("Card refund", True),
        ("Card sale", False),
        ("Cheque in", False),
        ("Voided refund", False),
    ]


async def test_a_staged_batch_gains_the_types(
    session: AsyncSession, run: Run, stage: Stage
) -> None:
    stored = await _profile(run, format="pdf", layout="hsbc_uk_statement")
    content = "Date,Type,Description,Amount\n10/11/2026,VIS,Card refund,60.00\n"
    batch = await stage(await _csv_profile(run, columns=COLUMNS), content)
    await session.execute(
        text(
            "UPDATE finance.import_batches SET profile_id = :p,"
            ' profile_config = \'{"format": "pdf", "layout": "hsbc_uk_statement"}\'::jsonb'
            " WHERE id = :b"
        ),
        {"p": stored.id, "b": batch.id},
    )

    await _run_data_steps(session)

    row = (
        await session.execute(select(models.ImportBatch).where(models.ImportBatch.id == batch.id))
    ).scalar_one()
    assert row.profile_config["card_payment_types"] == HSBC_TYPES
