"""Paperclip reader (worker job): GET-only client, cost snapshots per day and agent, the
isolation policy baseline and its diff alert, the 3-errors alert, the board key expiry
notice, and nothing written to the agents' activity feed. Paperclip is mocked (respx)."""

import asyncio
import logging
import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import httpx
import pytest
import respx
from sqlalchemy import delete, func, make_url, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from conftest import run_migrations
from smtrading.activity.models import Activity
from smtrading.common.config import get_settings
from smtrading.common.db import make_engine
from smtrading.paperclip.client import GetOnlyTransport, PaperclipClient
from smtrading.paperclip.config import load_config, load_state, save_config
from smtrading.paperclip.models import AgentCostDay
from smtrading.paperclip.reader import PaperclipReader
from smtrading.risk.models import AppSetting
from smtrading.telegram.models import TelegramOutbox

BASE = "https://paperclip.example.test"
CID = "comp-1"
KEY = "pcp_board_CANARY0001"
API = f"{BASE}/api/companies/{CID}"
NOW = datetime(2026, 11, 15, 12, 0, tzinfo=UTC)

TRADER_ID = "0b6f3c8e-1d2a-4f5b-9c7d-111111111111"
ANALYST_ID = "0b6f3c8e-1d2a-4f5b-9c7d-222222222222"


def _policy(pid, name, agent_id, connections):
    """A policy as Paperclip returns it (real shape, 2026-11-15)."""
    return {"id": pid, "companyId": CID, "name": name, "description": None,
            "policyType": "block", "priority": 10, "enabled": True,
            "selectors": {"agentId": agent_id, "connectionIds": connections},
            "conditions": None, "config": None, "createdByAgentId": None,
            "createdByUserId": "u1", "createdAt": "2026-11-01T10:00:00.000Z",
            "updatedAt": "2026-11-01T10:00:00.000Z"}


POLICY_A = _policy("p1", "Isolate Trader SMTrading", TRADER_ID, ["c2", "c1"])
POLICY_B = _policy("p2", "Isolate Analyst SMTrading", ANALYST_ID, ["c3"])
OTHER = {**_policy("p9", "Unrelated policy", TRADER_ID, []), "policyType": "allow"}
# the name must end with "SMTrading" (and have a space before it)
NEAR_MISS = _policy("p8", "Isolate Trader SMTrading (old)", TRADER_ID, ["c1"])


async def _admin_sql(pg_url: str, sql: str) -> None:
    engine = create_async_engine(make_url(pg_url).set(database="postgres"),
                                 isolation_level="AUTOCOMMIT")
    try:
        async with engine.connect() as conn:
            await conn.execute(text(sql))
    finally:
        await engine.dispose()


@pytest.fixture(scope="module")
def reader_db(pg_url):
    """One migrated database for the module (its tables are emptied before every test)."""
    name = f"t_pc_{uuid.uuid4().hex}"
    asyncio.run(_admin_sql(pg_url, f'CREATE DATABASE "{name}"'))
    url = make_url(pg_url).set(database=name).render_as_string(hide_password=False)
    run_migrations(url)
    yield url
    asyncio.run(_admin_sql(pg_url, f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))


@pytest.fixture
async def sm(reader_db, redis_url, monkeypatch):
    monkeypatch.setenv("SMT_DATABASE_URL", reader_db)
    monkeypatch.setenv("SMT_REDIS_URL", redis_url)
    monkeypatch.setenv("SMT_MASTER_KEY", "test-master-key")
    monkeypatch.setenv("SMT_ADMIN_EMAIL", "admin@example.com")
    monkeypatch.setenv("SMT_ADMIN_PASSWORD", "test-password")
    get_settings.cache_clear()
    engine = make_engine(reader_db)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as s:
        await s.execute(delete(AppSetting).where(
            AppSetting.key.in_(["paperclip", "paperclip_state"])))
        await s.execute(delete(AgentCostDay))
        await s.execute(text("delete from telegram_outbox"))
        await s.commit()
    yield maker
    await engine.dispose()


async def _configure(sm, enabled=True):
    async with sm() as s:
        await save_config(s, base_url=BASE, company_id=CID, board_key=KEY, enabled=enabled,
                          agent_map={}, by="test")
        await s.commit()


def _reader(sm, now=NOW):
    return PaperclipReader(sm, client_factory=lambda cfg: PaperclipClient(cfg),
                           clock=lambda: now)


