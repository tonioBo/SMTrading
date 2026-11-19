"""Agents' Paperclip sessions reset after an MCP tool catalog change (reader pass): first
run stores the fingerprint, a change resets every agent that is not running (the running
ones on a later pass), each agent once, one Telegram info message at the end; failures
never loop and never stop the reader. Paperclip is mocked (respx)."""

import asyncio
import uuid
from datetime import UTC, datetime

import httpx
import pytest
import respx
from sqlalchemy import delete, make_url, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from conftest import run_migrations
from smtrading.common.config import get_settings
from smtrading.common.db import make_engine
from smtrading.paperclip.client import GetOnlyTransport, PaperclipClient
from smtrading.paperclip.config import load_state, save_config
from smtrading.paperclip.reader import PaperclipReader
from smtrading.paperclip.sessions import load_sessions, sessions_dto
from smtrading.risk.models import AppSetting
from smtrading.telegram.models import TelegramOutbox

BASE = "https://paperclip.example.test"
CID = "comp-1"
KEY = "pcp_board_CANARY0003"
API = f"{BASE}/api/companies/{CID}"
RESET = rf"^{BASE}/api/agents/(?P<aid>[^/]+)/runtime-state/reset-session$"
NOW = datetime(2026, 11, 16, 23, 0, tzinfo=UTC)

AGENTS = [{"id": "a1", "name": "Trader", "status": "idle"},
          {"id": "a2", "name": "Analyst", "status": "running"},
          {"id": "a3", "name": "Risk", "status": "paused"}]


async def _admin_sql(pg_url: str, sql: str) -> None:
    engine = create_async_engine(make_url(pg_url).set(database="postgres"),
                                 isolation_level="AUTOCOMMIT")
    try:
        async with engine.connect() as conn:
            await conn.execute(text(sql))
    finally:
        await engine.dispose()


@pytest.fixture(scope="module")
def sessions_db(pg_url):
    name = f"t_pcs_{uuid.uuid4().hex}"
    asyncio.run(_admin_sql(pg_url, f'CREATE DATABASE "{name}"'))
    url = make_url(pg_url).set(database=name).render_as_string(hide_password=False)
    run_migrations(url)
    yield url
    asyncio.run(_admin_sql(pg_url, f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))


@pytest.fixture
async def sm(sessions_db, redis_url, monkeypatch):
    monkeypatch.setenv("SMT_DATABASE_URL", sessions_db)
    monkeypatch.setenv("SMT_REDIS_URL", redis_url)
    monkeypatch.setenv("SMT_MASTER_KEY", "test-master-key")
    monkeypatch.setenv("SMT_ADMIN_EMAIL", "admin@example.com")
    monkeypatch.setenv("SMT_ADMIN_PASSWORD", "test-password")
    get_settings.cache_clear()
    engine = make_engine(sessions_db)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as s:
        await s.execute(delete(AppSetting).where(AppSetting.key.in_(
            ["paperclip", "paperclip_state", "paperclip_sessions"])))
        await s.execute(text("delete from telegram_outbox"))
        await s.commit()
    yield maker
    await engine.dispose()


async def _configure(sm, enabled=True):
    async with sm() as s:
        await save_config(s, base_url=BASE, company_id=CID, board_key=KEY, enabled=enabled,
                          agent_map={}, by="test")
        await s.commit()


def _reader(sm, fp: str):
    async def fingerprint() -> str:
        return fp
    return PaperclipReader(sm, clock=lambda: NOW, fingerprint=fingerprint)


def _respx():
    return respx.mock(assert_all_mocked=True, assert_all_called=False)


def _mock(router, agents=None, reset_status=200):
    """The reader's GETs answered with nothing interesting, the agents list and the
    reset endpoint (the real answer: the runtime state with `sessionId` null)."""
    router.get(f"{API}/heartbeat-runs").mock(return_value=httpx.Response(200, json=[]))
    router.get(f"{API}/tools/policies").mock(return_value=httpx.Response(200, json=[]))
    router.get(f"{API}/routines").mock(return_value=httpx.Response(200, json=[]))
    router.get(f"{API}/issues").mock(return_value=httpx.Response(200, json=[]))
    router.get(API).mock(return_value=httpx.Response(200, json={"id": CID,
                                                                 "budgetMonthlyCents": 0}))
    router.get(f"{BASE}/api/cli-auth/me").mock(return_value=httpx.Response(200, json={}))
    router.get(f"{API}/agents").mock(return_value=httpx.Response(
        200, json=AGENTS if agents is None else agents))

    def reset(request, aid):
        return httpx.Response(reset_status if isinstance(reset_status, int)
                              else reset_status.get(aid, 200),
                              json={"agentId": aid, "sessionId": None})

    return router.post(url__regex=RESET).mock(side_effect=reset)


