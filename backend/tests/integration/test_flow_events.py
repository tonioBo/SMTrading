"""«Fiume dei flussi» events: every source translated into a path, the portfolio filter,
the time cursor with its overlap, the limit, and the summary built on the same events.

Each test works in its own rolled-back transaction, at a time far from any committed row
(T), and passes `now` explicitly."""

import hashlib
import random
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal as D

import pytest

from smtrading.agents.models import Agent, AgentPortfolio, McpAuditLog
from smtrading.autorules.tables import AutoRule, AutoTrade
from smtrading.brokers.models import BrokerLink
from smtrading.engine.models import Fill, Order, OrderSide, OrderStatus, OrderType
from smtrading.flow.events import flow_events
from smtrading.flow.summary import flow_summary
from smtrading.instruments.models import AssetClass, Instrument
from smtrading.ledger.models import Journal, JournalType
from smtrading.portfolios.models import Portfolio
from smtrading.telegram.models import TelegramOutbox

T = datetime(2031, 3, 4, 15, 0, tzinfo=UTC)
S = timedelta(seconds=1)
TZ = "Europe/Rome"


def ids(events):
    return [e["id"] for e in events]


@pytest.fixture
async def agent(db_session):
    a = Agent(name=f"Flow {uuid.uuid4().hex[:8]}", token_prefix=uuid.uuid4().hex[:8],
              token_hash="x")
    db_session.add(a)
    await db_session.flush()
    return a


@pytest.fixture
async def symbol(db_session):
    sym = f"FL{uuid.uuid4().hex[:6].upper()}"
    db_session.add(Instrument(symbol=sym, name=sym, asset_class=AssetClass.us_equity,
                              market="US", currency="USD"))
    await db_session.flush()
    return sym


async def _portfolio(db_session, agent=None, *, in_totals=True, link=None):
    p = Portfolio(name=f"F-{uuid.uuid4().hex[:8]}", in_totals=in_totals,
                  broker_link_id=link.id if link else None)
    db_session.add(p)
    await db_session.flush()
    if agent is not None:
        db_session.add(AgentPortfolio(agent_id=agent.id, portfolio_id=p.id, permission="trade"))
        await db_session.flush()
    return p


async def _link(db_session, kind="alpaca_paper"):
    link = BrokerLink(name=f"L-{uuid.uuid4().hex[:8]}", kind=kind)
    db_session.add(link)
    await db_session.flush()
    return link


async def _mcp(db_session, agent, method="get_forecast", *, at=T, outcome="ok",
               latency=900, params=None):
    row = McpAuditLog(agent_id=agent.id if agent else None, method=method,
                      params=params if params is not None else {}, outcome=outcome,
                      latency_ms=latency, created_at=at)
    db_session.add(row)
    await db_session.flush()
    return row


async def _order(db_session, p, symbol, *, link=None, sent_at=None,
                 status=OrderStatus.new, updated_at=None):
    o = Order(portfolio_id=p.id, symbol=symbol, side=OrderSide.buy, type=OrderType.market,
              qty=D(1), status=status, broker_link_id=link.id if link else None,
              broker_sent_at=sent_at)
    if updated_at is not None:
        o.updated_at = updated_at
    db_session.add(o)
    await db_session.flush()
    return o


async def _fill(db_session, o, *, at, source="sim"):
    h = hashlib.sha256(uuid.uuid4().bytes).hexdigest()
    j = Journal(type=JournalType.TRADE, author_type="system", prev_hash=h,
                hash=hashlib.sha256(h.encode()).hexdigest())
    db_session.add(j)
    await db_session.flush()
    f = Fill(order_id=o.id, portfolio_id=o.portfolio_id, symbol=o.symbol, side=o.side,
             qty=D(1), price=D(100), fee_currency="USD", journal_id=j.id, created_at=at,
             source=source)
    db_session.add(f)
    await db_session.flush()
    return f


