"""infra/caddy/Caddyfile: the headers every response carries."""

from pathlib import Path

CADDYFILE = (Path(__file__).parents[3] / "infra" / "caddy" / "Caddyfile").read_text()


def test_pages_cannot_be_framed() -> None:
    # A framed page is same-origin to the API, so a click lured onto it passes the
    # cross-site check; refusing to be framed closes that.
    assert """Content-Security-Policy "frame-ancestors 'none'\"""" in CADDYFILE
    assert 'X-Frame-Options "DENY"' in CADDYFILE


def test_ibkr_statement_upload_has_its_own_body_cap() -> None:
    # A year of Client Portal export is about 10 MB, over the 6 MB every other api route
    # gets; the route allows 64 MiB (MAX_IBKR_UPLOAD_BYTES) plus multipart framing.
    route = CADDYFILE.split("handle /api/v1/connections/*/ibkr-statement {", 1)[1]
    assert route.split("}", 2)[0].split("max_size", 1)[1].strip() == "65MiB"
    assert CADDYFILE.index("ibkr-statement {") < CADDYFILE.index("handle /api/* {")