def _reset_ids(route) -> list[str]:
    return [c.request.url.path.split("/")[3] for c in route.calls]


async def _alerts(sm) -> list[TelegramOutbox]:
    async with sm() as s:
        return list(await s.scalars(select(TelegramOutbox).order_by(TelegramOutbox.id)))


async def _sessions(sm) -> dict | None:
    async with sm() as s:
        return await load_sessions(s)


async def _seed(sm, fp="fp-old"):
    """An install that already stored a fingerprint (first run without Paperclip)."""
    await _reader(sm, fp).tick()
    assert (await _sessions(sm))["fingerprint"] == fp


# -- transport --------------------------------------------------------------------------

async def test_transport_lets_through_only_the_session_reset():
    with _respx() as router:
        ok = router.post(url__regex=RESET).mock(return_value=httpx.Response(200, json={}))
        other = router.post(url__startswith=BASE).mock(return_value=httpx.Response(200))
        async with httpx.AsyncClient(transport=GetOnlyTransport()) as http:
            with pytest.raises(RuntimeError, match="Paperclip: solo lettura"):
                await http.post(f"{BASE}/api/agents/a1/runtime-state/reset-session", json={})
        async with httpx.AsyncClient(
                transport=GetOnlyTransport(allow_session_reset=True)) as http:
            await http.post(f"{BASE}/api/agents/a1/runtime-state/reset-session", json={})
            for method, url in (
                    ("POST", f"{API}/agents"),
                    ("PUT", f"{BASE}/api/agents/a1/runtime-state/reset-session"),
                    ("DELETE", f"{BASE}/api/agents/a1/runtime-state/reset-session"),
                    ("POST", f"{BASE}/api/agents/a1/runtime-state/reset-session/x"),
                    ("POST", f"{BASE}/api/agents/a1/x/runtime-state/reset-session"),
                    ("POST", f"{BASE}/api/agents/a1/pause")):
                with pytest.raises(RuntimeError, match="Paperclip: solo lettura"):
                    await http.request(method, url, json={})
        assert ok.call_count == 1 and other.call_count == 0


async def test_client_reset_session_posts_an_empty_body():
    from smtrading.paperclip.config import PaperclipConfig

    cfg = PaperclipConfig(base_url=BASE, company_id=CID, board_key=KEY, enabled=True)
    with _respx() as router:
        route = router.post(url__regex=RESET).mock(
            return_value=httpx.Response(200, json={"sessionId": None}))
        async with PaperclipClient(cfg, allow_session_reset=True) as client:
            assert await client.reset_session("a/1") == {"sessionId": None}
        async with PaperclipClient(cfg) as client:  # the reader's client cannot write
            with pytest.raises(RuntimeError):
                await client.reset_session("a1")
    (call,) = route.calls
    assert call.request.url.raw_path == b"/api/agents/a%2F1/runtime-state/reset-session"
    assert call.request.content == b"{}"
    assert call.request.headers["authorization"] == f"Bearer {KEY}"


async def test_client_reset_session_under_a_base_path_prefix():
    """Paperclip served under a path (`https://host/pc`): the reset is allowed there, and
    only there."""
    from smtrading.paperclip.config import PaperclipConfig

    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.raw_path)
        return httpx.Response(200, json={})

    cfg = PaperclipConfig(base_url=f"{BASE}/pc/", company_id=CID, board_key=KEY, enabled=True)
    async with PaperclipClient(cfg, allow_session_reset=True,
                               transport=httpx.MockTransport(handler)) as client:
        await client.reset_session("a1")
        with pytest.raises(RuntimeError, match="Paperclip: solo lettura"):
            await client._http.post(f"{BASE}/api/agents/a1/runtime-state/reset-session")
    assert seen == [b"/pc/api/agents/a1/runtime-state/reset-session"]


# -- fingerprint ------------------------------------------------------------------------

