"""infra/compose.yaml, as `docker compose` resolves it."""

import json
import os
import subprocess
from pathlib import Path

import pytest

COMPOSE_FILE = Path(__file__).parents[3] / "infra" / "compose.yaml"


def _services() -> dict[str, dict[str, object]]:
    result = subprocess.run(
        [
            "docker",
            "compose",
            "-f",
            str(COMPOSE_FILE),
            "config",
            "--no-interpolate",
            "--format",
            "json",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(result.stdout)["services"]


def test_long_running_services_restart_unless_stopped() -> None:
    restart = {name: service.get("restart") for name, service in _services().items()}
    # migrate is a one-shot job; restarting it would rerun the migrations in a loop.
    assert restart == {
        "db": "unless-stopped",
        "migrate": None,
        "api": "unless-stopped",
        "web": "unless-stopped",
    }


REQUIRED_SETTINGS = {
    "POSTGRES_PASSWORD": "pg",
    "LARARIUM_OWNER_PASSWORD": "owner",
    "LARARIUM_BACKEND_PASSWORD": "backend",
    "LARARIUM_OWNER_LOGIN": "me@example.com",
    "LARARIUM_MASTER_KEY": "AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8=",
}


def _resolve(env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    # --env-file /dev/null: only the variables given here, never a local infra/.env.
    return subprocess.run(
        ["docker", "compose", "-f", str(COMPOSE_FILE), "--env-file", "/dev/null", "config"],
        capture_output=True,
        text=True,
        env={"PATH": os.environ["PATH"], "HOME": os.environ.get("HOME", "/")} | env,
    )


def test_resolves_with_every_required_setting() -> None:
    result = _resolve(REQUIRED_SETTINGS)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("name", REQUIRED_SETTINGS)
@pytest.mark.parametrize("value", [None, ""])
def test_refuses_to_start_without_a_required_setting(name: str, value: str | None) -> None:
    # The db init scripts only run on an empty volume, after initdb has made it permanent:
    # a setting they find missing would leave a volume they can never finish. So compose
    # itself refuses, before any container starts.
    env = {k: v for k, v in REQUIRED_SETTINGS.items() if k != name}
    if value is not None:
        env[name] = value
    result = _resolve(env)
    assert result.returncode != 0
    assert name in result.stderr
