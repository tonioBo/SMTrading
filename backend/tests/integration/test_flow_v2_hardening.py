"""«Fiume dei flussi» v2 review fixes: Neurograph health without false alarms (not
configured, one refusal, two in a row, a failed night), the forecast lookup bounded in time,
the Paperclip read bounded as a whole, the «oggi» window cached, error texts masked."""
# ruff: noqa: F811  (the `agent` fixture of test_flow_events, imported then requested)

import asyncio
import time
from datetime import timedelta

import pytest
from test_flow_events import TZ, S, T, _mcp, agent  # noqa: F401  (agent: fixture)

from smtrading.flow import agents as flow_agents_mod
from smtrading.flow import cache as flow_cache
from smtrading.flow.health import _ng, _safe
from smtrading.neurograph.models import NeurographExportNight
from smtrading.paperclip.config import PaperclipConfig

URL = "http://ng.example.test/api/serie"


@pytest.fixture(autouse=True)
def _fresh_caches():
    flow_cache.clear_all()
    yield
    flow_cache.clear_all()


async def test_ng_not_configured(db_session, agent):
    await _mcp(db_session, agent, "get_forecast", at=T - S)
    node = await _ng(db_session, TZ, now=T, neurograph_url=None)
    assert node["state"] == "sconosciuto" and "non configurato" in node["reason"]


async def test_ng_one_refusal_is_a_warning_two_are_an_error(db_session, agent):
    await _mcp(db_session, agent, "get_forecast", at=T - 3 * S)
    await _mcp(db_session, agent, "get_forecast", at=T - 2 * S, outcome="PROVIDER_UNAVAILABLE")
    await _mcp(db_session, agent, "get_forecast", at=T - S, outcome="INVALID_ORDER")  # agent's
    node = await _ng(db_session, TZ, now=T, neurograph_url=URL)
    assert node["state"] == "attenzione" and "barre insufficienti" in node["reason"]
    await _mcp(db_session, agent, "get_forecast", at=T - S // 2, outcome="PROVIDER_UNAVAILABLE")
    node = await _ng(db_session, TZ, now=T, neurograph_url=URL)
    assert node["state"] == "errore" and "2" in node["reason"]


async def test_ng_ok_and_old_calls_ignored(db_session, agent):
    await _mcp(db_session, agent, "get_forecast", at=T - timedelta(days=8),
               outcome="PROVIDER_UNAVAILABLE")
    await _mcp(db_session, agent, "get_forecast", at=T - timedelta(days=9),
               outcome="PROVIDER_UNAVAILABLE")
    node = await _ng(db_session, TZ, now=T, neurograph_url=URL)
    assert node["state"] == "sconosciuto"  # nothing in the last 7 days
    await _mcp(db_session, agent, "get_forecast", at=T - S)
    node = await _ng(db_session, TZ, now=T, neurograph_url=URL)
    assert node["state"] == "ok" and "riuscita" in node["reason"]


@pytest.mark.parametrize("outcome", ["outage", "unauthorized"])
async def test_ng_failed_night_is_an_error(db_session, agent, outcome):
    await _mcp(db_session, agent, "get_forecast", at=T - S)
    db_session.add(NeurographExportNight(night=(T - timedelta(days=1)).date(), outcome=outcome))
    await db_session.flush()
    node = await _ng(db_session, TZ, now=T, neurograph_url=URL)
    assert node["state"] == "errore" and "invio notturno" in node["reason"]


async def test_paperclip_read_bounded_as_a_whole(monkeypatch):
    class Slow:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return None

        async def agents(self):
            await asyncio.sleep(0.15)  # each request under its own timeout…
            return []

        async def heartbeat_runs(self, limit):
            await asyncio.sleep(0.15)  # …but the two together over the total
            return []

    monkeypatch.setattr(flow_agents_mod, "_client", lambda cfg: Slow())
    monkeypatch.setattr(flow_agents_mod, "FETCH_TIMEOUT_S", 0.2)
    cfg = PaperclipConfig(base_url="https://pc.example.test", company_id="c",
                          board_key="pcp_board_SLOW0001", enabled=True)
    started = time.monotonic()
    snap = await flow_agents_mod.paperclip_snapshot(cfg)
    assert time.monotonic() - started < 1
    assert snap["ok"] is False and "in tempo" in snap["error"]
    assert "SLOW0001" not in repr(snap)


def test_error_texts_are_masked():
    assert "abc123secret" not in _safe("HTTP 401 token=abc123secret")
    assert _safe(None) is None