async def test_mcp_call_becomes_a_path(db_session, agent):
    row = await _mcp(db_session, agent, params={"symbol": "SPY", "horizon": 5})
    events = await flow_events(db_session, after=None, limit=500, now=T + 5 * S)
    assert len(events) == 1
    e = events[0]
    assert e["id"] == f"mcp:{row.id}" and e["kind"] == "mcp"
    assert e["who"] == f"agent:{agent.id}"
    assert e["path"] == [f"agent:{agent.id}", "pc", "mcp", "ng", "mcp", "pc"]
    assert e["hops"] == ["get_forecast"] * 3 + ["risposta"] * 2
    assert e["symbol"] == "SPY" and e["outcome"] == "ok" and e["latency_ms"] == 900
    assert e["ts"] == T - timedelta(milliseconds=900)
    assert set(e) == {"id", "ts", "kind", "who", "path", "hops", "outcome", "latency_ms",
                      "symbol"}
    assert "horizon" not in repr(e)


async def test_mcp_paths_by_destination(db_session, agent):
    a = f"agent:{agent.id}"
    await _mcp(db_session, agent, "place_order", at=T, params={"symbol": "AAPL"})
    await _mcp(db_session, agent, "send_telegram", at=T + S, params={"text": "segreto"})
    await _mcp(db_session, agent, "get_quote", at=T + 2 * S, params={"symbols": ["MSFT", "X"]})
    await _mcp(db_session, agent, "unknown_tool", at=T + 3 * S)
    events = await flow_events(db_session, after=None, limit=500, now=T + 5 * S)
    by_method = {e["hops"][0]: e for e in events}
    assert by_method["place_order"]["path"] == [a, "pc", "mcp", "eng"]
    assert by_method["send_telegram"]["path"] == [a, "pc", "mcp", "tg"]
    assert by_method["send_telegram"]["symbol"] is None
    assert "segreto" not in repr(events)
    assert by_method["get_quote"]["path"] == [a, "pc", "mcp", "px", "mcp", "pc"]
    assert by_method["get_quote"]["symbol"] == "MSFT"
    assert by_method["unknown_tool"]["path"] == [a, "pc", "mcp", "pc"]
    assert by_method["unknown_tool"]["hops"] == ["unknown_tool", "unknown_tool", "risposta"]
    for e in events:
        assert len(e["hops"]) == len(e["path"]) - 1


async def test_admin_rows_are_not_events(db_session, agent):
    await _mcp(db_session, agent, "admin:order.place")
    await _mcp(db_session, None, "admin:agent.notify")
    await _mcp(db_session, None, "get_quote")  # no agent: not an agent's call
    assert await flow_events(db_session, after=None, limit=500, now=T + 5 * S) == []


async def test_error_outcome_marks_the_event(db_session, agent):
    await _mcp(db_session, agent, "place_order", outcome="INVALID_ORDER")
    [e] = await flow_events(db_session, after=None, limit=500, now=T + 5 * S)
    assert e["outcome"] == "error"


async def test_long_call_written_after_the_cursor_is_not_lost(db_session, agent):
    """A call started before the cursor's overlap but written after it still comes."""
    row = await _mcp(db_session, agent, "run_backtest", at=T + 60 * S, latency=50_000)
    events = await flow_events(db_session, after=T + 55 * S, limit=500, now=T + 62 * S)
    assert ids(events) == [f"mcp:{row.id}"]
    assert events[0]["ts"] == T + 10 * S


async def test_order_sent_then_filled_across_two_polls(db_session, agent, symbol):
    link = await _link(db_session)
    p = await _portfolio(db_session, agent, link=link)
    o = await _order(db_session, p, symbol, link=link, sent_at=T, status=OrderStatus.new)

    first = await flow_events(db_session, after=None, limit=500, now=T + 2 * S)
    assert ids(first) == [f"order:{o.id}:sent"]
    e = first[0]
    assert e["path"] == ["eng", "sync", "broker:alpaca_paper"]
    assert e["hops"] == ["invio", f"ordine {symbol}"]
    assert e["who"] == "eng" and e["kind"] == "order_sent" and e["symbol"] == symbol
    cursor = max(x["ts"] for x in first)

    f = await _fill(db_session, o, at=T + 120 * S, source="broker")
    o.status = OrderStatus.filled
    await db_session.flush()
    second = await flow_events(db_session, after=cursor, limit=500, now=T + 122 * S)
    assert len(ids(second)) == len(set(ids(second)))
    assert ids(second).count(f"fill:{f.id}") == 1
    fe = next(x for x in second if x["id"] == f"fill:{f.id}")
    assert fe["path"] == ["broker:alpaca_paper", "sync", "eng"]
    assert fe["hops"] == [f"eseguito {symbol}", "registrazione"]
    # the two polls together: each event once, in time order
    seen = {}
    for x in first + second:
        seen.setdefault(x["id"], x)
    assert list(seen) == [f"order:{o.id}:sent", f"fill:{f.id}"]

    third = await flow_events(db_session, after=T + 121 * S, limit=500, now=T + 130 * S)
    assert ids(third) == [f"fill:{f.id}"]  # the order is long past the overlap


