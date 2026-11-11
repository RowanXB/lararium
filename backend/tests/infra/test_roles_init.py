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
        .with_env("FG_OWNER_PASSWORD", OWNER_PW)
        .with_env("FG_BACKEND_PASSWORD", BACKEND_PW)
        .with_volume_mapping(str(INFRA_POSTGRES), "/fg", mode="ro")
        .with_volume_mapping(str(INFRA_POSTGRES / "init"), "/docker-entrypoint-initdb.d", mode="ro")
    )
    with container:
        yield container


@pytest.mark.parametrize(("user", "password"), [("fg_owner", OWNER_PW), ("fg_backend", BACKEND_PW)])
def test_roles_log_in_to_financial_gate(
    initialised: PostgresContainer, user: str, password: str
) -> None:
    with psycopg.connect(
        host=initialised.get_container_host_ip(),
        port=int(initialised.get_exposed_port(5432)),
        user=user,
        password=password,
        dbname="financial_gate",
    ) as conn:
        row = conn.execute("select current_user, current_database()").fetchone()
    assert row == (user, "financial_gate")
