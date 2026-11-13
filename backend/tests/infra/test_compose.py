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
            "--profile",
            "*",
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
        "worker": "unless-stopped",
        "web": "unless-stopped",
        "webhook-in": "unless-stopped",
        "cloudflared": "unless-stopped",
    }


def test_worker_runs_the_worker_module_after_the_migrations() -> None:
    worker = _services()["worker"]
    assert worker["command"] == ["python", "-m", "lararium.worker"]
    assert worker["depends_on"]["migrate"]["condition"] == "service_completed_successfully"
    assert "profiles" not in worker
    assert worker["environment"]["LARARIUM_DATABASE_URL"].startswith(
        "postgresql+psycopg://lararium_backend:"
    )


def test_api_does_not_log_requests() -> None:
    # The OAuth callback URL carries ?code= and &state=.
    assert "--no-access-log" in _services()["api"]["command"]


@pytest.mark.parametrize("name", ["api", "worker"])
def test_api_and_worker_get_the_connector_settings(name: str) -> None:
    environment = _services()[name]["environment"]
    for variable in (
        "LARARIUM_MASTER_KEY",
        "LARARIUM_PUBLIC_BASE_URL",
        "LARARIUM_WEBHOOK_BASE_URL",
        "LARARIUM_WEBHOOK_SECRET",
    ):
        assert variable in environment, variable
    if name == "worker":
        assert "LARARIUM_FEISHU_WEBHOOK_URL" in environment
        assert "LARARIUM_FEISHU_SECRET" in environment


@pytest.mark.parametrize("name", ["webhook-in", "cloudflared"])
def test_ingress_services_are_only_in_the_webhook_profile(name: str) -> None:
    assert _services()[name]["profiles"] == ["webhook"]


def test_webhook_receiver_runs_without_access_logs() -> None:
    # `python -m lararium.webhook_in` turns uvicorn's access log off: the secret is in the path.
    assert _services()["webhook-in"]["command"] == ["python", "-m", "lararium.webhook_in"]


def test_webhook_receiver_connects_as_its_own_role() -> None:
    environment = _services()["webhook-in"]["environment"]
    assert environment["LARARIUM_DATABASE_URL"].startswith(
        "postgresql+psycopg://lararium_webhook_in:"
    )
    assert "LARARIUM_WEBHOOK_SECRET" in environment
    assert "LARARIUM_MASTER_KEY" not in environment


def test_cloudflared_runs_a_pinned_tunnel() -> None:
    cloudflared = _services()["cloudflared"]
    assert cloudflared["command"] == ["tunnel", "--no-autoupdate", "run"]
    assert "TUNNEL_TOKEN" in cloudflared["environment"]
    image = cloudflared["image"]
    assert image.startswith("cloudflare/cloudflared:")
    assert image.split(":", 1)[1] not in ("", "latest")


def test_only_web_publishes_a_port() -> None:
    published = {name for name, service in _services().items() if service.get("ports")}
    assert published == {"web"}


REQUIRED_SETTINGS = {
    "POSTGRES_PASSWORD": "pg",
    "LARARIUM_OWNER_PASSWORD": "owner",
    "LARARIUM_BACKEND_PASSWORD": "backend",
    "LARARIUM_WEBHOOK_IN_PASSWORD": "webhook_in",
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


def test_webhook_settings_are_optional_without_the_profile() -> None:
    # Interpolation covers services outside the active profile: requiring their settings
    # would stop a deployment that doesn't use webhooks.
    environment = _services()["webhook-in"]["environment"]
    assert environment["LARARIUM_WEBHOOK_SECRET"] == "${LARARIUM_WEBHOOK_SECRET:-}"
    assert _resolve(REQUIRED_SETTINGS).returncode == 0


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
