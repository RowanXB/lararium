"""infra/backup/restore.sh: into the compose stack's db, or the container the restore
rehearsal names."""

import os
import shutil
import subprocess
from pathlib import Path

INFRA = Path(__file__).parents[3] / "infra"

# Records each call's arguments and what it read on stdin, one line per call.
FAKE_DOCKER = """#!/bin/sh
printf '%s | %s\\n' "$*" "$(cat)" >> "$FAKE_DOCKER_LOG"
"""


def _restore(tmp_path: Path, **env: str) -> list[str]:
    infra = tmp_path / "infra"
    (infra / "backup").mkdir(parents=True)
    shutil.copy(INFRA / "backup" / "restore.sh", infra / "backup" / "restore.sh")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "docker").write_text(FAKE_DOCKER)
    (bin_dir / "docker").chmod(0o755)
    dump = tmp_path / "x.dump"
    dump.write_text("fake-dump")
    log = tmp_path / "docker.log"
    clean = {k: v for k, v in os.environ.items() if k != "LARARIUM_DB_CONTAINER"}
    subprocess.run(
        ["bash", str(infra / "backup" / "restore.sh"), str(dump), "lararium_restore"],
        env=clean | env | {"PATH": f"{bin_dir}:{os.environ['PATH']}", "FAKE_DOCKER_LOG": str(log)},
        check=True,
        capture_output=True,
        stdin=subprocess.DEVNULL,
    )
    return log.read_text().splitlines()


def test_restores_into_the_compose_db(tmp_path: Path) -> None:
    infra = tmp_path / "infra"
    compose = f"compose -f {infra}/compose.yaml --env-file {infra}/.env exec -T db"
    assert _restore(tmp_path) == [
        f"{compose} psql -U postgres -d postgres -v ON_ERROR_STOP=1"
        " -c CREATE DATABASE lararium_restore OWNER lararium_owner | ",
        f"{compose} pg_restore -U postgres -d lararium_restore"
        " --no-owner --role=lararium_owner --exit-on-error | fake-dump",
    ]


def test_restores_into_a_named_container(tmp_path: Path) -> None:
    assert _restore(tmp_path, LARARIUM_DB_CONTAINER="rehearsal-db") == [
        "exec -i rehearsal-db psql -U postgres -d postgres -v ON_ERROR_STOP=1"
        " -c CREATE DATABASE lararium_restore OWNER lararium_owner | ",
        "exec -i rehearsal-db pg_restore -U postgres -d lararium_restore"
        " --no-owner --role=lararium_owner --exit-on-error | fake-dump",
    ]
