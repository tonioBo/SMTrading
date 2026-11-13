"""Admin API: portfolios, ledger movements (JSON + CSV), cash moves, reversal, reset, orders."""

import asyncio
import csv
import io
import json
import uuid
from datetime import UTC, datetime
from decimal import Decimal

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from smtrading.common.config import get_settings
from smtrading.common.db import make_engine

D = Decimal
PASSWORD = "correct-horse-battery"


@pytest.fixture
async def admin(fresh_db, redis_url, monkeypatch):
    """A logged-in client (CSRF header set) on a seeded fresh database.

    Yields a namespace with `client`, `sm` (sessionmaker), `qs` (the FakeMarketData of
    the app runtime) and `redis_url`."""
    from smtrading.admin.auth import hash_password
    from smtrading.admin.models import AdminUser
    from smtrading.api.main import create_app
    from smtrading.engine.quotesource import FakeMarketData
    from smtrading.mcp.runtime import McpRuntime
    from smtrading.seed import run_seed

    monkeypatch.setenv("SMT_DATABASE_URL", fresh_db)
    monkeypatch.setenv("SMT_REDIS_URL", redis_url)
    monkeypatch.setenv("SMT_MASTER_KEY", "test-master-key")
    monkeypatch.setenv("SMT_ADMIN_EMAIL", "admin@example.com")
    monkeypatch.setenv("SMT_ADMIN_PASSWORD", "test-password")
    get_settings.cache_clear()

    engine = make_engine(fresh_db)
    sm = async_sessionmaker(engine, expire_on_commit=False)
    email = f"admin-{uuid.uuid4().hex[:8]}@example.com"
    async with sm() as s:
        await run_seed(s)
        s.add(AdminUser(email=email, password_hash=await hash_password(PASSWORD)))
        await s.commit()

    qs = FakeMarketData()
    qs.set_fx("EUR", "USD", D("1.25"))
    qs.set_quote("AAPL", D("200.00"), D("200.10"), D("200.05"))
    qs.set_quote("BTC-USD", D("50000.00"), D("50010.00"), D("50005.00"))
    app = create_app(runtime_factory=lambda sm_, redis: McpRuntime(sm_, redis, qs, qs))
    started, stop = asyncio.Event(), asyncio.Event()

    async def run_lifespan():
        async with app.router.lifespan_context(app):
            started.set()
            await stop.wait()

    lifespan = asyncio.create_task(run_lifespan())
    await asyncio.wait_for(started.wait(), 15)
    try:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            r = await client.post("/api/auth/login", json={"email": email, "password": PASSWORD})
            assert r.status_code == 200
            client.headers["X-CSRF-Token"] = client.cookies["smt_csrf"]

            class NS:
                pass

            ns = NS()
            ns.client, ns.sm, ns.qs, ns.redis_url = client, sm, qs, redis_url
            yield ns
    finally:
        stop.set()
        await lifespan
        await engine.dispose()


async def _create(client, name=None, **extra) -> dict:
    body = {"name": name or f"P-{uuid.uuid4().hex[:8]}", "initial_cash": "10000", **extra}
    r = await client.post("/api/portfolios", json=body)
    assert r.status_code == 200, r.text
    return r.json()


async def _journal_count(sm, type_=None) -> int:
    from smtrading.ledger.models import Journal

    async with sm() as s:
        q = select(func.count()).select_from(Journal)
        if type_ is not None:
            q = q.where(Journal.type == type_)
        return await s.scalar(q)


