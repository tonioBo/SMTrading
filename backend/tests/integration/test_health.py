async def test_health_ok(app_client):
    r = await app_client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["db"] is True and body["redis"] is True
    assert body["status"] in ("ok", "degraded")  # degraded: nessun heartbeat del worker nel test