async def test_hidden_portfolio_orders_are_hidden_but_agent_calls_show(db_session, agent,
                                                                       symbol):
    link = await _link(db_session)
    hidden = await _portfolio(db_session, None, in_totals=False, link=link)
    archived = await _portfolio(db_session, agent, link=link)
    archived.status = "archived"
    await db_session.flush()
    for p in (hidden, archived):
        o = await _order(db_session, p, symbol, link=link, sent_at=T)
        await _fill(db_session, o, at=T + S, source="broker")
        await _order(db_session, p, symbol, status=OrderStatus.rejected, updated_at=T + S)
    rule = AutoRule(portfolio_id=hidden.id, symbol=symbol, model="breakout")
    db_session.add(rule)
    await db_session.flush()
    db_session.add(AutoTrade(rule_id=rule.id, rule_version=1, portfolio_id=hidden.id,
                             symbol=symbol, trigger_key="k", trigger_at=T + S,
                             trigger_price=D(1), trigger_values={}, expected_price=D(1),
                             status="sent"))
    row = await _mcp(db_session, agent, "place_order", at=T + 2 * S,
                     params={"portfolio_id": str(hidden.id), "symbol": symbol})
    events = await flow_events(db_session, after=None, limit=500, now=T + 5 * S)
    assert ids(events) == [f"mcp:{row.id}"]


async def test_rejections(db_session, agent, symbol):
    link = await _link(db_session, "etoro_demo")
    p = await _portfolio(db_session, agent, link=link)
    by_broker = await _order(db_session, p, symbol, link=link, sent_at=T,
                             status=OrderStatus.rejected, updated_at=T + S)
    by_engine = await _order(db_session, p, symbol, status=OrderStatus.rejected,
                             updated_at=T + 2 * S)
    events = {e["id"]: e for e in await flow_events(db_session, after=None, limit=500,
                                                    now=T + 5 * S)}
    e1 = events[f"order:{by_broker.id}:rejected"]
    assert e1["path"] == ["eng", "sync", "broker:etoro_demo"] and e1["outcome"] == "error"
    assert e1["ts"] == T + S and e1["kind"] == "order_rejected"
    e2 = events[f"order:{by_engine.id}:rejected"]
    assert e2["path"] == ["mcp", "eng"] and e2["hops"] == ["rifiutato"]
    assert e2["outcome"] == "error"
    assert f"order:{by_broker.id}:sent" in events
    assert f"order:{by_engine.id}:sent" not in events


async def test_telegram_agent_rows_not_counted_twice(db_session, agent):
    def row(source, status="sent", category="event"):
        return TelegramOutbox(source=source, category=category, level="info", text="t",
                              status=status, sent_at=T + S, created_at=T)
    mine = row("system")
    db_session.add_all([row(f"agent:{agent.id}", category="agent"), mine,
                        row("system", status="pending"), row("admin:x", category="test")])
    await db_session.flush()
    events = await flow_events(db_session, after=None, limit=500, now=T + 5 * S)
    assert len(events) == 2
    e = next(x for x in events if x["id"] == f"tg:{mine.id}")
    assert e["path"] == ["eng", "tg"] and e["hops"] == ["avviso"] and e["who"] == "eng"
    assert e["kind"] == "telegram" and e["ts"] == T + S


async def test_simulated_fill_goes_to_sim(db_session, agent, symbol):
    p = await _portfolio(db_session, agent)
    o = await _order(db_session, p, symbol, status=OrderStatus.filled)
    f = await _fill(db_session, o, at=T)
    events = await flow_events(db_session, after=None, limit=500, now=T + 5 * S)
    assert ids(events) == [f"fill:{f.id}"]
    assert events[0]["path"] == ["eng", "sim"]
    assert events[0]["hops"] == [f"esecuzione simulata {symbol}"]


