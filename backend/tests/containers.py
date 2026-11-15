"""Throwaway PostgreSQL containers for the tests, on a local or a remote Docker host.

With `DOCKER_HOST=ssh://[user@]host[:port]`, the containers run on that host: the remote
daemon's socket is forwarded to a local unix socket over ssh (docker-py's own ssh transport
needs paramiko and its exec streams can stall), and the database port, published on the
remote host's loopback only, is forwarded to a local port. Files go into the container by
copy, not a bind mount, since a remote daemon can't see this machine's paths. Without an
ssh DOCKER_HOST, the local Docker is used as before, with the port on loopback.
"""

import atexit
import os
import socket
import subprocess
import tempfile
import time
import urllib.parse
from collections.abc import Callable, Iterator
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from pathlib import Path

from testcontainers.community.postgres import PostgresContainer
from testcontainers.core.config import testcontainers_config

PORT = 5432
REMOTE_DOCKER_SOCKET = "/var/run/docker.sock"
TUNNEL_TIMEOUT_SECONDS = 20


@dataclass(frozen=True)
class Endpoint:
    host: str
    port: int


def _ssh_destination() -> list[str] | None:
    """The ssh arguments naming the Docker host, when DOCKER_HOST is ssh://."""
    docker_host = os.environ.get("DOCKER_HOST", "")
    if not docker_host.startswith("ssh://"):
        return None
    url = urllib.parse.urlparse(docker_host)
    if not url.hostname:
        raise RuntimeError(f"DOCKER_HOST has no host name: {docker_host}")
    destination = f"{url.username}@{url.hostname}" if url.username else url.hostname
    return [*(["-p", str(url.port)] if url.port else []), destination]


def _tunnel(ssh: list[str], forward: str, ready: Callable[[], bool]) -> Iterator[None]:
    process = subprocess.Popen(
        ["ssh", "-N", "-o", "ExitOnForwardFailure=yes", "-L", forward, *ssh],
        stdin=subprocess.DEVNULL,
    )
    try:
        deadline = time.monotonic() + TUNNEL_TIMEOUT_SECONDS
        while not ready():
            if process.poll() is not None:
                raise RuntimeError(f"ssh -L {forward} exited ({process.returncode})")
            if time.monotonic() > deadline:
                raise RuntimeError(f"ssh -L {forward} did not open in time")
            time.sleep(0.2)
        yield
    finally:
        process.terminate()
        process.wait(timeout=10)


def _accepts(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1):
            return True
    except OSError:
        return False


def _free_local_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


# Resolved once, before anything here points DOCKER_HOST at the local socket forward.
SSH = _ssh_destination()
_docker_forward: ExitStack | None = None


def _forward_docker_socket(ssh: list[str]) -> None:
    """Forward the remote daemon's socket to a local one, once for the test process, and point
    DOCKER_HOST at it. It stays open until the process exits."""
    global _docker_forward
    if _docker_forward is not None:
        return
    stack = ExitStack()
    directory = stack.enter_context(tempfile.TemporaryDirectory(prefix="lararium-docker-"))
    local_socket = Path(directory) / "docker.sock"
    stack.enter_context(
        contextmanager(_tunnel)(ssh, f"{local_socket}:{REMOTE_DOCKER_SOCKET}", local_socket.exists)
    )
    os.environ["DOCKER_HOST"] = f"unix://{local_socket}"
    # Ryuk, the reaper, would need a port reachable from here too; `running` stops each
    # container instead.
    testcontainers_config.ryuk_disabled = True
    atexit.register(stack.close)
    _docker_forward = stack


@contextmanager
def running(build: Callable[[], PostgresContainer]) -> Iterator[tuple[PostgresContainer, Endpoint]]:
    """Build and start the container, publish its port on the Docker host's loopback, and
    yield it with the address this machine reaches it at."""
    ssh = SSH
    if ssh is not None:
        _forward_docker_socket(ssh)
    with ExitStack() as stack:
        container = build()  # after DOCKER_HOST is set: the client is made here
        # docker-py takes (host_ip, host_port); None picks a free port on the Docker host.
        container.ports[f"{PORT}/tcp"] = ("127.0.0.1", None)  # type: ignore[assignment]
        stack.enter_context(container)
        published = int(container.get_exposed_port(PORT))
        if ssh is None:
            yield container, Endpoint(container.get_container_host_ip(), published)
            return
        local_port = _free_local_port()
        stack.enter_context(
            contextmanager(_tunnel)(
                ssh,
                f"127.0.0.1:{local_port}:127.0.0.1:{published}",
                lambda: _accepts(local_port),
            )
        )
        yield container, Endpoint("127.0.0.1", local_port)
