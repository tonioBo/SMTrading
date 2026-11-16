"""Agents connected via MCP must believe everything is real (mcp_hide_mode on, the seeded
default): no agent-facing output or error may carry a simulation fingerprint.

One sweep per portfolio flavor: orders, fills, a rejection, a cancel, a deposit, an
alert, a watchlist and a decision on the portfolio, then every agent-facing read tool,
the resources and a few errors are called and the JSON they return is scanned for the
words that would give the simulation away (`FINGERPRINT`, shared with the admin name
warning in `smtrading.common.fingerprints`).

Allowlist (documented, not scanned):
- the backtest tools (run_backtest / get_backtest / list_backtests): a backtest is a
  simulation by definition and says so; none of them is called here.
- names an admin chose (portfolio, agent, risk profile names): the fixture renames the
  seeded "Demo" portfolio and "test-agent" agent to neutral names, since the words would
  be false positives of the data, not of the code.
- market data provider names come from the real providers in production; the fixture's
  fake source is given a real provider name ("yahoo"); in the `etoro_source` flavor
  quotes and bars come from the real eToro provider on the fake eToro server.
"""

import dataclasses
import json
import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from smtrading.agents import service as agents
from smtrading.agents.models import Agent
from smtrading.common.db import make_engine
from smtrading.common.fingerprints import FINGERPRINT
from smtrading.marketdata.types import Bar, CalendarEvent, Fundamentals, NewsItem
from smtrading.portfolios.models import Portfolio
from smtrading.portfolios.service import create_portfolio, deposit
from smtrading.seed import run_seed

D = Decimal
SYM = "BTC-USD"
NOW = datetime.now(UTC).replace(microsecond=0)
ETORO_CANARY = {"api_key": "CANARYETOROAPI0001", "user_key": "CANARYETOROUSER-do-not-use"}

class _Db:
    def __init__(self, url: str) -> None:
        self.engine = make_engine(url)

    async def __aenter__(self) -> AsyncSession:
        self.session = AsyncSession(self.engine, expire_on_commit=False)
        return self.session

    async def __aexit__(self, *exc) -> None:
        await self.session.close()
        await self.engine.dispose()


def _market(qs) -> None:
    qs.set_fx("EUR", "USD", D("1.25"))
    qs.set_quote(SYM, D(50000), D(50000), D(50000))
    qs._quotes[SYM] = dataclasses.replace(qs._quotes[SYM], provider="yahoo")
    step = timedelta(days=1)
    qs.set_bars(SYM, [
        Bar(NOW - step * (60 - i), D(100 + i), D(101 + i), D(99 + i), D(100 + i), D(10))
        for i in range(60)
    ])
    qs.set_news([NewsItem("1", NOW - timedelta(hours=1), "Bitcoin sale", "s", "http://x",
                          "Reuters", (SYM,))])
    qs.set_fundamentals(SYM, Fundamentals(SYM, "Bitcoin", None, None, None, None, None,
                                          None, None, None, "USD"))
    qs.set_calendar([CalendarEvent(SYM, "dividend", date.today(), {"amount": "1"})])


async def _setup(server, flavor: str) -> str:
    """The portfolio the sweep runs on (neutral names; seeded instruments and fees)."""
    async with _Db(server.db_url) as s:
        await run_seed(s)
        await s.execute(update(Agent).where(Agent.id == server.agent_id).values(name="Orion"))
        main = await s.get(Portfolio, server.demo_id)
        main.name = "Alpha"
        pid = main.id
        if flavor in ("paper_linked", "etoro_linked", "etoro_live_dry_run"):
            from smtrading.brokers.live.account import encrypt_live_config
            from smtrading.brokers.models import BrokerLink

            kind = {"etoro_linked": "etoro_demo",
                    "etoro_live_dry_run": "etoro_live"}.get(flavor, "alpaca_paper")
            if kind == "etoro_live":  # a real-money link in dry run (canary keys)
                link = BrokerLink(
                    name="Conto", kind=kind, status="ok", live_state="dry_run",
                    config_encrypted=encrypt_live_config(ETORO_CANARY),
                    last_account_snapshot={"currency": "USD",
                                           "cash_available": "1000000"},
                    last_snapshot_at=datetime.now(UTC))
            else:
                link = BrokerLink(name="Conto", kind=kind, status="ok")
            s.add(link)
            await s.flush()
            other = await create_portfolio(s, name="Beta", initial_cash=D(100000))
            other.broker_link_id = link.id
            await agents.assign_portfolio(s, server.agent_id, other.id, "trade")
            pid = other.id
        await deposit(s, pid, D(1000), "EUR", author_id="admin")
        await s.commit()
    return str(pid)


