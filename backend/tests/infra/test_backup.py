"""infra/backup/backup.sh: a database dump and a tar of the import documents, both private
(owner-only directory and files, R20)."""

import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

BACKUP_SCRIPT = Path(__file__).parents[3] / "infra" / "backup" / "backup.sh"

# Stands in for `docker compose ... exec`: `db pg_dump` and `api tar` write to stdout. With
# FAKE_API_DOWN set, the api exec fails the way compose does when the container isn't running.
FAKE_DOCKER = """#!/bin/sh
case "$*" in
  *" db pg_dump "*) echo fake-dump ;;
  *" api tar "*)
    if [ -n "${FAKE_API_DOWN:-}" ]; then echo 'service "api" is not running' >&2; exit 1; fi
    echo fake-tar ;;
  *) echo "unexpected: $*" >&2; exit 2 ;;
esac
"""


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def _run(tmp_path: Path, *, existing_dir: bool = False, api_down: bool = False) -> int:
    infra = tmp_path / "infra"
    (infra / "backup").mkdir(parents=True)
    shutil.copy(BACKUP_SCRIPT, infra / "backup" / "backup.sh")
    (infra / ".env").write_text("BACKUP_DIR=backups\n")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "docker").write_text(FAKE_DOCKER)
    (bin_dir / "docker").chmod(0o755)
    if existing_dir:
        (tmp_path / "backups").mkdir(mode=0o755)

    env = {k: v for k, v in os.environ.items() if not k.startswith(("RESTIC_", "FAKE_"))}
    env |= {"PATH": f"{bin_dir}:{env['PATH']}"}
    if api_down:
        env["FAKE_API_DOWN"] = "1"
    return subprocess.run(
        ["bash", str(infra / "backup" / "backup.sh")],
        env=env,
        capture_output=True,
        preexec_fn=lambda: os.umask(0o022),  # a typical permissive login umask
    ).returncode


@pytest.mark.parametrize("existing_dir", [False, True])
def test_backups_are_owner_only(tmp_path: Path, existing_dir: bool) -> None:
    assert _run(tmp_path, existing_dir=existing_dir) == 0

    backups = tmp_path / "backups"
    [dump] = backups.glob("lararium-*.dump")
    [documents] = backups.glob("documents-*.tar")
    assert sorted(p.name for p in backups.iterdir()) == sorted([dump.name, documents.name])
    assert dump.read_text() == "fake-dump\n"
    assert documents.read_text() == "fake-tar\n"
    assert (_mode(backups), _mode(dump), _mode(documents)) == (0o700, 0o600, 0o600)


def test_documents_share_the_dumps_timestamp(tmp_path: Path) -> None:
    assert _run(tmp_path) == 0
    backups = tmp_path / "backups"
    [dump] = backups.glob("lararium-*.dump")
    [documents] = backups.glob("documents-*.tar")
    assert dump.name.removeprefix("lararium-").removesuffix(".dump") == documents.name.removeprefix(
        "documents-"
    ).removesuffix(".tar")


def test_a_missing_api_fails_the_backup(tmp_path: Path) -> None:
    # Without the api container the documents can't be read: no finished backup may claim
    # to be complete, neither the dump nor the tar.
    assert _run(tmp_path, api_down=True) != 0
    finished = [p.name for p in (tmp_path / "backups").iterdir() if not p.name.endswith(".partial")]
    assert finished == []


def test_script_tars_the_document_dir_through_partial_files() -> None:
    script = BACKUP_SCRIPT.read_text()
    assert "exec -T api tar -C /var/lib/lararium/documents -cf - ." in script
    umask = script.index("umask 077")
    for output in ('> "$out.partial"', '> "$out_docs.partial"'):
        assert umask < script.index(output), output
    assert script.index('> "$out_docs.partial"') < script.index('mv "$out_docs.partial"')
