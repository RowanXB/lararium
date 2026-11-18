"""backend/Dockerfile: the image the api and worker run from."""

from pathlib import Path

DOCKERFILE = Path(__file__).parents[2] / "Dockerfile"


def test_documents_directory_is_owner_only() -> None:
    # A new named volume copies this directory's ownership and mode on first mount.
    dockerfile = DOCKERFILE.read_text()
    assert (
        "mkdir -p /var/lib/lararium/documents"
        " && chown lararium /var/lib/lararium/documents"
        " && chmod 700 /var/lib/lararium/documents"
    ) in dockerfile