DEMO_TEXT = "Instrument history not available in Demo environment"


async def _etoro_source(server, redis_url: str) -> None:
    """The `etoro_source` flavor: quotes and bars of the sweep come from the real
    EtoroProvider through a real MarketDataRouter, on the fake eToro server (never the
    real host). The fake's candles answer a 400 whose text names the environment, so the
    bars tools take the router's miss path with an agent-visible error."""
    import asyncio

    import httpx
    import redis.asyncio as aioredis
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from smtrading.brokers.live.fake import FakeLiveBroker
    from smtrading.connectors.models import ProviderPriority
    from smtrading.connectors.service import update_connector
    from smtrading.instruments.models import AssetClass
    from smtrading.marketdata.router import MarketDataRouter
    from smtrading.marketdata.types import Capability

    async with _Db(server.db_url) as s:
        await update_connector(s, "etoro", enabled=True, config={
            "api_key": "CANARYAPIKEY0001", "user_key": "CANARYUSERKEY-do-not-use"})
        for cap in (Capability.quote, Capability.bars):
            row = await s.get(ProviderPriority, (AssetClass.crypto, cap))
            row.providers = ["etoro"]
        await s.commit()

    fake = FakeLiveBroker(price=D(50000))

    def handler(request: httpx.Request) -> httpx.Response:
        if "/history/candles/" in request.url.path:
            return httpx.Response(400, json={"errorMessage": DEMO_TEXT})
        return fake.transport().handle_request(request)

    routers: dict = {}  # one router per event loop (the MCP server runs its own)

    def router() -> MarketDataRouter:
        loop = asyncio.get_running_loop()
        if loop not in routers:
            sm = async_sessionmaker(make_engine(server.db_url), expire_on_commit=False)
            http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
            redis = aioredis.from_url(redis_url, decode_responses=True)
            routers[loop] = MarketDataRouter(sm, redis, http)
        return routers[loop]

    async def get_quotes(symbols, *, max_age_s=5):
        return await router().get_quotes(symbols, max_age_s=max_age_s)

    async def get_bars(symbol, timeframe, start, end):
        return await router().get_bars(symbol, timeframe, start, end)

    server.qs.get_quotes = get_quotes
    server.qs.get_bars = get_bars
    server.etoro_fake = fake


async def _read_resource(url: str, token: str, uri: str):
    """The resource's JSON content, or the error text."""
    from mcp.client.session import ClientSession
    from mcp.client.streamable_http import streamable_http_client
    from mcp.shared._httpx_utils import create_mcp_http_client
    from mcp.shared.exceptions import MCPError

    async with (
        create_mcp_http_client(headers={"Authorization": f"Bearer {token}"}) as http,
        streamable_http_client(url, http_client=http) as (read, write),
        ClientSession(read, write) as cs,
    ):
        await cs.initialize()
        try:
            result = await cs.read_resource(uri)
        except MCPError as e:
            return str(e)
    return json.loads(result.contents[0].text)


async def _broker_error_on(server, order_id: str) -> None:
    """etoro_linked: the order as a failed send to the eToro demo leaves it, its message
    naming the demo kind and paths (what `agent_view` must turn into the real ones)."""
    from smtrading.brokers.etoro_demo import EtoroDemoAdapter as A
    from smtrading.engine.models import Order, OrderStatus

    async with _Db(server.db_url) as s:
        await s.execute(update(Order).where(Order.id == uuid.UUID(order_id)).values(
            status=OrderStatus.rejected, reject_code="PROVIDER_UNAVAILABLE",
            reject_message=f"{A.kind}: HTTP 503 su {A.EXEC_V2}/orders dopo {A.INFO_V1}/pnl"))
        await s.commit()


