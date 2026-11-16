"""Throwaway PostgreSQL containers for the tests, on a local or a remote Docker host.

On a remote host, chosen by `DOCKER_HOST=ssh://[user@]host[:port]` or the current docker
context, the containers run on that host: the remote daemon's socket is forwarded to a local
unix socket over ssh (docker-py's own ssh transport needs paramiko and its exec streams can
stall), and the database port, published on the remote host's loopback only, is forwarded to
a local port. Files go into the container by copy, not a bind mount, since a remote daemon
can't see this machine's paths. Otherwise the local Docker is used as before, with the port
on loopback; a daemon reached at any other address is refused.

On the remote host nothing reaps containers if the test process dies without unwinding
(SIGKILL, OOM, a dropped link). List and remove what is left with
`docker --context <host> ps -a --filter label=org.testcontainers=true`.
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
from testcontainers.core.docker_client import get_docker_host

PORT = 5432
REMOTE_DOCKER_SOCKET = "/var/run/docker.sock"
LOOPBACK = frozenset({"localhost", "127.0.0.1", "::1"})
TUNNEL_TIMEOUT_SECONDS = 20


@dataclass(frozen=True)
class Endpoint:
    host: str
    port: int


def _ssh_destination() -> list[str] | None:
    """The ssh arguments naming the Docker host, when the daemon testcontainers uses is ssh://."""
    if "DOCKER_CONTEXT" in os.environ and "DOCKER_HOST" not in os.environ:
        raise RuntimeError(
            "DOCKER_CONTEXT is ignored here: testcontainers reads the docker context only from "
            "config.json and writes that back into DOCKER_HOST. Use DOCKER_HOST=ssh://<host> "
            "or `docker context use <context>` instead."
        )
    docker_host = get_docker_host() or ""
    if not docker_host.startswith("ssh://"):
        return None
    url = urllib.parse.urlparse(docker_host)
    if not url.hostname:
        raise RuntimeError(f"DOCKER_HOST has no host name: {docker_host}")
    destination = f"{url.username}@{url.hostname}" if url.username else url.hostname
    return [*(["-p", str(url.port)] if url.port else []), destination]


def _tunnel(ssh: list[str], forward: str, ready: Callable[[], bool]) -> Iterator[None]:
    # Own the connection (no ControlMaster) and miss the terminal's Ctrl-C, so teardown can run.
    process = subprocess.Popen(
        [
            "ssh",
            "-N",
            "-o",
            "ControlMaster=no",
            "-o",
            "ControlPath=none",
            "-o",
            "ExitOnForwardFailure=yes",
            "-L",
            forward,
            *ssh,
        ],
        stdin=subprocess.DEVNULL,
        start_new_session=True,
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


def _docker_answers(path: Path) -> bool:
    """Whether the Docker daemon answers a ping through the unix socket at `path`."""
    try:
        with socket.socket(socket.AF_UNIX) as sock:
            sock.settimeout(1)
            sock.connect(str(path))
            sock.sendall(b"GET /_ping HTTP/1.0\r\nHost: docker\r\n\r\n")
            reply = sock.recv(1024)
    except OSError:
        return False
    return reply.split(b"\r\n", 1)[0].split()[1:2] == [b"200"]


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
        contextmanager(_tunnel)(
            ssh, f"{local_socket}:{REMOTE_DOCKER_SOCKET}", lambda: _docker_answers(local_socket)
        )
    )
    os.environ["DOCKER_HOST"] = f"unix://{local_socket}"
    if (docker_host := get_docker_host()) != os.environ["DOCKER_HOST"]:
        stack.close()
        raise RuntimeError(
            f"testcontainers would still use {docker_host}, not the forwarded socket: a source "
            "ranked above DOCKER_HOST (tc.host in ~/.testcontainers.properties) is set. "
            "Remove it to run on an ssh Docker host."
        )
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
            host = container.get_container_host_ip()
            if host not in LOOPBACK:
                raise RuntimeError(
                    f"The Docker host is reached at {host}, but the database port is published "
                    "on its loopback only. For a remote Docker host use DOCKER_HOST=ssh://<host>."
                )
            yield container, Endpoint(host, published)
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