def _runs():
    """Newest first, as Paperclip returns them."""
    return list(reversed([
        {"id": "r0", "agentId": "a1", "startedAt": "2026-11-13T09:00:00Z",
         "usageJson": {"costUsd": 5, "billingType": "metered_api"}},
        {"id": "r1", "agentId": "a1", "startedAt": "2026-11-14T09:00:00Z",
         "usageJson": {"costUsd": 1, "cacheAdjustedCostUsd": 0.5,
                       "billingType": "metered_api"}},
        {"id": "r2", "agentId": "a1", "startedAt": "2026-11-14T10:00:00Z",
         "usageJson": {"costUsd": 2, "billingType": "subscription_included"}},
        {"id": "r3", "agentId": "a2", "startedAt": "2026-11-15T10:00:00Z",
         "usageJson": {"costUsd": 0.25, "billingType": "credits"}},
    ]))


def _mock(router, *, policies=None, runs=None, agents=None, keys=None, cid=CID):
    api = f"{BASE}/api/companies/{cid}"
    router.get(f"{api}/heartbeat-runs").mock(
        return_value=httpx.Response(200, json=runs if runs is not None else _runs()))
    router.get(f"{api}/agents").mock(return_value=httpx.Response(
        200, json=agents if agents is not None else
        [{"id": "a1", "name": "Trader"}, {"id": "a2", "name": "Analyst"}]))
    pol = router.get(f"{api}/tools/policies").mock(return_value=httpx.Response(
        200, json={"policies": policies if policies is not None
                   else [POLICY_A, POLICY_B, OTHER, NEAR_MISS]}))
    router.get(f"{BASE}/api/cli-auth/me").mock(return_value=httpx.Response(
        200, json={"user": {"id": "u1", "name": "Admin", "email": "a@example.com"},
                   "userId": "u1", "isInstanceAdmin": True, "companyIds": [CID],
                   "memberships": [], "source": "board_key", "keyId": "k1"}))
    router.get(f"{BASE}/api/board-api-keys").mock(return_value=httpx.Response(
        200, json=keys if keys is not None else [_key("k1", None)]))
    return pol


def _key(kid, expires_at):
    return {"id": kid, "name": "smtrading", "createdAt": "2026-11-01T10:00:00.000Z",
            "lastUsedAt": "2026-11-15T10:00:00.000Z", "revokedAt": None,
            "expiresAt": expires_at}


def _respx():
    return respx.mock(assert_all_mocked=True, assert_all_called=False)


async def _alerts(sm, level: str | None = None) -> list[TelegramOutbox]:
    async with sm() as s:
        q = select(TelegramOutbox).order_by(TelegramOutbox.id)
        if level is not None:
            q = q.where(TelegramOutbox.level == level)
        return list(await s.scalars(q))


async def _state(sm) -> dict:
    async with sm() as s:
        return await load_state(s)


# -- client ---------------------------------------------------------------------------

async def test_get_only_transport_refuses_post():
    with _respx() as router:
        route = router.post(f"{API}/agents").mock(return_value=httpx.Response(200, json=[]))
        async with httpx.AsyncClient(transport=GetOnlyTransport()) as http:
            for method in ("POST", "PUT", "PATCH", "DELETE"):
                with pytest.raises(RuntimeError, match="Paperclip: solo lettura"):
                    await http.request(method, f"{API}/agents")
        assert route.call_count == 0


# -- isolation ------------------------------------------------------------------------

async def test_first_run_snapshots_baseline(sm):
    await _configure(sm)
    with _respx() as router:
        _mock(router)
        await _reader(sm).tick()
        assert all(c.request.method == "GET" for c in router.calls)
    st = await _state(sm)
    assert st["isolation"] == "ok"
    assert [p["id"] for p in st["baseline"]] == ["p1", "p2"]  # filtered on the name
    assert set(st["baseline"][0]) == {"id", "policyType", "selectors", "conditions",
                                      "config", "priority", "enabled", "name"}
    # list values inside the selectors are sorted (an order change is not a change)
    assert st["baseline"][0]["selectors"] == {"agentId": TRADER_ID, "connectionIds": ["c1", "c2"]}
    assert st["baseline_at"] and st["last_ok_at"] and st["last_error"] is None
    assert await _alerts(sm) == []
    reordered = _policy("p1", "Isolate Trader SMTrading", TRADER_ID, ["c1", "c2"])
    with _respx() as router:
        _mock(router, policies=[reordered, POLICY_B])
        await _reader(sm).tick()
    assert (await _state(sm))["isolation"] == "ok" and await _alerts(sm) == []


