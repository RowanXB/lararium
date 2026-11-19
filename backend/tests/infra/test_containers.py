"""How tests/containers.py picks the Docker host and refuses setups it can't run safely.

No containers: the daemon is resolved from a temporary Docker config, and the container and
the ssh tunnel are stand-ins.
"""

import json
import socket
import tempfile
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any, cast

import pytest
from docker.context.config import get_context_id
from testcontainers.community.postgres import PostgresContainer
from testcontainers.core.config import testcontainers_config

from .. import containers


@pytest.fixture
def docker_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """An empty Docker config and no Docker environment: the shape of the CI runner."""
    for name in ("DOCKER_HOST", "DOCKER_CONTEXT", "TESTCONTAINERS_HOST_OVERRIDE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("DOCKER_CONFIG", str(tmp_path))
    (tmp_path / "config.json").write_text("{}")  # without it, docker-py reads ~/.docker
    monkeypatch.setattr(testcontainers_config, "tc_properties", {})
    return tmp_path


def _use_context(config: Path, name: str, host: str) -> None:
    meta = config / "contexts" / "meta" / get_context_id(name)
    meta.mkdir(parents=True)
    endpoint = {"Host": host, "SkipTLSVerify": False}
    (meta / "meta.json").write_text(
        json.dumps({"Name": name, "Metadata": {}, "Endpoints": {"docker": endpoint}})
    )
    (config / "config.json").write_text(json.dumps({"currentContext": name}))


def test_an_ssh_docker_host_is_forwarded(
    docker_config: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DOCKER_HOST", "ssh://me@docker-test:2222")
    assert containers._ssh_destination() == ["-p", "2222", "me@docker-test"]


def test_an_ssh_docker_context_is_forwarded(docker_config: Path) -> None:
    _use_context(docker_config, "docker-test", "ssh://docker-test")
    assert containers._ssh_destination() == ["docker-test"]


def test_a_unix_docker_host_is_local(docker_config: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DOCKER_HOST", "unix:///var/run/docker.sock")
    assert containers._ssh_destination() is None


def test_no_docker_configuration_is_local(docker_config: Path) -> None:
    assert containers._ssh_destination() is None


def test_docker_context_without_docker_host_is_refused(
    docker_config: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DOCKER_CONTEXT", "docker-test")
    with pytest.raises(RuntimeError, match="DOCKER_CONTEXT"):
        containers._ssh_destination()


def test_a_daemon_source_above_docker_host_is_refused_in_ssh_mode(
    docker_config: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def no_tunnel(*_: object) -> Iterator[None]:
        yield

    monkeypatch.setenv("DOCKER_HOST", "ssh://docker-test")
    monkeypatch.setattr(testcontainers_config, "tc_properties", {"tc.host": "ssh://elsewhere"})
    monkeypatch.setattr(containers, "_tunnel", no_tunnel)
    monkeypatch.setattr(containers, "_docker_forward", None)
    with pytest.raises(RuntimeError, match=r"tc\.host"):
        containers._forward_docker_socket(["docker-test"])
    assert containers._docker_forward is None


class _FakeContainer:
    def __init__(self, host: str) -> None:
        self.ports: dict[str, Any] = {}
        self.host = host
        self.stopped = False

    def __enter__(self) -> "_FakeContainer":
        return self

    def __exit__(self, *_: object) -> None:
        self.stopped = True

    def get_exposed_port(self, port: int) -> str:
        return "49153"

    def get_container_host_ip(self) -> str:
        return self.host


def test_a_non_loopback_docker_host_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(containers, "SSH", None)
    fake = _FakeContainer("10.0.0.5")
    with (
        pytest.raises(RuntimeError, match="ssh://"),
        containers.running(lambda: cast(PostgresContainer, fake)),
    ):
        pass
    assert fake.stopped


def test_a_loopback_docker_host_is_used(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(containers, "SSH", None)
    fake = _FakeContainer("localhost")
    with containers.running(lambda: cast(PostgresContainer, fake)) as (_, endpoint):
        assert endpoint == containers.Endpoint("localhost", 49153)


@pytest.fixture
def unix_server() -> Iterator[tuple[Path, list[bytes]]]:
    """A unix socket that answers each connection with the next queued reply."""
    replies: list[bytes] = []
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "s.sock"
        server = socket.socket(socket.AF_UNIX)
        server.bind(str(path))
        server.listen()

        def serve() -> None:
            while True:
                try:
                    conn, _ = server.accept()
                except OSError:
                    return
                with conn:
                    conn.recv(1024)
                    conn.sendall(replies.pop(0))

        threading.Thread(target=serve, daemon=True).start()
        yield path, replies
        server.close()


def test_the_socket_forward_is_ready_only_when_docker_answers(
    unix_server: tuple[Path, list[bytes]],
) -> None:
    path, replies = unix_server
    replies += [b"HTTP/1.1 200 OK\r\n\r\nOK", b"HTTP/1.1 500 Internal Server Error\r\n\r\n", b""]
    assert containers._docker_answers(path)
    assert not containers._docker_answers(path)
    assert not containers._docker_answers(path)
    assert not containers._docker_answers(path.with_name("missing.sock"))
