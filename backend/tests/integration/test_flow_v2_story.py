"""«Fiume dei flussi» v2: the story of an order (decision linked or not, the MCP call,
send, children, fills with expected and filled price, Telegram category and time only,
outcome) and of a refused MCP call without an order."""
# ruff: noqa: F811  (the `agent` and `symbol` fixtures of test_flow_events, imported then requested)

from datetime import timedelta
from decimal import Decimal as D

from test_flow_events import (  # noqa: F401  (agent, symbol: fixtures)
    S,
    T,
    _fill,
    _link,
    _mcp,
    _order,
    _portfolio,
    agent,
    symbol,
)

from smtrading.activity.models import Activity, Decision
from smtrading.engine.models import Order, OrderSide, OrderStatus, OrderType
from smtrading.flow.story import NO_DECISION, mcp_story, order_story
from smtrading.telegram.models import TelegramOutbox


async def _decision(db_session, agent, p):
    d = Decision(agent_id=agent.id, portfolio_id=p.id, kind="proposal",
                 summary="Compro su rottura dei massimi", reason="volumi in aumento",
                 confidence=70, expected={"return_pct": 2.5, "horizon_days": 5},
                 details={"segreto": "non mostrare"}, created_at=T - 60 * S)
    db_session.add(d)
    await db_session.flush()
    return d


