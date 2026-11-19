"""«Diario operazioni»: one row per trade (the «Strategie» FIFO trades) and its timeline,
from real sim orders shaped like production: a bracket entry stopped out, an entry
protected later by an OCO («PROTEGGI»), closed in part by the agents (CHIUDI on the
strategy's exit rule) then at the target, a plan close by SMTrading, a portal order still
open; EUR portfolio, USD instruments (FX fees). A viewer reads `GET /api/trades`."""

import asyncio
import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import httpx
import pytest
from redis.asyncio import Redis
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import async_sessionmaker

from smtrading.activity.models import Decision
from smtrading.agents.models import McpAuditLog
from smtrading.agents.service import create_agent
from smtrading.common.config import get_settings
from smtrading.common.db import make_engine
from smtrading.engine.execution import execute_fill
from smtrading.engine.models import Fill, Order, OrderSide, OrderStatus, OrderType
from smtrading.engine.quotesource import FakeMarketData
from smtrading.engine.service import (
    PlaceOrderRequest,
    close_position,
    place_bracket_order,
    place_oco_order,
    place_order,
)
from smtrading.events import discard_post_commit
from smtrading.goals.models import Goal, PlanClosure
from smtrading.performance.journal import trade_detail, trade_rows
from smtrading.portfolios.models import Portfolio, PortfolioStatus
from smtrading.portfolios.service import create_portfolio
from smtrading.seed import run_seed

D = Decimal
OPEN = datetime(2026, 11, 10, 15, 0, tzinfo=UTC)  # Tuesday: NASDAQ open
PASSWORD = "correct-horse-battery"


@pytest.fixture
async def env(fresh_db, redis_url, monkeypatch):
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
        await s.execute(update(Portfolio).values(status=PortfolioStatus.archived))  # Demo
        await s.commit()
    qs = FakeMarketData()
    qs.set_fx("EUR", "USD", D("1.25"))

    class NS:
        pass

    ns = NS()
    ns.sm, ns.qs = sm, qs
    yield ns
    await engine.dispose()


def _quote(env, symbol, bid, ask):
    env.qs.set_quote(symbol, D(bid), D(ask), None)


async def _fill(env, s, order):
    quote = (await env.qs.get_quotes([order.symbol]))[order.symbol]
    return await execute_fill(s, env.qs, order.id, quote=quote)


async def _commit(s):
    discard_post_commit(s)
    await s.commit()


