"""`make real-check` (README, "Verifying against your real statements"): which checks it runs
for which variables. Dry runs only (`make -n`): nothing is installed or run."""

import subprocess
from pathlib import Path

REPO = Path(__file__).parents[2]
BANKS = "tests/finance/importing/test_real_files.py"
ETORO = "tests/finance/etoro/test_real_statement.py"


def _dry_run(*variables: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["make", "-n", "-C", str(REPO), "real-check", *variables],
        capture_output=True,
        text=True,
        check=False,
    )


def test_real_check_accepts_etoro_statement() -> None:
    result = _dry_run("ETORO_STATEMENT=/synthetic/statement.xlsx")
    assert result.returncode == 0, result.stderr
    assert "LARARIUM_ETORO_STATEMENT=" in result.stdout
    assert "/synthetic/statement.xlsx" in result.stdout
    assert ETORO in result.stdout
    assert BANKS not in result.stdout


def test_real_check_with_real_root_only_runs_the_bank_files() -> None:
    result = _dry_run("REAL_ROOT=/synthetic/banks")
    assert result.returncode == 0, result.stderr
    assert 'LARARIUM_REAL_ROOT="$root"' in result.stdout
    assert BANKS in result.stdout
    assert ETORO not in result.stdout


def test_real_check_runs_both_when_both_are_set() -> None:
    result = _dry_run("REAL_ROOT=/synthetic/banks", "ETORO_STATEMENT=/synthetic/statement.xlsx")
    assert result.returncode == 0, result.stderr
    assert BANKS in result.stdout
    assert ETORO in result.stdout


def test_real_check_needs_one_of_them() -> None:
    result = _dry_run()
    assert result.returncode != 0
    assert "REAL_ROOT" in result.stderr
    assert "ETORO_STATEMENT" in result.stderr
