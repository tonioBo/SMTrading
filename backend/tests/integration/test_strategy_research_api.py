"""Portal «Strategie · Ricerca»: a viewer lists the agents' research runs
(`GET /api/strategies/research`) and reads one with its result."""

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx
import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

from smtrading.agents.service import create_agent
from smtrading.backtest.models import BacktestStatus, ResearchKind, ResearchRun
from smtrading.common.config import get_settings
from smtrading.common.db import make_engine
from smtrading.engine.quotesource import FakeMarketData
from smtrading.seed import run_seed

D = Decimal
PASSWORD = "correct-horse-battery"
T0 = datetime(2024, 1, 1, tzinfo=UTC)


@pytest.fixture
async def sm(fresh_db, redis_url, monkeypatch):
    monkeypatch.setenv("SMT_DATABASE_URL", fresh_db)
    monkeypatch.setenv("SMT_REDIS_URL", redis_url)
    monkeypatch.setenv("SMT_MASTER_KEY", "test-master-key")
    monkeypatch.setenv("SMT_ADMIN_EMAIL", "admin@example.com")
    monkeypatch.setenv("SMT_ADMIN_PASSWORD", "test-password")
    get_settings.cache_clear()
    engine = make_engine(fresh_db)
    sm = async_sessionmaker(engine, expire_on_commit=False)
    async with sm() as s:
        await run_seed(s)
        await s.commit()
    yield sm
    await engine.dispose()


def _run(agent_id, kind, minutes, **kw):
    base = {"agent_id": agent_id, "kind": kind, "strategy": "sma_cross", "symbols": ["SPY"],
            "timeframe": "1d", "start": T0, "end": T0 + timedelta(days=700),
            "initial_cash": D(10000), "spec": {}, "status": BacktestStatus.completed,
            "warnings": [], "created_at": T0 + timedelta(minutes=minutes)}
    return ResearchRun(**{**base, **kw})


async def test_viewer_lists_and_reads_research(sm):
    from smtrading.admin.auth import hash_password
    from smtrading.admin.models import AdminUser
    from smtrading.api.main import create_app
    from smtrading.mcp.runtime import McpRuntime

    email = f"viewer-{uuid.uuid4().hex[:8]}@example.com"
    async with sm() as s:
        quant, _ = await create_agent(s, "Quant Researcher")
        wf = _run(quant.id, ResearchKind.walk_forward, 1, result={
            "verdict": "solida", "oos_metrics": {"total_return_pct": "4.1"},
            "stability": {"stability_pct": "75"}, "windows": []})
        sweep = _run(quant.id, ResearchKind.param_sweep, 2, result={
            "best_by_metric": {"fast": 5}, "best_by_robust": {"fast": 10},
            "isolated_peak": True, "rows": [{"rank": 1}]})
        queued = _run(quant.id, ResearchKind.portfolio, 3, strategy=None,
                      status=BacktestStatus.queued)
        s.add_all([wf, sweep, queued])
        s.add(AdminUser(email=email, password_hash=await hash_password(PASSWORD),
                        role="viewer"))
        await s.commit()
    qs = FakeMarketData()
    app = create_app(runtime_factory=lambda sm_, redis_: McpRuntime(sm_, redis_, qs, qs))
    started, stop = asyncio.Event(), asyncio.Event()

    async def run_lifespan():
        async with app.router.lifespan_context(app):
            started.set()
            await stop.wait()

    lifespan = asyncio.create_task(run_lifespan())
    await asyncio.wait_for(started.wait(), 15)
    try:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            r = await c.get("/api/strategies/research")
            assert r.status_code == 401
            r = await c.post("/api/auth/login", json={"email": email, "password": PASSWORD})
            assert r.status_code == 200, r.text
            r = await c.get("/api/strategies/research")
            assert r.status_code == 200, r.text
            runs = r.json()["runs"]
            assert [x["kind"] for x in runs] == ["portfolio", "param_sweep", "walk_forward"]
            assert runs[2]["summary"] == {"verdict": "solida",
                                          "oos_metrics": {"total_return_pct": "4.1"},
                                          "stability_pct": "75"}
            assert runs[1]["summary"]["isolated_peak"] is True
            assert runs[0]["summary"] is None and runs[0]["agent"] == "Quant Researcher"
            assert all("result" not in x for x in runs)
            r = await c.get("/api/strategies/research", params={"kind": "walk_forward"})
            assert [x["id"] for x in r.json()["runs"]] == [str(wf.id)]
            r = await c.get("/api/strategies/research", params={"kind": "magic"})
            assert r.status_code == 400  # the API answers validation errors with 400
            r = await c.get(f"/api/strategies/research/{wf.id}")
            assert r.status_code == 200 and r.json()["result"]["verdict"] == "solida"
            r = await c.get(f"/api/strategies/research/{uuid.uuid4()}")
            assert r.status_code == 404
    finally:
        stop.set()
        await lifespan
