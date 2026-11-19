"""The owner's real statements, end to end, on the throwaway test database: `make real-check
REAL_ROOT=<dir>` (README, "Verifying against your real statements"). Skipped unless
LARARIUM_REAL_ROOT is set, so `make check` and CI never run it.

<dir> holds one folder per account, its name starting HSBC, Lloy, BOC or Revolut, each with
one account's files in any of the catalog's formats. A Revolut folder's files say which
account they are for (current or savings). In two orders (statements first, exports first) every
file must commit with no error row, every statement's closing balance must hold on its own
date, and rolling everything back, newest first, must be allowed and leave a zero balance.

Nothing of the files is printed or kept: only counts and pass or fail. Error text has its
digits and the file names masked, and the stored documents live in a temporary directory
removed at the end.
"""

import os
import re
import tempfile
from collections import Counter
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from lararium.core.documents import LocalDocumentStore
from lararium.modules.finance.importing.catalog import CATALOG
from lararium.modules.finance.importing.commit import commit_batch, rollback_batch
from lararium.modules.finance.importing.parsed import ParsedStatement
from lararium.modules.finance.importing.pdf.words import visible_lines
from lararium.modules.finance.importing.profiles import create_profile
from lararium.modules.finance.importing.staging import (
    _parse,  # pyright: ignore[reportPrivateUsage]
    ledger_balance_before,
    stage_import,
)
from lararium.modules.finance.instruments import InstrumentRecord

from .staging_helpers import MakeAccount, Run, _rows

ROOT = os.environ.get("LARARIUM_REAL_ROOT")
REVOLUT = "Revolut"
pytestmark = pytest.mark.skipif(not ROOT, reason="set LARARIUM_REAL_ROOT (make real-check)")

# A bank folder's name starts with one of these (compared case-insensitively); a Revolut
# folder's institution is told by its files.
BANKS = {"hsbc": "HSBC UK", "lloy": "Lloyds", "boc": "BOC UK", "revolut": REVOLUT}
PDF = "PDF statement"


def _revolut_account(path: Path) -> str:
    """Revolut Savings for the savings exports, Revolut Current for any other file."""
    content = path.read_bytes()
    if path.suffix.lower() == ".pdf":
        lines = visible_lines(content, x_tolerance=Decimal("1.5"))
        savings = any(
            " ".join(word.text for word in line) == "Savings Statement" for line in lines[:3]
        )
    else:
        savings = content.lstrip(b"\xef\xbb\xbf").startswith(b"Date,Description,Money out")
    return "Revolut Savings" if savings else "Revolut Current"


def _institution(folder: Path, institution: str) -> str:
    if institution != REVOLUT:
        return institution
    found = {_revolut_account(path) for path in folder.iterdir() if path.is_file()}
    assert len(found) == 1, f"{folder.name}: holds files of {len(found)} Revolut accounts"
    return found.pop()


def _label(institution: str, path: Path) -> str | None:
    """The catalog format of a file, or None when the catalog has none for it."""
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        return PDF
    if suffix == ".csv":
        if institution != "HSBC UK":
            return "CSV"
        head = path.read_bytes().lstrip(b"\xef\xbb\xbf")
        return "midata CSV" if head.startswith(b"Date,Type") else "CSV (transaction list)"
    return {".ofx": "OFX", ".qif": "QIF"}.get(suffix)


def _mask(message: str, names: list[str]) -> str:
    for name in names:
        message = message.replace(name, "<file>")
    return re.sub(r"\d", "9", message)


def _banks(root: Path) -> list[tuple[Path, str]]:
    found = [
        (folder, _institution(folder, institution))
        for folder in sorted(root.iterdir())
        if folder.is_dir()
        for prefix, institution in BANKS.items()
        if folder.name.lower().startswith(prefix)
    ]
    assert found, f"no folder starting {', '.join(BANKS)} in LARARIUM_REAL_ROOT"
    return found


@pytest.mark.parametrize("order", ["statements_first", "exports_first"])
async def test_real_files_end_to_end(
    session: AsyncSession, run: Run, make_account: MakeAccount, gbp: InstrumentRecord, order: str
) -> None:
    assert ROOT is not None
    failures: list[str] = []
    with tempfile.TemporaryDirectory() as documents:
        store = LocalDocumentStore(Path(documents))
        for folder, institution in _banks(Path(ROOT)):
            failures += await _bank(
                session, run, make_account, gbp, store, folder, institution, order
            )
    assert not failures, "\n".join(failures)