async def test_portfolio_crud(admin):
    c = admin.client
    created = await _create(c, leverage="2", slippage_bps=5, timezone="Europe/Rome")
    pid = created["id"]
    assert created["leverage"] == "2" and created["slippage_bps"] == 5
    assert created["status"] == "active" and created["base_currency"] == "EUR"
    assert created["equity_eur"] == "10000"

    listed = (await c.get("/api/portfolios")).json()
    mine = next(p for p in listed if p["id"] == pid)
    assert mine["equity_eur"] == "10000"
    assert "day_pnl_eur" in mine and "Demo" in {p["name"] for p in listed}

    got = (await c.get(f"/api/portfolios/{pid}")).json()
    assert got["name"] == created["name"]

    r = await c.patch(f"/api/portfolios/{pid}", json={
        "name": "Renamed " + pid[:8], "leverage": "3", "maintenance_margin_pct": "40",
        "warning_margin_pct": "70", "slippage_bps": 10, "timezone": "UTC",
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert (body["name"], body["leverage"], body["maintenance_margin_pct"]) == (
        "Renamed " + pid[:8], "3", "40",
    )
    assert (body["warning_margin_pct"], body["slippage_bps"], body["timezone"]) == (
        "70", 10, "UTC",
    )

    locked = await c.patch(f"/api/portfolios/{pid}", json={"status": "locked"})
    assert locked.json()["status"] == "locked"

    from smtrading.portfolios.models import Portfolio, PortfolioStatus

    async with admin.sm() as s:
        p = await s.get(Portfolio, uuid.UUID(pid))
        p.status = PortfolioStatus.reduce_only
        p.reduce_only_reason = "daily_loss"
        p.reduce_only_until = datetime.now(UTC)
        await s.commit()
    active = (await c.patch(f"/api/portfolios/{pid}", json={"status": "active"})).json()
    assert active["status"] == "active"
    assert active["reduce_only_reason"] is None and active["reduce_only_until"] is None

    bad_status = await c.patch(f"/api/portfolios/{pid}", json={"status": "reduce_only"})
    assert bad_status.status_code == 400

    missing = await c.get(f"/api/portfolios/{uuid.uuid4()}")
    assert missing.status_code == 404 and missing.json()["code"] == "NOT_FOUND"


async def test_duplicate_name(admin):
    created = await _create(admin.client)
    r = await admin.client.post(
        "/api/portfolios", json={"name": created["name"], "initial_cash": "1"}
    )
    assert r.status_code == 400
    assert r.json()["code"] == "INVALID_ORDER" and r.json()["details"]["field"] == "name"


async def test_patch_leverage_above_10(admin):
    pid = (await _create(admin.client))["id"]
    r = await admin.client.patch(f"/api/portfolios/{pid}", json={"leverage": "11"})
    assert r.status_code == 400
    assert r.json()["code"] == "INVALID_ORDER" and r.json()["details"]["field"] == "leverage"
    ok = await admin.client.patch(f"/api/portfolios/{pid}", json={"leverage": "10"})
    assert ok.status_code == 200


async def test_deposit_then_balance(admin):
    c = admin.client
    pid = (await _create(c))["id"]
    r = await c.post(f"/api/portfolios/{pid}/deposit", json={
        "amount": "250.50", "currency": "EUR", "reason": "top-up",
    })
    assert r.status_code == 200, r.text
    assert r.json()["journal"]["type"] == "DEPOSIT"
    val = (await c.get(f"/api/portfolios/{pid}/valuation")).json()
    assert val["cash"] == {"EUR": "10250.5"} and val["equity_eur"] == "10250.5"

    w = await c.post(f"/api/portfolios/{pid}/withdraw", json={"amount": "50.5"})
    assert w.status_code == 200 and w.json()["journal"]["type"] == "WITHDRAWAL"
    too_much = await c.post(f"/api/portfolios/{pid}/withdraw", json={"amount": "99999"})
    assert too_much.status_code == 400 and too_much.json()["code"] == "INSUFFICIENT_FUNDS"
    val = (await c.get(f"/api/portfolios/{pid}/valuation")).json()
    assert val["cash"] == {"EUR": "10200"}


async def test_idempotency_key_single_journal(admin):
    from redis.asyncio import Redis

    c = admin.client
    pid = (await _create(c))["id"]
    before = await _journal_count(admin.sm)
    headers = {"Idempotency-Key": "dep-" + uuid.uuid4().hex}
    first = await c.post(f"/api/portfolios/{pid}/deposit", json={"amount": "10"}, headers=headers)
    second = await c.post(f"/api/portfolios/{pid}/deposit", json={"amount": "10"}, headers=headers)
    assert first.status_code == second.status_code == 200
    assert first.json() == second.json()
    assert await _journal_count(admin.sm) == before + 1

    # a request still in flight with the same key -> 409
    redis = Redis.from_url(admin.redis_url)
    key = "dep-" + uuid.uuid4().hex
    try:
        await redis.set(f"idem:deposit:{key}:lock", "1", ex=60)
        busy = await c.post(
            f"/api/portfolios/{pid}/deposit", json={"amount": "10"},
            headers={"Idempotency-Key": key},
        )
        assert busy.status_code == 409
        stored = await redis.get(f"idem:deposit:{headers['Idempotency-Key']}")
        assert json.loads(stored)["status"] == 200
        assert 0 < await redis.ttl(f"idem:deposit:{headers['Idempotency-Key']}") <= 24 * 3600
    finally:
        await redis.aclose()
    assert await _journal_count(admin.sm) == before + 1


async def test_reverse_empty_reason(admin):
    c = admin.client
    pid = (await _create(c))["id"]
    jid = (await c.post(f"/api/portfolios/{pid}/deposit", json={"amount": "10"})).json()[
        "journal"]["id"]
    r = await c.post(f"/api/journals/{jid}/reverse", json={"reason": "   "})
    assert r.status_code == 400 and r.json()["code"] == "INVALID_REVERSAL"
    ok = await c.post(f"/api/journals/{jid}/reverse", json={"reason": "errore"})
    assert ok.status_code == 200, ok.text
    assert ok.json()["type"] == "REVERSAL" and ok.json()["reversed_journal_id"] == jid
    again = await c.post(f"/api/journals/{jid}/reverse", json={"reason": "errore"})
    assert again.status_code == 400 and again.json()["code"] == "ALREADY_REVERSED"


async def _fill(sm, qs, pid, symbol, side, qty):
    from smtrading.engine.execution import execute_fill
    from smtrading.engine.models import Order, OrderSide, OrderStatus, OrderType

    async with sm() as s:
        o = Order(
            portfolio_id=pid, symbol=symbol, side=OrderSide(side), type=OrderType.market,
            qty=D(qty), status=OrderStatus.new,
        )
        s.add(o)
        await s.flush()
        quote = (await qs.get_quotes([symbol]))[symbol]
        res = await execute_fill(s, qs, o.id, quote=quote)
        await s.commit()
        return res.journals[0].id


async def test_reverse_short_trade(admin):
    """P1: reversing the trade that covered a short brings the position back below zero."""
    c = admin.client
    pid = uuid.UUID((await _create(c))["id"])
    await _fill(admin.sm, admin.qs, pid, "AAPL", "sell", "5")
    admin.qs.set_quote("AAPL", D("179.90"), D("180.00"), D("179.95"))  # covered at a profit
    cover = await _fill(admin.sm, admin.qs, pid, "AAPL", "buy", "5")
    r = await c.post(f"/api/journals/{cover}/reverse", json={"reason": "fill errato"})
    assert r.status_code == 200, r.text
    from smtrading.ledger.models import AccountKind
    from smtrading.ledger.service import get_balance, get_or_create_account

    async with admin.sm() as s:
        acct = await get_or_create_account(
            s, portfolio_id=pid, asset="AAPL", kind=AccountKind.position
        )
        assert await get_balance(s, acct.id) == D(-5)


async def test_transactions_pagination(admin):
    c = admin.client
    pid = (await _create(c, initial_cash="0"))["id"]
    from smtrading.portfolios.service import deposit

    async with admin.sm() as s:
        for i in range(120):
            await deposit(s, uuid.UUID(pid), D(i + 1), "EUR", author_id="test")
        await s.commit()

    pages, before = [], None
    while True:
        params = {"limit": 50} | ({"before_seq": before} if before else {})
        body = (await c.get(f"/api/portfolios/{pid}/transactions", params=params)).json()
        pages.append(body["items"])
        before = body["next_before_seq"]
        if before is None:
            break
    assert [len(p) for p in pages] == [50, 50, 20]
    seqs = [j["seq"] for p in pages for j in p]
    assert seqs == sorted(seqs, reverse=True) and len(set(seqs)) == 120
    assert pages[0][0]["lines"][0]["quantity"] == "120"
    assert pages[2][-1]["lines"][0]["quantity"] == "1"

    only = (await c.get(
        f"/api/portfolios/{pid}/transactions", params={"type": "WITHDRAWAL"}
    )).json()
    assert only == {"items": [], "next_before_seq": None}


async def test_transactions_csv(admin):
    c = admin.client
    pid = (await _create(c, initial_cash="1234.56"))["id"]
    r = await c.get(f"/api/portfolios/{pid}/transactions.csv")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/csv")
    assert "attachment" in r.headers["content-disposition"]
    raw = r.content
    assert raw.startswith("﻿".encode())
    rows = list(csv.reader(io.StringIO(raw.decode("utf-8-sig")), delimiter=";"))
    assert rows[0] == [
        "seq", "data", "tipo", "asset", "conto", "quantità", "prezzo", "valuta",
        "motivazione", "id_ordine", "id_journal",
    ]
    assert len(rows) == 2
    row = dict(zip(rows[0], rows[1], strict=True))
    assert row["tipo"] == "DEPOSIT" and row["asset"] == "EUR" and row["conto"] == "cash"
    assert row["quantità"] == "1234,56" and row["motivazione"] == "initial deposit"


async def test_reset_with_open_positions(admin):
    from smtrading.engine.models import OrderSide, OrderType
    from smtrading.engine.service import PlaceOrderRequest, place_order
    from smtrading.events import dispatch_post_commit
    from smtrading.risk.models import EquitySnapshot, SnapshotKind

    c = admin.client
    pid = uuid.UUID((await _create(c, initial_cash="100000"))["id"])
    async with admin.sm() as s:
        await place_order(s, admin.qs, PlaceOrderRequest(
            portfolio_id=pid, symbol="BTC-USD", side=OrderSide.buy, type=OrderType.market,
            qty=D("0.5"), source="admin",
        ))
        await place_order(s, admin.qs, PlaceOrderRequest(
            portfolio_id=pid, symbol="BTC-USD", side=OrderSide.buy, type=OrderType.limit,
            qty=D("0.1"), limit_price=D("1000.00"), source="admin",
        ))
        await s.commit()
        await dispatch_post_commit(s, None)
    assert len((await c.get(f"/api/portfolios/{pid}/positions")).json()) == 1
    # a locked portfolio is unlocked only for the closing orders
    assert (await c.patch(f"/api/portfolios/{pid}", json={"status": "locked"})).status_code == 200

    r = await c.post(f"/api/portfolios/{pid}/reset", json={
        "new_cash": "5000", "currency": "EUR", "reason": "nuovo esperimento",
    })
    assert r.status_code == 200, r.text
    val = (await c.get(f"/api/portfolios/{pid}/valuation")).json()
    assert {k: v for k, v in val["cash"].items() if v != "0"} == {"EUR": "5000"}
    assert val["positions"] == [] and val["equity_eur"] == "5000"
    assert all(v == "0" for v in [*val["collateral"].values(), *val["margin_loan"].values()])
    assert (await c.get(f"/api/portfolios/{pid}/positions")).json() == []
    open_orders = (await c.get(f"/api/portfolios/{pid}/orders", params={"status": "open"})).json()
    assert open_orders == []
    async with admin.sm() as s:
        snaps = list(await s.scalars(select(EquitySnapshot).where(
            EquitySnapshot.portfolio_id == pid, EquitySnapshot.kind == SnapshotKind.day_open,
        )))
    assert len(snaps) == 1 and snaps[0].equity_eur == D(5000)
    detail = (await c.get(f"/api/portfolios/{pid}")).json()
    assert detail["day_pnl_eur"] == "0" and detail["status"] == "locked"
    from smtrading.ledger.models import JournalType

    assert await _journal_count(admin.sm, JournalType.RESET) == 1


async def test_reset_with_closed_market(admin, monkeypatch):
    from smtrading.admin import portfolio_ops
    from smtrading.instruments.market_hours import MarketStatus

    c = admin.client
    pid = uuid.UUID((await _create(c, initial_cash="100000"))["id"])
    await _fill(admin.sm, admin.qs, pid, "AAPL", "buy", "1")
    monkeypatch.setattr(
        portfolio_ops, "market_status",
        lambda market, at: MarketStatus(market, False, None, None),
    )
    r = await c.post(f"/api/portfolios/{pid}/reset", json={"new_cash": "1", "reason": "x"})
    assert r.status_code == 400
    assert r.json()["code"] == "INVALID_ORDER"
    assert r.json()["message"] == "posizioni aperte su mercati chiusi"


async def test_orders_list_and_cancel(admin):
    from smtrading.engine.models import OrderSide, OrderType
    from smtrading.engine.service import PlaceOrderRequest, place_order

    c = admin.client
    pid = uuid.UUID((await _create(c))["id"])
    async with admin.sm() as s:
        o = await place_order(s, admin.qs, PlaceOrderRequest(
            portfolio_id=pid, symbol="BTC-USD", side=OrderSide.buy, type=OrderType.limit,
            qty=D("0.01"), limit_price=D("1000.00"), source="admin",
        ))
        await s.commit()
    orders = (await c.get(f"/api/portfolios/{pid}/orders")).json()
    assert [x["id"] for x in orders] == [str(o.id)] and orders[0]["status"] == "pending"
    r = await c.post(f"/api/orders/{o.id}/cancel")
    assert r.status_code == 200 and r.json()["status"] == "canceled"
    again = await c.post(f"/api/orders/{o.id}/cancel")
    assert again.status_code == 400
    assert (await c.get(f"/api/portfolios/{pid}/orders", params={"status": "open"})).json() == []
    perf = await c.get(f"/api/portfolios/{pid}/performance", params={"period": "1m"})
    assert perf.status_code == 200 and "series" in perf.json()
    bad = await c.get(f"/api/portfolios/{pid}/performance", params={"period": "2y"})
    assert bad.status_code == 400
