"""get_forecast on the live fixture server, against a fake Neurograph «serie» endpoint."""

import json
import threading
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from smtrading.common.config import get_settings
from smtrading.common.db import make_engine
from smtrading.marketdata.types import Bar
from smtrading.seed import run_seed

D = Decimal
SYM = "BTC-USD"
NOW = datetime.now(UTC).replace(second=0, microsecond=0)
Q = [0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9]


def _bars(n: int) -> list[Bar]:
    return [Bar(NOW - timedelta(days=n - i), D(100 + i), D(101 + i), D(99 + i), D(100 + i), D(10))
            for i in range(n)]


@pytest.fixture
def neurograph():
    yield from _fake_neurograph(good=True)


@pytest.fixture
def neurograph_odd():
    yield from _fake_neurograph(good=False)


def _fake_neurograph(good: bool):
    seen: list[dict] = []

    class Fake(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            seen.append({"path": self.path, **body})
            daily = body["timeframe"] == "1d"  # daily bars: HAR + scheduled events; otherwise GARCH
            closes = [b["c"] for b in body["barre"]]
            h, last = body["h"], closes[-1]
            out = {} if not good else {"modello": "har_eventi" if daily else "garch", "h": h, "quantili": Q, "ultimo": last,
                   "barre_usate": len(closes),
                   "eventi": [{"tipo": "utili", "data": "2030-01-02", "fra_barre": 2}] if daily else [],
                   "prezzi": [[last * (1 + (lv - 0.5) * 0.1 * (k + 1)) for lv in Q] for k in range(h)],
                   "sigma_h": 0.031, "forchetta80": [-0.04, 0.04]}
            b = json.dumps(out).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Fake)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}/api/serie", seen
    srv.shutdown()


@pytest.fixture
async def env(mcp_server, mcp_call):
    engine = make_engine(mcp_server.db_url)
    async with AsyncSession(engine, expire_on_commit=False) as s:
        await run_seed(s)
        await s.commit()
    await engine.dispose()

    async def call(tool, args=None):
        return await mcp_call(mcp_server.url, mcp_server.token, tool, args or {})

    return {"call": call, "qs": mcp_server.qs}


def _configure(monkeypatch, url):
    if url is None:
        monkeypatch.delenv("SMT_NEUROGRAPH_URL", raising=False)
    else:
        monkeypatch.setenv("SMT_NEUROGRAPH_URL", url)
    get_settings.cache_clear()


async def test_listed(mcp_server, mcp_list_tools):
    assert "get_forecast" in set(await mcp_list_tools(mcp_server.url, mcp_server.token))


async def test_not_configured(env, monkeypatch):
    _configure(monkeypatch, None)
    err, out = await env["call"]("get_forecast", {"symbol": SYM})
    assert err and out["code"] == "PROVIDER_UNAVAILABLE"


async def test_forecast(env, neurograph, monkeypatch):
    url, seen = neurograph
    _configure(monkeypatch, url)
    env["qs"].set_bars(SYM, _bars(300))
    err, out = await env["call"]("get_forecast", {"symbol": SYM, "timeframe": "1d", "horizon": 3})
    assert not err, out
    assert seen[-1]["path"] == "/api/serie/rischio" and seen[-1]["h"] == 3
    sent = seen[-1]  # symbol, asset class, timeframe and OHLC, oldest first
    assert sent["simbolo"] == SYM and sent["classe"] == "crypto" and sent["timeframe"] == "1d"
    assert len(sent["barre"]) == 300 and sent["barre"][-1]["c"] == 399.0 and sent["barre"][-1]["h"] == 400.0
    assert sent["barre"][-1]["t"][:10] == _bars(300)[-1].ts.date().isoformat()
    assert out["symbol"] == SYM and out["model"] == "har_eventi" and out["horizon"] == 3
    assert out["events"] == [{"type": "earnings", "date": "2030-01-02", "in_bars": 2}]
    assert "Scheduled events" in out["note"]
    assert out["range80_pct"] == ["-4.00", "4.00"] and out["sigma_pct"] == "3.10"
    assert len(out["steps"]) == 3 and set(out["steps"][0]["price"]) == {f"q{k}" for k in range(10, 100, 10)}
    assert out["range80_price"] == [out["steps"][-1]["price"]["q10"], out["steps"][-1]["price"]["q90"]]
    assert "never" in out["note"]


async def test_forecast_intraday(env, neurograph, monkeypatch):
    url, seen = neurograph
    _configure(monkeypatch, url)
    env["qs"].set_bars(SYM, [Bar(NOW - timedelta(hours=300 - i), D(100 + i), D(101 + i), D(99 + i), D(100 + i), D(10))
                             for i in range(300)])
    err, out = await env["call"]("get_forecast", {"symbol": SYM, "timeframe": "1h", "horizon": 2})
    assert not err, out
    assert seen[-1]["timeframe"] == "1h" and len(seen[-1]["barre"]) == 300
    assert out["model"] == "garch" and out["events"] == [] and "Scheduled events" not in out["note"]


async def test_bad_input_and_short_history(env, neurograph, monkeypatch):
    url, seen = neurograph
    _configure(monkeypatch, url)
    env["qs"].set_bars(SYM, _bars(50))
    err, out = await env["call"]("get_forecast", {"symbol": SYM})
    assert err and out["code"] == "PROVIDER_UNAVAILABLE" and out["details"]["bars"] == 50
    err, out = await env["call"]("get_forecast", {"symbol": SYM, "horizon": 21})
    assert err and out["code"] == "INVALID_ORDER"
    assert not seen  # nothing reached Neurograph


async def test_neurograph_down(env, monkeypatch):
    _configure(monkeypatch, "http://127.0.0.1:9/api/serie")
    env["qs"].set_bars(SYM, _bars(300))
    err, out = await env["call"]("get_forecast", {"symbol": SYM})
    assert err and out["code"] == "PROVIDER_UNAVAILABLE"


async def test_neurograph_unexpected_answer(env, neurograph_odd, monkeypatch):
    url, seen = neurograph_odd
    _configure(monkeypatch, url)
    env["qs"].set_bars(SYM, _bars(300))
    err, out = await env["call"]("get_forecast", {"symbol": SYM})
    assert seen and err and out["code"] == "PROVIDER_UNAVAILABLE"