@dataclass(frozen=True)
class _File:
    label: str
    content: bytes
    statement: ParsedStatement


def _load(folder: Path, institution: str, tag: str) -> tuple[list[_File], list[str], list[str]]:
    """(the folder's files in catalog formats, their names, failures), read and parsed."""
    paths = sorted(path for path in folder.iterdir() if path.is_file())
    names = [path.name for path in paths]
    entries = {entry.label: entry for entry in CATALOG if entry.institution == institution}
    loaded: list[_File] = []
    failures: list[str] = []
    for path in paths:
        label = _label(institution, path)
        if label is None or label not in entries:
            failures.append(f"{tag}: a {path.suffix or 'suffixless'} file has no catalog format")
            continue
        content = path.read_bytes()
        try:
            statement, _ = _parse(content, entries[label].config, ZoneInfo("Europe/London"))
        except Exception as error:  # reported, masked, as a failure
            kind = f"{type(error).__name__}: {_mask(str(error), names)}"
            failures.append(f"{tag}: a {label} doesn't parse ({kind})")
            continue
        loaded.append(_File(label, content, statement))
    return loaded, names, failures


async def _bank(
    session: AsyncSession,
    run: Run,
    make_account: MakeAccount,
    gbp: InstrumentRecord,
    store: LocalDocumentStore,
    folder: Path,
    institution: str,
    order: str,
) -> list[str]:
    tag = f"{folder.name} [{order}]"
    files, names, failures = _load(folder, institution, tag)
    entries = {entry.label: entry for entry in CATALOG if entry.institution == institution}
    statements = sorted(
        (file for file in files if file.label == PDF),
        key=lambda file: file.statement.period or (date.min, date.min),
    )
    exports = [file for file in files if file.label != PDF]
    ordered = statements + exports if order == "statements_first" else exports + statements

    account = await make_account(name=f"Real {folder.name}", balance_mode="transactions")
    profiles = {}
    committed = []
    outcome: Counter[str] = Counter()
    for number, file in enumerate(ordered):
        label = file.label
        if label not in profiles:
            config = entries[label].config
            profiles[label] = await run(
                create_profile, name=f"{folder.name} {label}", config=config
            )
        try:
            batch = await run(
                stage_import,
                store,
                account_id=account.id,
                profile_id=profiles[label].id,
                content=file.content,
                filename=f"{folder.name}-{number}",
                media_type="application/octet-stream",
                default_instrument_id=None if label == PDF else gbp.id,
            )
            errors = Counter(
                _mask(row.error or "", names)
                for row in await _rows(session, batch)
                if row.status == "error"
            )
            if errors:
                failures.append(f"{tag}: a {label} staged with error rows {dict(errors)}")
                continue
            await run(commit_batch, batch.id)
            session.expunge_all()
        except Exception as error:  # reported, masked, as a failure
            failures.append(
                f"{tag}: a {label} refused ({type(error).__name__}: {_mask(str(error), names)})"
            )
            continue
        committed.append(batch.id)
        outcome[label] += 1

    closings = [
        (file.statement.closing_balance_date, file.statement.closing_balance)
        for file in files
        if file.statement.closing_balance is not None
        and file.statement.closing_balance_date is not None
    ]
    held = 0
    for day, closing in closings:
        assert day is not None
        balance = await ledger_balance_before(session, account.id, gbp.id, day + timedelta(days=1))
        held += balance == closing
    if held != len(closings):
        failures.append(
            f"{tag}: {len(closings) - held} of {len(closings)} closings don't hold on their date"
        )

    refused = 0
    for batch_id in reversed(committed):
        try:
            await run(rollback_batch, batch_id)
            session.expunge_all()
        except Exception:  # counted as a failure
            refused += 1
    if refused:
        failures.append(f"{tag}: {refused} of {len(committed)} rollbacks refused")
    if await ledger_balance_before(session, account.id, gbp.id, date.max) != 0:
        failures.append(f"{tag}: the balance isn't 0 after every rollback")

    print(
        f"\n{tag}: {len(names)} files, committed {dict(outcome)}; "
        f"closings holding {held}/{len(closings)}; rollbacks refused {refused}"
    )
    return failures
