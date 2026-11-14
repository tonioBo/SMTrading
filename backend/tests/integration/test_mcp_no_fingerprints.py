"""Agents connected via MCP must believe everything is real (mcp_hide_mode on, the seeded
default): no agent-facing output or error may carry a simulation fingerprint.

One sweep per portfolio flavor: orders, fills, a rejection, a cancel, a deposit, an
alert, a watchlist and a decision on the portfolio, then every agent-facing read tool,
the resources and a few errors are called and the JSON they return is scanned for the
words that would give the simulation away (`FINGERPRINT`).

Allowlist (documented, not scanned):
- the backtest tools (run_backtest / get_backtest / list_backtests): a backtest is a
  simulation by definition and says so; none of them is called here.
- names an admin chose (portfolio, agent, risk profile names): the fixture renames the
  seeded "Demo" portfolio and "test-agent" agent to neutral names, since the words would
  be false positives of the data, not of the code.
- market data provider names come from the real providers in production; the fixture's
  fake source is given a real provider name ("yahoo").
"""

import dataclasses
import json
import re
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from smtrading.agents import service as agents
from smtrading.agents.models import Agent
from smtrading.common.db import make_engine
from smtrading.marketdata.types import Bar, CalendarEvent, Fundamentals, NewsItem
from smtrading.portfolios.models import Portfolio
from smtrading.portfolios.service import create_portfolio, deposit
from smtrading.seed import run_seed

D = Decimal
SYM = "BTC-USD"
NOW = datetime.now(UTC).replace(microsecond=0)

FINGERPRINT = re.compile(
    r"\bsim\b|simul|paper|demo|\bdry|\bprova\b|\btest|fake|mock|sandbox|virtual",
    re.IGNORECASE,
)


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
        if flavor == "paper_linked":
            from smtrading.brokers.models import BrokerLink

            link = BrokerLink(name="Conto", kind="alpaca_paper", status="ok")
            s.add(link)
            await s.flush()
            other = await create_portfolio(s, name="Beta", initial_cash=D(100000))
            other.broker_link_id = link.id
            await agents.assign_portfolio(s, server.agent_id, other.id, "trade")
            pid = other.id
        await deposit(s, pid, D(1000), "EUR", author_id="admin")
        await s.commit()
    return str(pid)


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


@pytest.mark.parametrize("flavor", ["simulated", "paper_linked"])
async def test_no_simulation_fingerprint_reaches_agents(mcp_server, mcp_call, flavor):
    _market(mcp_server.qs)
    pid = await _setup(mcp_server, flavor)
    outputs: list[tuple[str, object]] = []

    async def call(tool, args=None):
        is_error, payload = await mcp_call(mcp_server.url, mcp_server.token, tool, args or {})
        outputs.append((tool, payload))
        return is_error, payload

    order = {"portfolio_id": pid, "symbol": SYM, "side": "buy", "qty": "0.1"}
    _, filled = await call("place_order", {**order, "type": "market"})
    _, resting = await call("place_order", {**order, "type": "limit", "limit_price": "100"})
    await call("place_order", {**order, "type": "market", "qty": "1000"})  # rejected
    await call("preview_order", {**order, "type": "market"})
    if isinstance(resting, dict) and "id" in resting:
        await call("cancel_order", {"order_id": resting["id"]})
    await call("create_watchlist", {"name": "Crypto"})
    await call("create_alert", {"symbol": SYM, "condition": "price_above", "value": "60000"})
    await call("log_decision", {"kind": "strategy", "summary": "Compro BTC",
                                "portfolio_id": pid, "symbols": [SYM]})

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
    assert {i["type"] for i in feed["items"]} >= {"portfolio.cash", "decision.strategy"}
    if flavor == "simulated":
        assert filled["status"] == "filled", filled
        assert "order.filled" in {i["type"] for i in feed["items"]}
        got = next(p for t, p in outputs if t == "get_order" and "fills" in p)
        assert got["fills"], got

    leaks = []
    for tool, payload in outputs:
        text = json.dumps(payload, ensure_ascii=False)
        for m in FINGERPRINT.finditer(text):
            leaks.append((tool, text[max(0, m.start() - 60): m.end() + 40]))
    assert not leaks, "\n".join(f"{t}: …{ctx}…" for t, ctx in leaks)
