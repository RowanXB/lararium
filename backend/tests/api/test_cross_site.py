"""Writes from another site are refused: the browser attaches the owner's identity to them."""

import pytest

OWNER = {"Tailscale-User-Login": "me@example.com"}
TIMEZONE = {"timezone": "Europe/London"}

CROSS_SITE = [
    {"Sec-Fetch-Site": "cross-site"},
    {"Sec-Fetch-Site": "same-site"},
    {"Origin": "https://evil.example"},
    {"Origin": "http://test:8080"},
    {"Origin": "null"},
    {"Origin": "http://test", "Sec-Fetch-Site": "cross-site"},
]
SAME_ORIGIN = [
    {},  # curl, scripts and tests send neither header
    {"Sec-Fetch-Site": "same-origin", "Origin": "http://test"},
    {"Sec-Fetch-Site": "none"},
    {"Origin": "http://TEST"},
]


@pytest.mark.parametrize("headers", CROSS_SITE)
async def test_cross_site_write_rejected(client, headers) -> None:
    response = await client.put("/api/v1/settings", json=TIMEZONE, headers=headers)
    assert response.status_code == 403


@pytest.mark.parametrize("headers", CROSS_SITE)
async def test_cross_site_write_rejected_in_tailscale_mode(tailscale_client, headers) -> None:
    response = await tailscale_client.post(
        "/api/v1/finance/accounts",
        json={"name": "X", "ledger_type": "asset", "subtype": "bank", "balance_mode": "snapshots"},
        headers=OWNER | headers,
    )
    assert response.status_code == 403


@pytest.mark.parametrize("headers", SAME_ORIGIN)
async def test_same_origin_write_allowed(tailscale_client, headers) -> None:
    response = await tailscale_client.put(
        "/api/v1/settings", json=TIMEZONE, headers=OWNER | headers
    )
    assert response.status_code == 200


async def test_cross_site_read_unaffected(tailscale_client) -> None:
    response = await tailscale_client.get(
        "/api/v1/settings",
        headers=OWNER | {"Sec-Fetch-Site": "cross-site", "Origin": "https://evil.example"},
    )
    assert response.status_code == 200
