"""infra/caddy/Caddyfile: the headers every response carries."""

from pathlib import Path

CADDYFILE = (Path(__file__).parents[3] / "infra" / "caddy" / "Caddyfile").read_text()


def test_pages_cannot_be_framed() -> None:
    # A framed page is same-origin to the API, so a click lured onto it passes the
    # cross-site check; refusing to be framed closes that.
    assert """Content-Security-Policy "frame-ancestors 'none'\"""" in CADDYFILE
    assert 'X-Frame-Options "DENY"' in CADDYFILE
