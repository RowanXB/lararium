async def test_health(client) -> None:
    response = await client.get("/api/v1/health")
    assert response.json() == {"status": "ok"}


async def test_tailscale_mode_requires_owner_header(tailscale_client) -> None:
    assert (await tailscale_client.get("/api/v1/finance/accounts")).status_code == 401
    ok = await tailscale_client.get(
        "/api/v1/finance/accounts", headers={"Tailscale-User-Login": "me@example.com"}
    )
    assert ok.status_code == 200


async def test_writes_record_actor_in_audit_log(tailscale_client, session) -> None:
    from sqlalchemy import text

    await tailscale_client.post(
        "/api/v1/finance/accounts",
        headers={"Tailscale-User-Login": "me@example.com"},
        json={
            "name": "Manual bank",
            "ledger_type": "asset",
            "subtype": "bank",
            "balance_mode": "hybrid",
        },
    )
    row = (
        await session.execute(text("select actor_type, actor_id, command from core.audit_log"))
    ).one()
    assert tuple(row) == ("user", "me@example.com", "create_account")


async def test_health_needs_no_identity(tailscale_client) -> None:
    assert (await tailscale_client.get("/api/v1/health")).status_code == 200


async def test_tailscale_mode_rejects_other_logins(tailscale_client) -> None:
    response = await tailscale_client.get(
        "/api/v1/settings", headers={"Tailscale-User-Login": "someone@example.com"}
    )
    assert response.status_code == 401


async def test_timezone_setting_round_trip(client) -> None:
    assert (await client.get("/api/v1/settings")).json() == {"timezone": "Europe/London"}
    bad = await client.put("/api/v1/settings", json={"timezone": "Mars/Olympus_Mons"})
    assert bad.status_code == 422
    ok = await client.put("/api/v1/settings", json={"timezone": "America/New_York"})
    assert ok.json() == {"timezone": "America/New_York"}
    assert (await client.get("/api/v1/settings")).json() == {"timezone": "America/New_York"}