async def test_story_of_a_linked_order(db_session, agent, symbol):
    link = await _link(db_session)
    p = await _portfolio(db_session, agent, link=link)
    d = await _decision(db_session, agent, p)
    o = await _order(db_session, p, symbol, link=link, sent_at=T + S,
                     status=OrderStatus.filled)
    o.agent_id, o.decision_id, o.created_at = str(agent.id), d.id, T
    o.type, o.limit_price = OrderType.limit, D("100.5")
    child = Order(portfolio_id=p.id, symbol=symbol, side=OrderSide.sell,
                  type=OrderType.stop, qty=D(1), stop_price=D(95), status=OrderStatus.held,
                  parent_id=o.id, role="stop_loss", created_at=T)
    db_session.add(child)
    await db_session.flush()
    call = await _mcp(db_session, agent, "place_bracket_order", at=T + S // 2, latency=800,
                      params={"symbol": symbol, "qty": "1", "limit_price": "100.5"})
    f = await _fill(db_session, o, at=T + 5 * S, source="broker")
    f.quote_ask, f.quote_bid = D("100.4"), D("100.2")
    tg = TelegramOutbox(source="system", category="event", level="info",
                        text="SEGRETO del messaggio", status="sent", sent_at=T + 6 * S)
    db_session.add(tg)
    await db_session.flush()
    db_session.add(Activity(type="order.filled", level="info", portfolio_id=p.id,
                            summary="eseguito", data={"order_id": str(o.id)},
                            telegram="sent", outbox_id=tg.id, ts=T + 5 * S))
    await db_session.flush()

    s = await order_story(db_session, o.id)
    assert s is not None
    assert s["order"]["id"] == o.id and s["order"]["symbol"] == symbol
    assert s["order"]["status"] == "filled" and s["order"]["status_it"] == "eseguito"
    assert s["order"]["broker"] == {"kind": "alpaca_paper", "label": "Alpaca (prova)",
                                    "node": "broker:alpaca_paper"}
    assert s["agent"] == {"key": f"agent:{agent.id}", "name": agent.name}
    assert s["decision"] == {"id": d.id, "kind": "proposal",
                             "summary": "Compro su rottura dei massimi",
                             "reason": "volumi in aumento", "confidence": 70,
                             "expected": {"return_pct": 2.5, "horizon_days": 5},
                             "at": T - 60 * S}
    assert s["decision_note"] is None
    assert s["mcp"] == {"audit_id": call.id, "tool": "place_bracket_order", "outcome": "ok",
                        "reason_code": None, "latency_ms": 800, "symbol": symbol,
                        "at": T + S // 2 - timedelta(milliseconds=800)}
    assert s["sent"] == {"at": T + S, "broker": "Alpaca (prova)"}
    assert s["rejected"] is None
    [c] = s["children"]
    assert c["id"] == child.id and c["role"] == "stop_loss" and c["role_it"] == "stop"
    assert c["stop_price"] == D(95) and c["status_it"] == "in attesa del padre"
    [fl] = s["fills"]
    assert fl["price"] == D(100) and fl["expected_price"] == D("100.5")
    assert fl["expected_from"] == "limite" and fl["source"] == "broker"
    assert s["telegram"] == [{"category": "event", "category_it": "avviso", "status": "sent",
                              "at": T + 6 * S}]
    assert s["outcome"]["status"] == "filled"
    steps = [x["step"] for x in s["steps"]]
    assert steps == ["decision", "mcp", "created", "child", "sent", "fill", "telegram"]
    assert [x["at"] for x in s["steps"]] == sorted(x["at"] for x in s["steps"])
    text = repr(s)
    assert "SEGRETO" not in text and "segreto" not in text and "limit_price" not in repr(s["mcp"])


async def test_story_of_an_order_without_decision(db_session, agent, symbol):
    p = await _portfolio(db_session, agent)
    o = await _order(db_session, p, symbol, status=OrderStatus.filled)
    o.created_at = T
    await db_session.flush()
    f = await _fill(db_session, o, at=T + S)
    f.quote_ask, f.quote_bid = D(101), D(99)
    await db_session.flush()
    s = await order_story(db_session, o.id)
    assert s["decision"] is None and s["decision_note"] == NO_DECISION == "decisione non collegata"
    assert s["order"]["broker"] is None and s["sent"] is None
    assert s["mcp"] is None and s["mcp_note"]
    [fl] = s["fills"]
    assert fl["expected_price"] == D(101) and fl["expected_from"] == "quotazione"
    assert fl["source"] == "sim"


async def test_story_of_a_rejected_order(db_session, agent, symbol):
    p = await _portfolio(db_session, agent)
    o = await _order(db_session, p, symbol, status=OrderStatus.rejected)
    o.created_at, o.reject_code, o.reject_message = T, "RISK_LIMIT", "oltre il limite"
    o.updated_at = T + S
    await db_session.flush()
    s = await order_story(db_session, o.id)
    assert s["rejected"] == {"at": T + S, "code": "RISK_LIMIT", "message": "oltre il limite"}
    assert s["steps"][-1]["step"] == "rejected"


async def test_hidden_or_unknown_order_has_no_story(db_session, agent, symbol):
    import uuid
    hidden = await _portfolio(db_session, None, in_totals=False)
    o = await _order(db_session, hidden, symbol)
    assert await order_story(db_session, o.id) is None
    assert await order_story(db_session, uuid.uuid4()) is None


async def test_story_of_a_refused_mcp_call(db_session, agent):
    row = await _mcp(db_session, agent, "place_order", outcome="INVALID_ORDER", latency=40,
                     params={"symbol": "AAPL", "qty": "5", "note": "segreto"})
    s = await mcp_story(db_session, row.id)
    assert s == {"audit_id": row.id, "at": T - timedelta(milliseconds=40),
                 "tool": "place_order", "agent": {"key": f"agent:{agent.id}", "name": agent.name},
                 "outcome": "error", "reason_code": "INVALID_ORDER", "latency_ms": 40,
                 "symbol": "AAPL", "path": [f"agent:{agent.id}", "pc", "mcp", "eng"],
                 "note": "chiamata rifiutata: nessun ordine creato", "order_id": None}
    assert "segreto" not in repr(s) and "qty" not in repr(s)
    ok = await _mcp(db_session, agent, "get_quote", params={"symbols": ["MSFT"]})
    s = await mcp_story(db_session, ok.id)
    assert s["outcome"] == "ok" and s["reason_code"] is None and s["note"] is None
    admin = await _mcp(db_session, agent, "admin:order.place")
    assert await mcp_story(db_session, admin.id) is None
    assert await mcp_story(db_session, 2**62) is None


async def test_mcp_story_links_its_order(db_session, agent, symbol):
    p = await _portfolio(db_session, agent)
    o = await _order(db_session, p, symbol)
    o.agent_id, o.created_at = str(agent.id), T
    await db_session.flush()
    call = await _mcp(db_session, agent, "place_order", at=T + S // 2, latency=800,
                      params={"symbol": symbol})
    other = await _mcp(db_session, agent, "place_order", at=T + 60 * S, latency=100,
                       params={"symbol": symbol})
    refused = await _mcp(db_session, agent, "place_order", at=T + S // 2, latency=800,
                         outcome="INVALID_ORDER", params={"symbol": symbol})
    assert (await mcp_story(db_session, call.id))["order_id"] == str(o.id)
    assert (await mcp_story(db_session, other.id))["order_id"] is None
    assert (await mcp_story(db_session, refused.id))["order_id"] is None
    quote = await _mcp(db_session, agent, "get_quote", at=T + S // 2, latency=800,
                       params={"symbol": symbol})
    assert (await mcp_story(db_session, quote.id))["order_id"] is None


async def test_fill_events_carry_their_order(db_session, agent, symbol):
    from smtrading.flow.events import flow_events
    p = await _portfolio(db_session, agent)
    o = await _order(db_session, p, symbol)
    f = await _fill(db_session, o, at=T)
    [e] = [x for x in await flow_events(db_session, after=None, limit=500, now=T + S)
           if x["id"] == f"fill:{f.id}"]
    assert e["order_id"] == str(o.id) and e["path"] == ["eng", "sim"]