@pytest.fixture
async def book(env):
    """The production-shaped history; returns the ids the tests look at."""
    ids: dict = {}
    async with env.sm() as s:
        p = await create_portfolio(s, name="Portafoglio Aggressivo", initial_cash=D(20000))
        pid = ids["pid"] = p.id
        ceo, _ = await create_agent(s, "CEO")
        pm, _ = await create_agent(s, "Portfolio Manager")
        rm, _ = await create_agent(s, "Risk Manager")
        ids["pm"], ids["rm"] = pm.id, rm.id
        s.add(Decision(agent_id=ceo.id, kind="strategy", portfolio_id=pid,
                       summary="Approvata STR-7 rsi_reversion su AAPL, MSFT",
                       details={"ref": "STR-7", "status": "approvata"}))
        for prop, sym in (("PROP-AGG-20261110-1500-1", "AAPL"),
                          ("PROP-AGG-20261110-1500-2", "MSFT")):
            s.add(Decision(agent_id=pm.id, kind="proposal", portfolio_id=pid,
                           summary=f"{prop}: buy {sym} RSI 27 sotto 30", symbols=[sym],
                           details={"ref": prop, "strategy_id": "STR-7",
                                    "indicators": {"rsi14": "27.4", "threshold": "30"}}))
            s.add(Decision(agent_id=rm.id, kind="approval", portfolio_id=pid,
                           subject=f"{prop} approvata", summary=f"Approvata {prop}",
                           details={"ref": prop, "stop": "150", "target": "250"}))
        await _commit(s)

    # 1. bracket entry (STR-7) stopped out
    _quote(env, "AAPL", "199", "200")
    async with env.sm() as s:
        entry, tp, sl = await place_bracket_order(s, env.qs, PlaceOrderRequest(
            portfolio_id=pid, symbol="AAPL", side=OrderSide.buy, type=OrderType.market,
            qty=D(5), client_order_id="PROP-AGG-20261110-1500-1", agent_id=str(ids["pm"]),
        ), take_profit=D(250), stop_loss=D(150), now=OPEN)
        ids["bracket"], ids["tp"], ids["sl"] = entry.id, tp.id, sl.id
        s.add(McpAuditLog(agent_id=ids["rm"], method="modify_order",
                          params={"order_id": str(sl.id), "stop_price": "160"},
                          outcome="ok", latency_ms=12))
        await _commit(s)
    _quote(env, "AAPL", "149", "150")
    async with env.sm() as s:
        sl = await s.get(Order, ids["sl"])
        sl.status = OrderStatus.triggered
        await s.flush()
        await _fill(env, s, sl)
        await _commit(s)
    async with env.sm() as s:
        s.add(Decision(agent_id=pm.id, kind="lesson", portfolio_id=pid,
                       summary="Stop di PROP-AGG-20261110-1500-1 troppo stretto",
                       details={"refs": ["PROP-AGG-20261110-1500-1"], "agents": ["pm"]}))
        await _commit(s)

    # 2. plain entry, OCO «PROTEGGI», CHIUDI parziale (exit rule), then the target
    _quote(env, "MSFT", "399", "400")
    async with env.sm() as s:
        entry = await place_order(s, env.qs, PlaceOrderRequest(
            portfolio_id=pid, symbol="MSFT", side=OrderSide.buy, type=OrderType.market,
            qty=D(10), client_order_id="PROP-AGG-20261110-1500-2", agent_id=str(ids["pm"]),
        ), now=OPEN)
        ids["msft"] = entry.id
        a, b = await place_oco_order(s, env.qs, PlaceOrderRequest(
            portfolio_id=pid, symbol="MSFT", side=OrderSide.sell, type=OrderType.limit,
            qty=D(10), limit_price=D(460),
        ), PlaceOrderRequest(
            portfolio_id=pid, symbol="MSFT", side=OrderSide.sell, type=OrderType.stop,
            qty=D(10), stop_price=D(360),
        ), now=OPEN)
        ids["oco1"] = (a.id, b.id)
        await _commit(s)
    async with env.sm() as s:
        s.add(Decision(agent_id=pm.id, kind="proposal", portfolio_id=pid, symbols=["MSFT"],
                       summary="CHIUSURA · AGG · MSFT · parziale 4: regola d'uscita RSI "
                               "sopra 70", details={"ref": "CHIUSURA-AGG-MSFT-1"}))
        await _commit(s)
    _quote(env, "MSFT", "419", "420")
    async with env.sm() as s:
        close = await close_position(s, env.qs, pid, "MSFT", D(4), now=OPEN)
        ids["msft_close"] = close.id
        a, b = await place_oco_order(s, env.qs, PlaceOrderRequest(
            portfolio_id=pid, symbol="MSFT", side=OrderSide.sell, type=OrderType.limit,
            qty=D(6), limit_price=D(460),
        ), PlaceOrderRequest(
            portfolio_id=pid, symbol="MSFT", side=OrderSide.sell, type=OrderType.stop,
            qty=D(6), stop_price=D(380),
        ), now=OPEN)
        ids["oco2"] = (a.id, b.id)
        await _commit(s)
    _quote(env, "MSFT", "461", "462")
    async with env.sm() as s:
        await _fill(env, s, await s.get(Order, ids["oco2"][0]))
        await _commit(s)

    # 3. a manual (portal) buy closed by SMTrading at the end of a piano
    _quote(env, "AAPL", "199", "200")
    async with env.sm() as s:
        o = await place_order(s, env.qs, PlaceOrderRequest(
            portfolio_id=pid, symbol="AAPL", side=OrderSide.buy, type=OrderType.market,
            qty=D(2), source="admin"), now=OPEN)
        ids["manual"] = o.id
        await _commit(s)
    _quote(env, "AAPL", "209", "210")
    async with env.sm() as s:
        close = await close_position(s, env.qs, pid, "AAPL", source="system", now=OPEN)
        g = Goal(name="Piano", horizon="week", start_date=date(2026, 11, 9), scope="portfolio",
                 portfolio_id=pid, target_pct=D(2), closes_positions=True)
        s.add(g)
        await s.flush()
        s.add(PlanClosure(goal_id=g.id, period_start=date(2026, 11, 9),
                          period_end=date(2026, 11, 13), ended_at=OPEN, portfolio_id=pid,
                          symbol="AAPL", qty=D(2), state="closed", grace_days=0,
                          order_id=close.id, closed_by="system", closed_at=OPEN))
        await _commit(s)

    # 4. still open
    _quote(env, "AAPL", "199", "200")
    async with env.sm() as s:
        o = await place_order(s, env.qs, PlaceOrderRequest(
            portfolio_id=pid, symbol="AAPL", side=OrderSide.buy, type=OrderType.market,
            qty=D(1), source="admin"), now=OPEN)
        ids["open"] = o.id
        await _commit(s)
    _quote(env, "AAPL", "219", "221")
    return ids


