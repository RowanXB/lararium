"""infra/compose.yaml, as `docker compose` resolves it."""

import json
import subprocess
from pathlib import Path

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