async def test_first_run_stores_the_fingerprint_and_resets_nothing(sm):
    await _configure(sm)
    with _respx() as router:
        route = _mock(router)
        await _reader(sm, "fp-1").tick()
    assert route.call_count == 0
    st = await _sessions(sm)
    assert st["fingerprint"] == "fp-1" and st["pending"] is None
    assert await _alerts(sm) == []
    with _respx() as router:  # same catalog: nothing
        route = _mock(router)
        await _reader(sm, "fp-1").tick()
    assert route.call_count == 0 and await _alerts(sm) == []


async def test_first_run_with_paperclip_disabled_stores_without_requests(sm):
    with _respx() as router:
        await _reader(sm, "fp-1").tick()
        assert router.calls.call_count == 0
    assert (await _sessions(sm))["fingerprint"] == "fp-1"


async def test_change_resets_idle_agents_running_ones_on_a_later_pass(sm):
    await _seed(sm)
    await _configure(sm)
    with _respx() as router:
        route = _mock(router)
        await _reader(sm, "fp-new").tick()
        assert all(c.request.method == "GET" for c in router.calls
                   if c.request.url.path.endswith("/agents"))
    assert sorted(_reset_ids(route)) == ["a1", "a3"]  # never the running one
    st = await _sessions(sm)
    assert st["fingerprint"] == "fp-old" and st["pending"] == "fp-new"
    assert st["waiting"] == ["Analyst"] and sorted(st["done"]) == ["a1", "a3"]
    assert sessions_dto(st) == {"pending": True, "refreshed": 2, "waiting": ["Analyst"],
                                "given_up": [], "last_error": None,
                                "updated_at": NOW.isoformat()}
    assert await _alerts(sm) == []
    # still running: nothing new
    with _respx() as router:
        route = _mock(router)
        await _reader(sm, "fp-new").tick()
    assert route.call_count == 0 and await _alerts(sm) == []
    # idle now: only it is reset, the fingerprint is stored, one message
    idle = [{**a, "status": "idle"} for a in AGENTS]
    with _respx() as router:
        route = _mock(router, agents=idle)
        await _reader(sm, "fp-new").tick()
    assert _reset_ids(route) == ["a2"]
    st = await _sessions(sm)
    assert (st["fingerprint"], st["pending"], st["waiting"], st["done"]) == (
        "fp-new", None, [], [])
    (alert,) = await _alerts(sm)
    assert (alert.text, alert.category, alert.level) == (
        "Strumenti degli agenti aggiornati: sessioni ripartite per 3 agenti", "system", "info")
    # later passes and a restart (a new reader, same catalog): nothing
    for _ in range(2):
        with _respx() as router:
            route = _mock(router, agents=idle)
            await _reader(sm, "fp-new").tick()
        assert route.call_count == 0
    assert len(await _alerts(sm)) == 1


async def test_unknown_status_waits(sm):
    await _seed(sm)
    await _configure(sm)
    with _respx() as router:
        route = _mock(router, agents=[{"id": "a1", "name": "Trader"},
                                      {"id": "a2", "name": "Analyst", "status": "error"}])
        await _reader(sm, "fp-new").tick()
    assert _reset_ids(route) == ["a2"]
    assert (await _sessions(sm))["waiting"] == ["Trader"]


async def test_single_agent_message(sm):
    await _seed(sm)
    await _configure(sm)
    with _respx() as router:
        _mock(router, agents=[AGENTS[0]])
        await _reader(sm, "fp-new").tick()
    (alert,) = await _alerts(sm)
    assert alert.text == "Strumenti degli agenti aggiornati: sessioni ripartite per 1 agente"


async def test_a_new_change_while_pending_starts_over(sm):
    await _seed(sm)
    await _configure(sm)
    with _respx() as router:
        _mock(router)
        await _reader(sm, "fp-2").tick()
    with _respx() as router:  # another release before Analyst was idle
        route = _mock(router)
        await _reader(sm, "fp-3").tick()
    assert sorted(_reset_ids(route)) == ["a1", "a3"]
    assert (await _sessions(sm))["pending"] == "fp-3"


async def test_catalog_back_to_the_stored_one_clears_the_pending_change(sm):
    await _seed(sm)
    await _configure(sm)
    with _respx() as router:
        _mock(router)
        await _reader(sm, "fp-2").tick()
    with _respx() as router:
        route = _mock(router)
        await _reader(sm, "fp-old").tick()
    assert route.call_count == 0
    st = await _sessions(sm)
    assert (st["fingerprint"], st["pending"], st["waiting"]) == ("fp-old", None, [])