async def test_rows(env, book):
    async with env.sm() as s:
        rows = await trade_rows(s, env.qs)
    by = {r.order_id: r for r in rows}
    assert rows[0].order_id == book["open"]  # newest first
    assert len(rows) == 4

    stopped = by[book["bracket"]]
    assert stopped.key == str(book["bracket"])
    assert (stopped.strategy_ref, stopped.proposal_ref) == ("STR-7", "PROP-AGG-20261110-1500-1")
    assert stopped.outcome == "loss" and stopped.exit_reason == "stop"
    assert stopped.exit_reason_label == "Stop loss" and stopped.pnl_eur < 0
    assert stopped.pnl_pct < 0 and stopped.closed_at is not None and stopped.holding_s >= 0

    msft = by[book["msft"]]
    assert msft.exit_reasons == ["strategy_exit", "target"] and msft.exit_reason == "target"
    assert msft.outcome == "win" and msft.open_qty == 0 and msft.qty == 10
    # % over the entry value (10 × 400 USD = 3200 EUR)
    assert msft.pnl_pct == (msft.pnl_eur / (msft.entry_price * 10 * D("0.8")) * 100
                            ).quantize(D("0.01"))

    plan = by[book["manual"]]
    assert plan.strategy_ref is None and plan.exit_reason == "plan_close"
    assert plan.exit_reason_label == "Chiusura di fine piano"

    still = by[book["open"]]
    assert still.outcome == "open" and still.exit_reason is None and still.mark == D(220)
    assert still.unrealized_pnl_eur is not None and still.closed_at is None


async def test_detail_timeline(env, book):
    async with env.sm() as s:
        d = await trade_detail(s, env.qs, str(book["bracket"]))
    assert d["trade"].exit_reason == "stop"
    assert d["proposal"]["details"]["indicators"]["rsi14"] == "27.4"
    assert d["proposal"]["agent"] == "Portfolio Manager"
    kinds = [x["kind"] for x in d["decisions"]]
    assert "proposal" in kinds and "approval" in kinds
    assert all(x["details"].get("ref") != "PROP-AGG-20261110-1500-2" for x in d["decisions"])
    [lesson] = d["lessons"]
    assert "troppo stretto" in lesson["summary"]
    orders = {o["id"]: o for o in d["orders"]}
    assert set(orders) == {book["bracket"], book["tp"], book["sl"]}
    assert orders[book["bracket"]]["label"] == "ingresso"
    assert orders[book["sl"]]["label"] == "stop loss"
    tp = orders[book["tp"]]
    assert tp["status"] == "canceled" and "OCO" in tp["cancel_reason"]
    [change] = d["changes"]
    assert change["agent"] == "Risk Manager" and change["changes"] == {"stop_price": "160"}
    entry, exit_ = d["fills"]
    assert (entry["kind"], exit_["kind"]) == ("entry", "exit")
    assert exit_["exit_reason"] == "stop" and entry["fee_amount"] > 0
    # EUR portfolio, USD stock: the buy converted EUR to USD, with an FX fee
    fx = entry["fx"]
    assert fx["sold"]["currency"] == "EUR" and fx["bought"]["currency"] == "USD"
    assert fx["rate"] == D("1.25") and fx["fee"]
    types = [i["type"] for i in d["timeline"]]
    assert types.index("decision.proposal") < types.index("fill.entry") < types.index(
        "fill.exit")
    assert "lesson" in types and "order.change" in types and "order.canceled" in types
    assert d["timeline"] == sorted(d["timeline"], key=lambda i: i["ts"])


async def test_detail_oco_and_partial_close(env, book):
    async with env.sm() as s:
        d = await trade_detail(s, env.qs, str(book["msft"]))
    ids = {o["id"] for o in d["orders"]}
    assert ids == {book["msft"], *book["oco1"], book["msft_close"], *book["oco2"]}
    labels = {o["id"]: o["label"] for o in d["orders"]}
    assert labels[book["oco1"][0]] == "OCO target" and labels[book["oco1"][1]] == "OCO stop"
    assert labels[book["msft_close"]] == "chiusura"
    first = [o for o in d["orders"] if o["id"] in book["oco1"]]
    assert all(o["status"] == "canceled" for o in first)
    assert [f["exit_reason"] for f in d["fills"] if f["kind"] == "exit"] == [
        "strategy_exit", "target"]
    assert sum(f["qty"] for f in d["fills"] if f["kind"] == "exit") == 10
    assert any(i["type"] == "activity.order.exits_canceled" for i in d["timeline"])
    # the bracket trade's orders are not this trade's
    assert book["bracket"] not in ids


async def test_detail_unknown_and_second_trade_key(env, book):
    async with env.sm() as s:
        assert await trade_detail(s, env.qs, "nope") is None
        assert await trade_detail(s, env.qs, str(uuid.uuid4())) is None
        assert await trade_detail(s, env.qs, f"{book['bracket']}.2") is None


