"""Check a restored database: `python -m lararium.restore_check`, from the backend directory
(it reads alembic.ini there), with LARARIUM_MIGRATION_DATABASE_URL naming the database.

The restore rehearsal (infra/backup/rehearse-restore.sh) runs it after `alembic upgrade head`.
It fails (exit 1) when the migration revision is not the code's head, when a transaction's
postings don't balance, or when a transaction breaks the transfer invariant. It prints
transaction ids and counts only, never amounts or descriptions.
"""

import asyncio
import os
import sys
from collections.abc import Sequence
from pathlib import Path
from uuid import UUID

from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import Connection
from sqlalchemy.ext.asyncio import AsyncSession

from lararium.db import create_engine
from lararium.modules.finance.ledger import find_unbalanced_transactions
from lararium.modules.finance.transfers import find_transfer_violations

# Enough ids to look a few up; the count says how many there are in all.
SHOWN_IDS = 20


def code_heads(alembic_ini: Path) -> set[str]:
    """The head revisions of the migrations next to alembic.ini."""
    return set(ScriptDirectory.from_config(Config(str(alembic_ini))).get_heads())


def migration_problem(connection: Connection, heads: set[str]) -> str | None:
    current = set(MigrationContext.configure(connection).get_current_heads())
    if current == heads:
        return None
    return f"migration revision {sorted(current)} is not the code's head {sorted(heads)}"


def _ids(what: str, ids: Sequence[UUID]) -> str:
    shown = ", ".join(str(i) for i in ids[:SHOWN_IDS])
    more = f" and {len(ids) - SHOWN_IDS} more" if len(ids) > SHOWN_IDS else ""
    return f"{len(ids)} {what}: {shown}{more}"


async def ledger_problems(session: AsyncSession) -> list[str]:
    problems: list[str] = []
    if unbalanced := await find_unbalanced_transactions(session):
        problems.append(_ids("unbalanced transactions", unbalanced))
    if violations := await find_transfer_violations(session):
        problems.append(_ids("transactions breaking the transfer invariant", violations))
    return problems


async def check(url: str, alembic_ini: Path) -> list[str]:
    heads = code_heads(alembic_ini)
    engine = create_engine(url)
    try:
        async with engine.connect() as connection:
            problem = await connection.run_sync(lambda c: migration_problem(c, heads))
            problems = [problem] if problem else []
            async with AsyncSession(bind=connection) as session:
                problems += await ledger_problems(session)
            await connection.rollback()  # read only
    finally:
        await engine.dispose()
    return problems


def main() -> int:
    url = os.environ.get("LARARIUM_MIGRATION_DATABASE_URL")
    if not url:
        print("set LARARIUM_MIGRATION_DATABASE_URL to the restored database", file=sys.stderr)
        return 2
    problems = asyncio.run(check(url, Path("alembic.ini")))
    for problem in problems:
        print(f"FAIL {problem}")
    if problems:
        return 1
    print("ok: migrations at head, every transaction balances, the transfer invariant holds")
    return 0


if __name__ == "__main__":
    sys.exit(main())
