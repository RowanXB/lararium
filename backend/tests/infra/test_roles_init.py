"""infra/postgres/init/00-roles.sh, as the postgres image runs it on first start."""

import re
from collections.abc import Iterator
from pathlib import Path

import psycopg
import pytest
from testcontainers.community.postgres import PostgresContainer

INFRA_POSTGRES = Path(__file__).parents[3] / "infra" / "postgres"
INIT_SCRIPT = INFRA_POSTGRES / "init" / "00-roles.sh"

# Quotes, a backslash and a space: anything that would break naive quoting.
OWNER_PW = "o'w\\ner pw"
BACKEND_PW = "b\"a:ck'end"
WEBHOOK_IN_PW = "w$e b\\hook'in"


REPO = Path(__file__).parents[3]


@pytest.mark.parametrize("readme", ["README.md", "README.zh-CN.md"])
def test_the_dev_database_command_passes_every_variable_the_script_requires(readme: str) -> None:
    required = re.findall(r'"\$\{(\w+):\?', INIT_SCRIPT.read_text())
    assert required
    text = (REPO / readme).read_text()
    command = text[text.index("docker run -d --name lararium-dev-db") :].split("postgres:18")[0]
    for name in required:
        assert f"-e {name}=" in command, f"{readme}: {name}"


def test_passwords_are_not_on_the_psql_command_line() -> None:
    lines = INIT_SCRIPT.read_text().splitlines()
    psql_lines = [line for line in lines if re.match(r"\s*psql", line)]
    assert psql_lines
    for line in psql_lines:
        assert "PASSWORD" not in line and "_pw" not in line, line


@pytest.fixture(scope="module")
def initialised() -> Iterator[PostgresContainer]:
    container = (
        PostgresContainer("postgres:18")
        .with_env("LARARIUM_OWNER_PASSWORD", OWNER_PW)
        .with_env("LARARIUM_BACKEND_PASSWORD", BACKEND_PW)
        .with_env("LARARIUM_WEBHOOK_IN_PASSWORD", WEBHOOK_IN_PW)
        .with_volume_mapping(str(INFRA_POSTGRES), "/lararium", mode="ro")
        .with_volume_mapping(str(INFRA_POSTGRES / "init"), "/docker-entrypoint-initdb.d", mode="ro")
    )
    with container:
        yield container


@pytest.mark.parametrize(
    ("user", "password"),
    [
        ("lararium_owner", OWNER_PW),
        ("lararium_backend", BACKEND_PW),
        ("lararium_webhook_in", WEBHOOK_IN_PW),
    ],
)
def test_roles_log_in_to_lararium(initialised: PostgresContainer, user: str, password: str) -> None:
    with psycopg.connect(
        host=initialised.get_container_host_ip(),
        port=int(initialised.get_exposed_port(5432)),
        user=user,
        password=password,
        dbname="lararium",
    ) as conn:
        row = conn.execute("select current_user, current_database()").fetchone()
    assert row == (user, "lararium")