async def test_empty_policies_first_run_unknown_no_baseline(sm):
    await _configure(sm)
    with _respx() as router:
        _mock(router, policies=[])
        await _reader(sm).tick()
    st = await _state(sm)
    assert st["isolation"] == "unknown" and st["baseline"] is None
    assert st["last_error"] == ("nessuna regola di isolamento trovata: controlla l'id "
                                "azienda e i permessi della chiave")
    assert await _alerts(sm) == []


async def test_empty_policies_with_baseline_alerts_all_removed(sm):
    await _configure(sm)
    with _respx() as router:
        _mock(router)
        await _reader(sm).tick()
    with _respx() as router:
        _mock(router, policies=[])
        await _reader(sm).tick()
    st = await _state(sm)
    assert st["isolation"] == "changed"
    assert sorted(st["changes"]["removed"]) == ["p1", "p2"]
    (alert,) = await _alerts(sm)
    assert alert.category == "system" and alert.level == "critical"
    assert alert.text == ("🔴 Paperclip: le regole di isolamento di SMTrading sono cambiate "
                          "(2 rimosse, 0 modificate). Controlla Paperclip, poi premi "
                          "«Riconferma» nel portale.")


async def test_changed_policy_alerts_once_per_signature(sm):
    await _configure(sm)
    with _respx() as router:
        _mock(router)
        await _reader(sm).tick()
    disabled = {**POLICY_A, "enabled": False}
    for _ in range(3):
        with _respx() as router:
            _mock(router, policies=[disabled, POLICY_B])
            await _reader(sm).tick()
    alerts = await _alerts(sm)
    assert len(alerts) == 1 and alerts[0].category == "system"
    assert "(0 rimosse, 1 modificate)" in alerts[0].text
    st = await _state(sm)
    assert st["isolation"] == "changed" and st["changes"]["changed"] == ["p1"]
    # a different change is a new signature: one more alert
    with _respx() as router:
        _mock(router, policies=[disabled])
        await _reader(sm).tick()
    assert len(await _alerts(sm, "critical")) == 2
    # back to the baseline: ok, one info message (once), and the same change later
    # alerts again
    for _ in range(2):
        with _respx() as router:
            _mock(router)
            await _reader(sm).tick()
    assert (await _state(sm))["isolation"] == "ok"
    (info,) = await _alerts(sm, "info")
    assert info.category == "system"
    assert info.text == "🟢 Regole di isolamento di nuovo come da fotografia"
    with _respx() as router:
        _mock(router, policies=[disabled, POLICY_B])
        await _reader(sm).tick()
    assert len(await _alerts(sm, "critical")) == 3


async def test_added_policy_listed_no_alert(sm):
    await _configure(sm)
    with _respx() as router:
        _mock(router)
        await _reader(sm).tick()
    extra = _policy("p3", "Isolate New SMTrading", ANALYST_ID, [])
    with _respx() as router:
        _mock(router, policies=[POLICY_A, POLICY_B, extra])
        await _reader(sm).tick()
    st = await _state(sm)
    assert st["isolation"] == "ok" and st["changes"]["added"] == ["p3"]
    assert await _alerts(sm) == []


# -- errors ---------------------------------------------------------------------------

async def test_three_errors_alert_once_then_reset_on_success(sm):
    await _configure(sm)
    for _ in range(5):
        with _respx() as router:
            router.get(url__startswith=BASE).mock(return_value=httpx.Response(401))
            await _reader(sm).tick()
    st = await _state(sm)
    assert st["consecutive_errors"] == 5 and st["error_alerted"] is True
    assert st["last_error"] == "HTTP 401"
    (alert,) = await _alerts(sm)
    assert alert.text == "🔴 Paperclip non raggiungibile da 3 letture: HTTP 401"
    assert alert.category == "system" and alert.level == "critical"
    with _respx() as router:
        _mock(router)
        await _reader(sm).tick()
    st = await _state(sm)
    assert st["consecutive_errors"] == 0 and st["error_alerted"] is False
    alerts = await _alerts(sm)
    assert len(alerts) == 2  # the error alert, then the recovery message
    assert (alerts[1].text, alerts[1].category, alerts[1].level) == (
        "🟢 Paperclip di nuovo raggiungibile.", "system", "info")
    with _respx() as router:  # no second recovery message
        _mock(router)
        await _reader(sm).tick()
    assert len(await _alerts(sm)) == 2
    # unreachable host: a redacted text
    with _respx() as router:
        router.get(url__startswith=BASE).mock(side_effect=httpx.ConnectError("boom"))
        await _reader(sm).tick()
    assert (await _state(sm))["last_error"] == "non raggiungibile"