async def test_api_filters_and_pages(env, book, redis_url):
    from smtrading.admin.auth import hash_password
    from smtrading.admin.models import AdminUser
    from smtrading.admin.routes_trades import CACHE_KEY
    from smtrading.api.main import create_app
    from smtrading.mcp.runtime import McpRuntime

    # the stopped-out trade happened two months ago
    old = datetime.now(UTC) - timedelta(days=60)
    async with env.sm() as s:
        await s.execute(update(Fill).where(Fill.order_id.in_([book["bracket"], book["sl"]]))
                        .values(created_at=old))
        email = f"viewer-{uuid.uuid4().hex[:8]}@example.com"
        s.add(AdminUser(email=email, password_hash=await hash_password(PASSWORD),
                        role="viewer"))
        await s.commit()
    qs = env.qs
    app = create_app(runtime_factory=lambda sm_, redis_: McpRuntime(sm_, redis_, qs, qs))
    started, stop = asyncio.Event(), asyncio.Event()

    async def run_lifespan():
        async with app.router.lifespan_context(app):
            started.set()
            await stop.wait()

    lifespan = asyncio.create_task(run_lifespan())
    await asyncio.wait_for(started.wait(), 15)
    redis = Redis.from_url(redis_url)
    await redis.delete(CACHE_KEY)
    try:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            r = await c.post("/api/auth/login", json={"email": email, "password": PASSWORD})
            assert r.status_code == 200, r.text

            async def get(**params):
                r = await c.get("/api/trades", params=params)
                assert r.status_code == 200, r.text
                return r.json()

            body = await get()
            assert body["total"] == 4 and len(body["items"]) == 4
            assert body["summary"]["open"] == 1 and body["summary"]["closed"] == 3
            assert body["filters"]["symbols"] == ["AAPL", "MSFT"]
            assert body["filters"]["strategies"] == [
                {"ref": "STR-7", "name": "Approvata STR-7 rsi_reversion su AAPL, MSFT"}]
            assert body["filters"]["no_strategy"] is True
            assert (await get(period="1m"))["total"] == 3  # the old one closed before
            assert (await get(strategy="STR-7"))["total"] == 2
            assert (await get(strategy="none"))["total"] == 2
            assert (await get(symbol="msft"))["total"] == 1
            assert [i["outcome"] for i in (await get(outcome="open"))["items"]] == ["open"]
            assert (await get(outcome="win", symbol="MSFT"))["total"] == 1
            assert (await get(portfolio_id=str(uuid.uuid4())))["total"] == 0
            page = await get(page=2, page_size=3)
            assert page["total"] == 4 and len(page["items"]) == 1
            assert (await c.get("/api/trades", params={"outcome": "x"})).status_code == 400
            assert (await c.get("/api/trades", params={"period": "x"})).status_code == 400

            key = str(book["msft"])
            r = await c.get(f"/api/trades/{key}")
            assert r.status_code == 200, r.text
            detail = r.json()
            assert detail["trade"]["key"] == key and detail["timeline"]
            assert isinstance(detail["trade"]["pnl_eur"], str)
            assert (await c.get(f"/api/trades/{uuid.uuid4()}")).status_code == 404
        await redis.delete(CACHE_KEY)
    finally:
        stop.set()
        await lifespan
        await redis.aclose()
    async with env.sm() as s:
        assert await s.scalar(select(Order.id).where(Order.id == book["open"]))


async def test_two_open_trades_of_a_symbol_keep_their_own_orders(env):
    async with env.sm() as s:
        p = await create_portfolio(s, name="Portafoglio Aggressivo", initial_cash=D(20000))
        await _commit(s)
    _quote(env, "AAPL", "199", "200")
    legs = []
    for coid in ("PROP-A", "PROP-B"):
        async with env.sm() as s:
            legs.append(await place_bracket_order(s, env.qs, PlaceOrderRequest(
                portfolio_id=p.id, symbol="AAPL", side=OrderSide.buy, type=OrderType.market,
                qty=D(2), client_order_id=coid), take_profit=D(250), stop_loss=D(150),
                now=OPEN))
            await _commit(s)
    (a, a_tp, a_sl), (b, b_tp, b_sl) = legs
    async with env.sm() as s:
        da = await trade_detail(s, env.qs, str(a.id))
        db = await trade_detail(s, env.qs, str(b.id))
    assert {o["id"] for o in da["orders"]} == {a.id, a_tp.id, a_sl.id}
    assert {o["id"] for o in db["orders"]} == {b.id, b_tp.id, b_sl.id}
    assert da["trade"].outcome == db["trade"].outcome == "open"