async def test_auto_trade_becomes_a_path(db_session, agent, symbol):
    p = await _portfolio(db_session, agent)
    rule = AutoRule(portfolio_id=p.id, symbol=symbol, model="breakout")
    db_session.add(rule)
    await db_session.flush()
    t = AutoTrade(rule_id=rule.id, rule_version=1, portfolio_id=p.id, symbol=symbol,
                  trigger_key="k1", trigger_at=T, trigger_price=D(1), trigger_values={},
                  expected_price=D(1), status="sent")
    db_session.add(t)
    await db_session.flush()
    [e] = await flow_events(db_session, after=None, limit=500, now=T + 5 * S)
    assert e["id"] == f"auto:{t.id}" and e["who"] == "rules" and e["kind"] == "auto"
    assert e["path"] == ["px", "rules", "eng"] and e["hops"] == [f"scatto {symbol}", "entrata"]


async def test_limit_and_order(db_session, agent):
    rows = list(range(30))
    random.shuffle(rows)
    made = {}
    for i in rows:
        made[i] = await _mcp(db_session, agent, "get_quote", at=T + (20 + i) * S, latency=0)
    events = await flow_events(db_session, after=T + 30 * S, limit=10, now=T + 60 * S)
    # the cursor re-reads 10 s: rows after T + 20 s, the 10 oldest, sorted
    assert ids(events) == [f"mcp:{made[i].id}" for i in range(1, 11)]
    assert [e["ts"] for e in events] == sorted(e["ts"] for e in events)


async def test_nothing_after_now_nor_before_the_first_window(db_session, agent):
    await _mcp(db_session, agent, at=T - 16 * 60 * S, latency=0)
    await _mcp(db_session, agent, at=T + 10 * S, latency=0)
    assert await flow_events(db_session, after=None, limit=500, now=T) == []


async def test_summary(db_session, agent, symbol):
    a = f"agent:{agent.id}"
    await _mcp(db_session, agent, "get_quote", at=T - 50 * S, latency=100)
    await _mcp(db_session, agent, "get_quote", at=T - 40 * S, latency=300)
    await _mcp(db_session, agent, "place_order", at=T - 30 * S, latency=200,
               outcome="INVALID_ORDER")
    await _mcp(db_session, agent, "get_quote", at=T - 10 * 60 * S, latency=100)  # outside 1 min
    p = await _portfolio(db_session, agent)
    o = await _order(db_session, p, symbol, status=OrderStatus.filled)
    o.created_at = T - 20 * S
    await db_session.flush()
    await _fill(db_session, o, at=T - 20 * S)

    out = await flow_summary(db_session, window_min=1, now=T, tz=TZ)
    links = out["links"]
    assert links["pc|mcp"]["calls"] == 3 and links["pc|mcp"]["errors"] == 1
    assert links[f"{a}|pc"]["calls"] == 3
    assert links["mcp|px"] == {"calls": 2, "errors": 0,
                               "top_tools": [{"tool": "get_quote", "calls": 2}]}
    assert links["mcp|eng"]["errors"] == 1
    assert links["pc|mcp"]["top_tools"][0] == {"tool": "get_quote", "calls": 2}
    assert links["eng|sim"]["calls"] == 1
    assert out["nodes"]["mcp"] == 3.0 and out["nodes"]["sim"] == 1.0
    head = out["head"]
    assert head["calls_per_min"] == 3.0
    assert head["errors_window"] == 1
    assert head["mcp_latency_ms"] == 200
    assert head["orders_today"] == 1

    five = await flow_summary(db_session, window_min=5, now=T, tz=TZ)
    assert five["head"]["calls_per_min"] == 0.6


async def test_summary_empty(db_session):
    out = await flow_summary(db_session, window_min=15, now=T + 3650 * 86400 * S, tz=TZ)
    assert out["links"] == {} and out["nodes"] == {}
    assert out["head"]["calls_per_min"] == 0 and out["head"]["errors_window"] == 0
    assert out["head"]["mcp_latency_ms"] is None