# -- costs ----------------------------------------------------------------------------

async def test_costs_upserted_per_day_agent(sm):
    await _configure(sm)
    for _ in range(2):  # the second read updates the same rows
        with _respx() as router:
            _mock(router, runs=_runs() + [{"id": f"x{i}", "agentId": "a2",
                                          "startedAt": "2026-11-12T08:00:00Z",
                                          "usageJson": {"costUsd": 1}}
                                         for i in range(996)])
            await _reader(sm).tick()
    async with sm() as s:
        rows = {(r.day, r.paperclip_agent_id): r for r in await s.scalars(select(AgentCostDay))}
    # 1000 runs (the limit): the oldest day (12th) is dropped as incomplete
    assert set(rows) == {(date(2026, 11, 13), "a1"), (date(2026, 11, 14), "a1"),
                         (date(2026, 11, 15), "a2")}
    a1 = rows[(date(2026, 11, 14), "a1")]
    assert (a1.api_usd, a1.subscription_usd, a1.other_usd, a1.runs) == (
        Decimal("0.5"), Decimal(2), Decimal(0), 2)
    assert a1.paperclip_agent_name == "Trader"
    assert rows[(date(2026, 11, 15), "a2")].other_usd == Decimal("0.25")


async def test_agent_names_fall_back_to_the_id(sm):
    await _configure(sm)
    with _respx() as router:
        _mock(router)
        router.get(f"{API}/agents").mock(return_value=httpx.Response(500))  # overrides
        await _reader(sm).tick()
    async with sm() as s:
        names = set(await s.scalars(select(AgentCostDay.paperclip_agent_name)))
    assert names == {"a1", "a2"}
    assert (await _state(sm))["consecutive_errors"] == 0


async def test_disabled_no_requests(sm):
    await _configure(sm, enabled=False)
    with _respx() as router:
        await _reader(sm).tick()
        assert router.calls.call_count == 0
    assert (await _state(sm))["isolation"] == "off"


# -- board key expiry -----------------------------------------------------------------

async def test_key_expiry_alerts_once_seven_days_before(sm):
    await _configure(sm)
    expires = "2026-11-20T00:00:00.000Z"
    for now in (NOW - timedelta(days=3), NOW, NOW + timedelta(hours=1)):
        with _respx() as router:
            _mock(router, keys=[_key("k0", None), _key("k1", expires)])
            await _reader(sm, now=now).tick()
    st = await _state(sm)
    assert st["key_expires_at"] == "2026-11-20T00:00:00+00:00"
    (alert,) = await _alerts(sm)  # 8 days before: nothing; then one notice
    assert alert.category == "system" and alert.level == "warning"
    assert alert.text == ("🟠 Paperclip: la chiave board scade il 20/11/2026. Creane una "
                          "nuova e aggiornala nel portale.")
    assert KEY not in alert.text


async def test_key_without_expiry_no_alert(sm):
    await _configure(sm)
    with _respx() as router:
        _mock(router)  # keyId k1 from cli-auth/me, expiresAt null in the list
        await _reader(sm).tick()
    assert (await _state(sm))["key_expires_at"] is None
    assert await _alerts(sm) == []


async def test_key_missing_from_the_list_clears_a_stale_expiry(sm):
    await _configure(sm)
    with _respx() as router:
        _mock(router, keys=[_key("k1", "2027-01-01T00:00:00.000Z")])
        await _reader(sm).tick()
    assert (await _state(sm))["key_expires_at"] == "2027-01-01T00:00:00+00:00"
    with _respx() as router:
        _mock(router, keys=[_key("k0", "2027-01-01T00:00:00.000Z")])
        await _reader(sm).tick()
    assert (await _state(sm))["key_expires_at"] is None
    # cli-auth/me not answering: the expiry is not known, the stored value is kept
    with _respx() as router:
        _mock(router, keys=[_key("k1", "2027-02-01T00:00:00.000Z")])
        await _reader(sm).tick()
    with _respx() as router:
        _mock(router)
        router.get(f"{BASE}/api/cli-auth/me").mock(return_value=httpx.Response(500))
        await _reader(sm).tick()
    st = await _state(sm)
    assert st["key_expires_at"] == "2027-02-01T00:00:00+00:00"
    assert st["consecutive_errors"] == 0