async def test_disabled_paperclip_keeps_the_change_pending(sm):
    await _seed(sm)
    await _configure(sm, enabled=False)
    with _respx() as router:
        await _reader(sm, "fp-new").tick()
        assert router.calls.call_count == 0
    assert (await _sessions(sm))["fingerprint"] == "fp-old"
    await _configure(sm, enabled=True)
    with _respx() as router:
        route = _mock(router, agents=[AGENTS[0]])
        await _reader(sm, "fp-new").tick()
    assert _reset_ids(route) == ["a1"]
    assert (await _sessions(sm))["fingerprint"] == "fp-new"


# -- failures ---------------------------------------------------------------------------

async def test_agents_list_failure_resets_nothing_and_is_shown(sm):
    await _seed(sm)
    await _configure(sm)
    with _respx() as router:
        route = _mock(router)
        router.get(f"{API}/agents").mock(return_value=httpx.Response(503))
        await _reader(sm, "fp-new").tick()
    assert route.call_count == 0
    st = await _sessions(sm)
    assert st["fingerprint"] == "fp-old" and st["pending"] == "fp-new"
    assert st["last_error"] == "sessioni agenti non aggiornate: HTTP 503"
    async with sm() as s:  # not the reader's own error count
        assert (await load_state(s))["consecutive_errors"] == 0
    with _respx() as router:
        route = _mock(router, agents=[AGENTS[0]])
        await _reader(sm, "fp-new").tick()
    assert _reset_ids(route) == ["a1"]
    st = await _sessions(sm)
    assert st["fingerprint"] == "fp-new" and st["last_error"] is None


async def test_failing_reset_retried_at_most_three_times_then_given_up(sm):
    await _seed(sm)
    await _configure(sm)
    agents = [AGENTS[0], {**AGENTS[2], "status": "idle"}]
    calls: list[str] = []
    for _ in range(5):
        with _respx() as router:
            route = _mock(router, agents=agents, reset_status={"a1": 500})
            await _reader(sm, "fp-new").tick()
        calls += _reset_ids(route)
    assert calls.count("a1") == 3 and calls.count("a3") == 1
    st = await _sessions(sm)
    assert st["fingerprint"] == "fp-new" and st["pending"] is None
    assert st["given_up"] == ["Trader"]
    assert st["last_error"] == "sessione non azzerata per Trader: HTTP 500"
    (alert,) = await _alerts(sm)
    assert alert.text == ("Strumenti degli agenti aggiornati: sessioni ripartite per 1 agente "
                          "(non riuscito per: Trader)")
    assert KEY not in alert.text


async def test_a_failing_refresh_never_stops_the_reader(sm):
    await _configure(sm)

    async def broken() -> str:
        raise RuntimeError("boom")

    with _respx() as router:
        _mock(router)
        await PaperclipReader(sm, clock=lambda: NOW, fingerprint=broken).tick()
    async with sm() as s:
        assert (await load_state(s))["last_ok_at"] == NOW.isoformat()


async def test_reader_errors_do_not_stop_the_refresh(sm):
    await _seed(sm)
    await _configure(sm)
    with _respx() as router:
        route = _mock(router, agents=[AGENTS[0]])
        router.get(f"{API}/heartbeat-runs").mock(return_value=httpx.Response(500))
        await _reader(sm, "fp-new").tick()
    assert _reset_ids(route) == ["a1"]
    async with sm() as s:
        assert (await load_state(s))["consecutive_errors"] == 1


async def test_an_unexpected_error_keeps_the_resets_already_made(sm):
    await _seed(sm)
    await _configure(sm)
    agents = [AGENTS[0], {**AGENTS[2], "status": "idle"}]

    def reset(request, aid):
        if aid == "a3":
            raise RuntimeError("boom")
        return httpx.Response(200, json={"sessionId": None})

    with _respx() as router:
        _mock(router, agents=agents)
        route = router.post(url__regex=RESET).mock(side_effect=reset)
        await _reader(sm, "fp-new").tick()  # logged, never raised
    assert _reset_ids(route) == ["a1", "a3"]
    st = await _sessions(sm)
    assert st["done"] == ["a1"] and st["pending"] == "fp-new"
    assert st["last_error"] == "sessioni agenti non aggiornate: errore interno"
    with _respx() as router:
        route = _mock(router, agents=agents)
        await _reader(sm, "fp-new").tick()
    assert _reset_ids(route) == ["a3"]  # never a1 again
    assert (await _sessions(sm))["fingerprint"] == "fp-new"
    (alert,) = await _alerts(sm)
    assert alert.text.endswith("per 2 agenti")
