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

POLICY_A = {"id": "p1", "name": "Isolate trader SMTrading", "policyType": "deny",
            "selectors": {"agent": "trader"}, "conditions": {}, "config": {"x": 1},
            "priority": 10, "enabled": True, "createdAt": "ignored"}
POLICY_B = {"id": "p2", "name": "Isolate analyst SMTrading", "policyType": "deny",
            "selectors": {"agent": "analyst"}, "conditions": {}, "config": {},
            "priority": 10, "enabled": True}
OTHER = {"id": "p9", "name": "Unrelated policy", "policyType": "allow", "selectors": {},
         "conditions": {}, "config": {}, "priority": 1, "enabled": True}


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
    return [
        {"id": "r0", "agentId": "a1", "startedAt": "2026-11-13T09:00:00Z",
         "usageJson": {"costUsd": 5, "billingType": "metered_api"}},
        {"id": "r1", "agentId": "a1", "startedAt": "2026-11-14T09:00:00Z",
         "usageJson": {"costUsd": 1, "cacheAdjustedCostUsd": 0.5,
                       "billingType": "metered_api"}},
        {"id": "r2", "agentId": "a1", "startedAt": "2026-11-14T10:00:00Z",
         "usageJson": {"costUsd": 2, "billingType": "subscription_included"}},
        {"id": "r3", "agentId": "a2", "startedAt": "2026-11-15T10:00:00Z",
         "usageJson": {"costUsd": 0.25, "billingType": "credits"}},
    ]


def _mock(router, *, policies=None, runs=None, agents=None):
    router.get(f"{API}/heartbeat-runs").mock(
        return_value=httpx.Response(200, json=runs if runs is not None else _runs()))
    router.get(f"{API}/agents").mock(return_value=httpx.Response(
        200, json=agents if agents is not None else
        [{"id": "a1", "name": "Trader"}, {"id": "a2", "name": "Analyst"}]))
    pol = router.get(f"{API}/tools/policies").mock(return_value=httpx.Response(
        200, json={"items": policies if policies is not None else [POLICY_A, POLICY_B, OTHER]}))
    router.get(f"{BASE}/api/cli-auth/me").mock(return_value=httpx.Response(404))
    router.get(f"{BASE}/api/board-api-keys").mock(return_value=httpx.Response(404))
    return pol


def _respx():
    return respx.mock(assert_all_mocked=True, assert_all_called=False)


async def _alerts(sm) -> list[TelegramOutbox]:
    async with sm() as s:
        return list(await s.scalars(select(TelegramOutbox).order_by(TelegramOutbox.id)))


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
    assert st["baseline_at"] and st["last_ok_at"] and st["last_error"] is None
    assert await _alerts(sm) == []


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
    assert len(await _alerts(sm)) == 2
    # back to the baseline: ok, and the same change later alerts again
    with _respx() as router:
        _mock(router)
        await _reader(sm).tick()
    assert (await _state(sm))["isolation"] == "ok"
    with _respx() as router:
        _mock(router, policies=[disabled, POLICY_B])
        await _reader(sm).tick()
    assert len(await _alerts(sm)) == 3


async def test_added_policy_listed_no_alert(sm):
    await _configure(sm)
    with _respx() as router:
        _mock(router)
        await _reader(sm).tick()
    extra = {**POLICY_B, "id": "p3", "name": "Isolate new SMTrading"}
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
        router.get(f"{API}/agents").mock(return_value=httpx.Response(500))
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
    expires = "2026-11-20T00:00:00Z"
    for now in (NOW - timedelta(days=3), NOW, NOW + timedelta(hours=1)):
        with _respx() as router:
            _mock(router)
            router.get(f"{BASE}/api/cli-auth/me").mock(
                return_value=httpx.Response(200, json={"keyId": "k1"}))
            router.get(f"{BASE}/api/board-api-keys").mock(return_value=httpx.Response(
                200, json=[{"id": "k0", "expiresAt": None},
                           {"id": "k1", "expiresAt": expires}]))
            await _reader(sm, now=now).tick()
    st = await _state(sm)
    assert st["key_expires_at"] == "2026-11-20T00:00:00+00:00"
    (alert,) = await _alerts(sm)  # 8 days before: nothing; then one notice
    assert alert.category == "system" and "20/11/2026" in alert.text
    assert KEY not in alert.text


async def test_key_without_expiry_no_alert(sm):
    await _configure(sm)
    async with sm() as s:
        await save_config(s, base_url=BASE, company_id=CID, board_key=None, enabled=True,
                          agent_map={}, by="test", board_key_id="k1")
        await s.commit()
    with _respx() as router:
        _mock(router)
        router.get(f"{BASE}/api/board-api-keys").mock(return_value=httpx.Response(
            200, json={"items": [{"id": "k1", "expiresAt": None}]}))
        await _reader(sm).tick()
    assert (await _state(sm))["key_expires_at"] is None
    assert await _alerts(sm) == []


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