async def test_new_board_key_resets_the_expiry(sm):
    await _configure(sm)
    with _respx() as router:
        _mock(router, keys=[_key("k1", "2026-11-18T00:00:00.000Z")])
        await _reader(sm).tick()
    st = await _state(sm)
    assert st["key_expires_at"] and st["expiry_alerted"] == st["key_expires_at"]
    async with sm() as s:
        await save_config(s, base_url=BASE, company_id=CID, board_key="pcp_board_CANARY0002",
                          enabled=True, agent_map={}, by="test")
        await s.commit()
    st = await _state(sm)
    assert (st["key_expires_at"], st["expiry_alerted"]) == (None, None)
    assert st["baseline"] is not None  # same company and URL: the baseline stays


# -- configuration change -------------------------------------------------------------

async def test_company_change_resets_state_and_rebaselines_with_one_notice(sm):
    await _configure(sm)
    with _respx() as router:
        _mock(router, keys=[_key("k1", "2026-11-18T00:00:00.000Z")])
        await _reader(sm).tick()
    for _ in range(3):
        with _respx() as router:
            router.get(url__startswith=BASE).mock(return_value=httpx.Response(401))
            await _reader(sm).tick()
    async with sm() as s:
        await save_config(s, base_url=BASE, company_id="comp-2", board_key=None,
                          enabled=True, agent_map={}, by="test")
        await s.commit()
    st = await _state(sm)
    assert st["baseline"] is None and st["baseline_at"] is None
    assert st["isolation"] == "unknown" and st["rebaseline_reason"] == "config"
    assert st["changes"] == {"removed": [], "changed": [], "added": []}
    assert st["alerted_signature"] is None
    assert (st["consecutive_errors"], st["error_alerted"], st["last_error"]) == (0, False, None)
    assert (st["key_expires_at"], st["expiry_alerted"]) == (None, None)
    before = len(await _alerts(sm))
    # an empty answer after the reset stays unknown (no notice, the flag is kept)
    with _respx() as router:
        _mock(router, cid="comp-2", policies=[])
        await _reader(sm).tick()
    st = await _state(sm)
    assert st["isolation"] == "unknown" and st["rebaseline_reason"] == "config"
    assert len(await _alerts(sm)) == before
    for _ in range(2):
        with _respx() as router:
            _mock(router, cid="comp-2", policies=[POLICY_B])
            await _reader(sm).tick()
    st = await _state(sm)
    assert st["isolation"] == "ok" and [p["id"] for p in st["baseline"]] == ["p2"]
    assert st["rebaseline_reason"] is None
    new = (await _alerts(sm))[before:]
    assert [(a.text, a.category, a.level) for a in new] == [(
        ("ℹ️ Paperclip: configurazione cambiata, nuova fotografia delle regole di isolamento "
        "(1 regole). Controllala in Paperclip."), "system", "info")]


# -- secrets and agents' feed ---------------------------------------------------------

async def test_board_key_never_in_logs_or_outbox(sm, caplog):
    from smtrading.common.logsetup import configure_logging

    configure_logging(logging.DEBUG)
    await _configure(sm)
    with caplog.at_level(logging.DEBUG):
        with _respx() as router:
            _mock(router)
            await _reader(sm).tick()
            seen = [c.request.headers.get("authorization") for c in router.calls]
        assert seen and all(h == f"Bearer {KEY}" for h in seen)
        for _ in range(3):
            with _respx() as router:
                router.get(url__startswith=BASE).mock(
                    side_effect=httpx.ConnectError(f"failed with {KEY}"))
                await _reader(sm).tick()
        async with sm() as s:
            cfg = await load_config(s)
            logging.getLogger("smtrading.test").info("config %r", cfg)
    assert KEY not in repr(cfg)
    assert KEY not in caplog.text
    for r in caplog.records:
        assert KEY not in r.getMessage()
    for row in await _alerts(sm):
        assert KEY not in row.text
    async with sm() as s:
        dump = " ".join(str(v) for v in await s.scalars(select(AppSetting.value)))
    assert KEY not in dump


async def test_no_activity_rows_written(sm):
    async with sm() as s:
        before = await s.scalar(select(func.count()).select_from(Activity))
    await _configure(sm)
    with _respx() as router:
        _mock(router)
        await _reader(sm).tick()
    with _respx() as router:
        _mock(router, policies=[])
        await _reader(sm).tick()
    async with sm() as s:
        after = await s.scalar(select(func.count()).select_from(Activity))
    assert after == before

