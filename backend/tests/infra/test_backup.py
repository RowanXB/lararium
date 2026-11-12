"""infra/backup/backup.sh keeps dumps private: owner-only directory and files (R20)."""

import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

BACKUP_SCRIPT = Path(__file__).parents[3] / "infra" / "backup" / "backup.sh"

# Stands in for `docker compose ... exec db pg_dump`, which writes the dump to stdout.
FAKE_DOCKER = "#!/bin/sh\necho fake-dump\n"


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


@pytest.mark.parametrize("existing_dir", [False, True])
def test_backups_are_owner_only(tmp_path: Path, existing_dir: bool) -> None:
    infra = tmp_path / "infra"
    (infra / "backup").mkdir(parents=True)
    shutil.copy(BACKUP_SCRIPT, infra / "backup" / "backup.sh")
    (infra / ".env").write_text("BACKUP_DIR=backups\n")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "docker").write_text(FAKE_DOCKER)
    (bin_dir / "docker").chmod(0o755)
    backups = tmp_path / "backups"
    if existing_dir:
        backups.mkdir(mode=0o755)

    env = {k: v for k, v in os.environ.items() if not k.startswith("RESTIC_")}
    subprocess.run(
        ["bash", str(infra / "backup" / "backup.sh")],
        env=env | {"PATH": f"{bin_dir}:{env['PATH']}"},
        check=True,
        capture_output=True,
        preexec_fn=lambda: os.umask(0o022),  # a typical permissive login umask
    )

    [dump] = backups.iterdir()
    assert dump.read_text() == "fake-dump\n"
    assert (_mode(backups), _mode(dump)) == (0o700, 0o600)
