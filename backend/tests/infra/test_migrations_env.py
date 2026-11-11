"""`alembic` must run with only LARARIUM_MIGRATION_DATABASE_URL set, as compose `migrate` does."""

import os
import subprocess
import sys
from pathlib import Path

BACKEND = Path(__file__).parents[2]


def _alembic(*args: str, **env: str) -> subprocess.CompletedProcess[str]:
    clean = {k: v for k, v in os.environ.items() if not k.startswith("LARARIUM_")}
    return subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=BACKEND,
        env=clean | env,
        capture_output=True,
        text=True,
        check=False,
    )


def test_needs_only_the_migration_url(owner_url: str) -> None:
    result = _alembic("current", LARARIUM_MIGRATION_DATABASE_URL=owner_url)
    assert result.returncode == 0, result.stderr


def test_missing_migration_url_says_what_to_set() -> None:
    result = _alembic("current")
    assert result.returncode != 0
    assert "Set LARARIUM_MIGRATION_DATABASE_URL" in result.stderr