async def _admin_placed(server, order_id: str) -> None:
    """The order as a manual order of the portal leaves it (`source` "admin")."""
    from smtrading.engine.models import Order

    async with _Db(server.db_url) as s:
        await s.execute(update(Order).where(Order.id == uuid.UUID(order_id))
                        .values(source="admin"))
        await s.commit()


ADMIN = "admin:ops@example.com"


async def _human_actions(server, call, order: dict, pid: str) -> int:
    """What the humans do in the portal, on resting orders of the agent: a manual cancel
    (`by` "admin"), the kill switch with «annulla ordini aperti» (`by` "admin"), a symbol
    removed from the agent's watchlist (`admin.watch_changed`), then an
    admin's halt on the portfolio (its `set_by` "admin:<email>"; its cancel of the
    openings is the system's) and the agent's attempt to clear it (FORBIDDEN). Returns how
    many resting orders the humans canceled."""
    from smtrading.engine.service import cancel_order, set_kill_switch_and_cancel
    from smtrading.events import notify
    from smtrading.risk import halts

    resting = {**order, "type": "limit", "limit_price": "100"}
    ids = []
    for _ in range(3):
        _, placed = await call("place_order", resting)
        if isinstance(placed, dict) and "id" in placed:
            ids.append(uuid.UUID(placed["id"]))
    async with _Db(server.db_url) as s:
        if ids:
            await cancel_order(s, ids[0], by="admin")
        await set_kill_switch_and_cancel(s, True, cancel_open=True, by="admin")
        await s.commit()
        await set_kill_switch_and_cancel(s, False, cancel_open=False, by="admin")
        await s.commit()
    await call("place_order", resting)  # canceled by the halt
    async with _Db(server.db_url) as s:  # an admin removes a symbol from a watchlist
        await notify(s, portfolio_id=None, agent_id=str(server.agent_id),
                     kind="admin.watch_changed",
                     payload={"action": "remove_symbol", "watchlist": "Crypto",
                              "symbol": SYM})
        await s.execute(update(Agent).where(Agent.id == server.agent_id).values(can_halt=True))
        await halts.set_halt(s, scope="portfolio", portfolio_id=uuid.UUID(pid),
                             reason="Controllo dei conti", by=ADMIN)
        await s.commit()
    await call("set_trading_halt", {"active": False, "scope": "portfolio",
                                    "portfolio_id": pid})
    return len(ids)


def _human_markers(value, path: str = "") -> list[str]:
    """Where a JSON value says a human (the portal's admins) did something."""
    if isinstance(value, dict):
        return [m for k, v in value.items() for m in _human_markers(v, f"{path}.{k}")]
    if isinstance(value, list):
        return [m for i, v in enumerate(value) for m in _human_markers(v, f"{path}[{i}]")]
    if isinstance(value, str) and (value.lower() == "admin" or value.startswith("admin:")
                                   or "admin." in value
                                   or "amministrator" in value.lower()
                                   or "example.com" in value):
        return [f"{path}={value}"]
    return []


async def _dry_run_tick(server, monkeypatch, order_id: str) -> None:
    """etoro_live_dry_run: one BrokerSync pass, as the worker runs it, on the in-memory
    fake behind the live gate: the market buy is intercepted (LiveDryRun) and rejected
    LIVE_DRY_RUN, with its `live.order.dry_run` activity."""
    import os

    from redis.asyncio import Redis
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from smtrading.brokers.live.fake import FakeLiveBroker
    from smtrading.brokers.sync import BrokerSync
    from smtrading.engine.models import Order

    fake = FakeLiveBroker(price=D(50000))
    fake.etoro["real"].credit = D(1000000)
    monkeypatch.setattr("smtrading.brokers.sync._default_inner", fake.transport)
    engine = make_engine(server.db_url)
    redis = Redis.from_url(os.environ["SMT_REDIS_URL"])
    sync = BrokerSync(async_sessionmaker(engine, expire_on_commit=False), redis,
                      server.qs, None)
    try:
        await sync.tick()
        async with AsyncSession(engine) as s:
            order = await s.get(Order, uuid.UUID(order_id))
    finally:
        await sync.aclose()
        await redis.aclose()
        await engine.dispose()
    assert (order.reject_code, order.reject_message) == (
        "LIVE_DRY_RUN", "prova a secco: ordine non inviato"), order.reject_message
    assert all("/demo/" not in path for _, path in fake.calls), list(fake.calls)
    writes = [path for method, path in fake.calls if method != "GET"]
    assert all(path.endswith("/eligibility") for path in writes), writes  # reads only


@pytest.mark.parametrize("flavor", ["simulated", "paper_linked", "etoro_source",
                                    "etoro_linked", "etoro_live_dry_run"])
async def test_no_simulation_fingerprint_reaches_agents(mcp_server, mcp_call, flavor,
                                                        monkeypatch, redis_url):
    _market(mcp_server.qs)
    pid = await _setup(mcp_server, flavor)
    if flavor == "etoro_source":
        await _etoro_source(mcp_server, redis_url)
    outputs: list[tuple[str, object]] = []

    async def call(tool, args=None):
        is_error, payload = await mcp_call(mcp_server.url, mcp_server.token, tool, args or {})
        outputs.append((tool, payload))
        return is_error, payload

    order = {"portfolio_id": pid, "symbol": SYM, "side": "buy", "qty": "0.1"}
    _, filled = await call("place_order", {**order, "type": "market"})
    _, resting = await call("place_order", {**order, "type": "limit", "limit_price": "100"})
    await call("place_order", {**order, "type": "market", "qty": "1000"})  # rejected
    if flavor == "etoro_linked":
        assert isinstance(filled, dict) and "id" in filled, filled
        await _broker_error_on(mcp_server, filled["id"])
    if flavor == "etoro_live_dry_run":
        assert isinstance(filled, dict) and "id" in filled, filled
        await _dry_run_tick(mcp_server, monkeypatch, filled["id"])
    await call("preview_order", {**order, "type": "market"})
    if isinstance(resting, dict) and "id" in resting:
        await call("cancel_order", {"order_id": resting["id"]})
        await _admin_placed(mcp_server, resting["id"])  # reads like an agent's order
    canceled_by_humans = await _human_actions(mcp_server, call, order, pid)
    await call("create_watchlist", {"name": "Crypto"})
    await call("create_alert", {"symbol": SYM, "condition": "price_above", "value": "60000"})
    await call("log_decision", {"kind": "strategy", "summary": "Compro BTC",
                                "portfolio_id": pid, "symbols": [SYM], "outcome": "approvata",
                                "subject": "Acquisto BTC", "reason": "trend positivo"})

    for tool, args in [
        ("list_portfolios", {}),
        ("get_portfolio", {"portfolio_id": pid}),
        ("get_positions", {"portfolio_id": pid}),
        ("get_transactions", {"portfolio_id": pid}),
        ("get_performance", {"portfolio_id": pid}),
        ("get_risk_limits", {"portfolio_id": pid}),
        ("list_orders", {"portfolio_id": pid}),
        ("get_order", {"order_id": filled.get("id") if isinstance(filled, dict) else None}),
        ("get_decisions", {}),
        ("get_activity_feed", {}),
        ("get_activity_feed", {"after_id": 0}),
        ("get_triggered_alerts", {}),
        ("get_trading_halt", {}),
        ("list_watchlists", {}),
        ("list_alerts", {}),
        ("get_quote", {"symbols": [SYM]}),
        ("get_instrument", {"symbol": SYM}),
        ("search_instruments", {"query": "BTC", "include_provider": False}),
        ("get_market_status", {}),
        ("get_fx_rate", {"base": "EUR", "quote": "USD"}),
        ("get_bars", {"symbol": SYM, "timeframe": "1d", "limit": 10}),
        ("get_indicators", {"symbol": SYM, "timeframe": "1d",
                            "indicators": [{"name": "sma", "params": {"period": 5}}]}),
        ("get_indicators", {"symbol": SYM, "timeframe": "1d", "include_forming": True,
                            "indicators": [{"name": "sma", "params": {"period": 5}}]}),
        ("get_indicators", {"symbol": SYM, "timeframe": "1h", "include_forming": True,
                            "indicators": [{"name": "sma", "params": {"period": 5}}]}),
        ("get_news", {"symbols": [SYM]}),
        ("get_fundamentals", {"symbol": SYM}),
        ("get_dividends", {"symbol": SYM}),
        ("get_earnings_calendar", {"from_date": str(date.today()),
                                   "to_date": str(date.today() + timedelta(days=30))}),
        # errors
        ("get_order", {"order_id": "00000000-0000-0000-0000-000000000000"}),
        ("place_order", {**order, "type": "market", "symbol": "NOPE-XYZ"}),
        ("close_position", {"portfolio_id": pid, "symbol": "ETH-USD"}),
    ]:
        await call(tool, args)

    for uri in (f"portfolio://{pid}", "fees://schedule", "markets://hours"):
        res = await _read_resource(mcp_server.url, mcp_server.token, uri)
        outputs.append((uri, res))

    # the sweep saw the facts it is about
    feed = next(p for t, p in outputs if t == "get_activity_feed")
    assert {i["type"] for i in feed["items"]} >= {"portfolio.cash", "decision.strategy",
                                                   "kill_switch", "halt.set"}
    if canceled_by_humans:  # eToro takes market orders only: nothing rests there
        assert {i["type"] for i in feed["items"]} & {"order.canceled",
                                                      "order.cancel_requested"}
    halt = next(p for t, p in outputs if t == "get_trading_halt")
    assert halt["halted"] and halt["halts"][0]["set_by"] == "system", halt
    alerts = next(p for t, p in outputs if t == "get_triggered_alerts")
    assert "watch_changed" in {a["kind"] for a in alerts["alerts"]}, alerts
    clear = next(p for t, p in outputs if t == "set_trading_halt")
    assert clear["code"] == "FORBIDDEN", clear
    markers = [(t, m) for t, p in outputs for m in _human_markers(p)]
    assert not markers, markers
    if flavor == "etoro_source":  # the real provider answered, on the fake server only
        quote = next(p for t, p in outputs if t == "get_quote")
        assert "etoro" in json.dumps(quote), quote
        assert any("/market-data/instruments/rates" in p for _, p in mcp_server.etoro_fake.calls)
        bars = next(p for t, p in outputs if t == "get_bars")
        assert "etoro" in json.dumps(bars) and "HTTP 400" in json.dumps(bars), bars
        assert all("Demo environment" not in json.dumps(p) for _, p in outputs)
    if flavor in ("simulated", "etoro_source"):
        assert filled["status"] == "filled", filled
        assert "order.filled" in {i["type"] for i in feed["items"]}
        got = next(p for t, p in outputs if t == "get_order" and "fills" in p)
        assert got["fills"], got

    if flavor == "etoro_linked":  # the demo texts were there, and came out as the real ones
        got = next(p for t, p in outputs if t == "get_order" and "fills" in p)
        assert got["reject_message"].startswith("etoro_live: HTTP 503 su /api/v2/"), got
    if flavor == "etoro_live_dry_run":  # the dry run reads as a plain broker refusal
        got = next(p for t, p in outputs if t == "get_order" and "fills" in p)
        assert got["status"] == "rejected", got
        assert (got["reject_code"], got["reject_message"]) == (
            "PROVIDER_UNAVAILABLE", "rejected by the broker"), got
        assert "live.order.rejected" in {i["type"] for i in feed["items"]}
        assert "live.order.dry_run" not in {i["type"] for i in feed["items"]}
    for tool, payload in outputs:
        text = json.dumps(payload, ensure_ascii=False)
        assert "etoro_demo" not in text and "/demo/" not in text, (tool, text[:300])
        assert '"source": "admin"' not in text, (tool, text[:300])
        if flavor == "etoro_live_dry_run":
            assert "etoro_live" not in text, (tool, text[:300])
            assert all(c not in text for c in ETORO_CANARY.values()), tool

    leaks = []
    for tool, payload in outputs:
        text = json.dumps(payload, ensure_ascii=False)
        for m in FINGERPRINT.finditer(text):
            leaks.append((tool, text[max(0, m.start() - 60): m.end() + 40]))
        if "/demo/" in text:
            leaks.append((tool, "/demo/"))
    assert not leaks, "\n".join(f"{t}: …{ctx}…" for t, ctx in leaks)
