"""Automatic reduce-only of a real-money portfolio, its guard, and the Telegram rules of
the `live.*` activities (respx only: the real Telegram API is never called)."""

import json
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx
import pytest
import respx
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import async_sessionmaker

from smtrading.activity.models import Activity
from smtrading.brokers.base import BrokerOrder
from smtrading.brokers.live.models import LiveAudit
from smtrading.brokers.live.protect import (
    live_alert,
    record_live_order_activity,
    trip_reduce_only,
)
from smtrading.brokers.models import BrokerLink
from smtrading.common.db import make_engine
from smtrading.common.errors import ErrorCode, SMTError
from smtrading.engine.models import Order, OrderSide, OrderType
from smtrading.events import discard_post_commit, queue_event
from smtrading.portfolios.models import Portfolio, PortfolioStatus
from smtrading.portfolios.service import create_portfolio, set_portfolio_status
from smtrading.risk.daily_loss import DAILY_LOSS_REASON, reset_daily
from smtrading.risk.models import AppSetting
from smtrading.seed import run_seed
from smtrading.telegram import config as tg_config
from smtrading.telegram.client import API_URL
from smtrading.telegram.config import TelegramConfig, save_config
from smtrading.telegram.models import TelegramOutbox
from smtrading.telegram.outbox import enqueue, resume_paused
from smtrading.telegram.planner import TelegramPlanner
from smtrading.telegram.sender import OutboxSender

TOKEN = "123456789:AAHsecretSECRETsecret_SECRET-1234567"
SEND_URL = f"{API_URL}/bot{TOKEN}/sendMessage"
CHAT = "-100123"
TZ = "Europe/Rome"
DAY = datetime(2027, 3, 10, 9, 0, tzinfo=UTC)  # 10:00 Rome
NIGHT = datetime(2027, 3, 10, 22, 30, tzinfo=UTC)  # 23:30 Rome


class Clock:
    def __init__(self, t: datetime):
        self.t = t

    def __call__(self) -> datetime:
        return self.t


@pytest.fixture(autouse=True)
def _clear_warned():
    tg_config._warned.clear()
    yield
    tg_config._warned.clear()


@pytest.fixture
async def maker(fresh_db, redis_url, monkeypatch):
    monkeypatch.setenv("SMT_DATABASE_URL", fresh_db)
    monkeypatch.setenv("SMT_REDIS_URL", redis_url)
    monkeypatch.setenv("SMT_MASTER_KEY", "test-master-key")
    monkeypatch.setenv("SMT_ADMIN_EMAIL", "admin@example.com")
    monkeypatch.setenv("SMT_ADMIN_PASSWORD", "test-password")
    engine = make_engine(fresh_db)
    sm = async_sessionmaker(engine, expire_on_commit=False)
    async with sm() as s:
        await run_seed(s)
        await s.commit()
    yield sm
    await engine.dispose()


async def _configure(maker, **kw) -> None:
    cfg = TelegramConfig(bot_token=TOKEN, chat_id=CHAT, enabled=True)
    for k, v in kw.items():
        setattr(cfg, k, v)
    async with maker() as s:
        await save_config(s, cfg)
        await s.commit()


async def _portfolio(maker, kind: str | None = "alpaca_live",
                     state: str | None = "armed") -> tuple[uuid.UUID, uuid.UUID | None]:
    async with maker() as s:
        p = await create_portfolio(s, name=f"Reale {uuid.uuid4().hex[:6]}",
                                   initial_cash=Decimal(0))
        link_id = None
        if kind is not None:
            link = BrokerLink(name=f"L-{uuid.uuid4().hex[:8]}", kind=kind, live_state=state)
            s.add(link)
            await s.flush()
            p.broker_link_id = link_id = link.id
        discard_post_commit(s)
        await s.commit()
        return p.id, link_id


async def _get(maker, pid) -> Portfolio:
    async with maker() as s:
        return await s.get(Portfolio, pid)


async def _activities(maker, prefix: str = "") -> list[Activity]:
    async with maker() as s:
        return list(await s.scalars(
            select(Activity).where(Activity.telegram.is_not(None),
                                   Activity.type.startswith(prefix))
            .order_by(Activity.id)))


async def _outbox(maker) -> list[TelegramOutbox]:
    async with maker() as s:
        return list(await s.scalars(select(TelegramOutbox).order_by(TelegramOutbox.id)))


async def _trip(maker, pid, cause, **details) -> bool:
    async with maker() as s:
        changed = await trip_reduce_only(s, pid, cause=cause, details=details)
        await s.commit()
        return changed


async def _order(maker, pid, link_id) -> Order:
    async with maker() as s:
        o = Order(portfolio_id=pid, symbol="AAPL", side=OrderSide.buy, type=OrderType.market,
                  qty=Decimal("0.5"), broker_link_id=link_id, source="admin")
        s.add(o)
        await s.commit()
        return o


# -- automatic reduce-only -------------------------------------------------------------


async def test_trip_reduce_only_sets_live_reason_and_alert(maker):
    pid, link_id = await _portfolio(maker)
    assert await _trip(maker, pid, "fuse", orders=31) is True
    p = await _get(maker, pid)
    assert (p.status, p.reduce_only_reason, p.reduce_only_until) == (
        PortfolioStatus.reduce_only, "live_fuse", None)
    [alert] = await _activities(maker, "live.")
    assert (alert.type, alert.level, alert.telegram) == ("live.reduce_only", "critical",
                                                         "pending")
    assert alert.portfolio_id == pid
    assert alert.data["cause"] == "fuse"
    assert alert.data["link_id"] == str(link_id)
    async with maker() as s:
        [audit] = await s.scalars(select(LiveAudit))
    assert (audit.action, audit.link_id, audit.portfolio_id, audit.actor) == (
        "reduce_only_auto", link_id, pid, "system")
    assert audit.details["cause"] == "fuse" and audit.details["orders"] == 31

    # already blocked: same cause within the hour -> no new alert; another cause -> one
    assert await _trip(maker, pid, "fuse") is False
    assert len(await _activities(maker, "live.")) == 1
    assert await _trip(maker, pid, "reconcile_diff") is False
    assert [a.data["cause"] for a in await _activities(maker, "live.")] == [
        "fuse", "reconcile_diff"]
    assert (await _get(maker, pid)).reduce_only_reason == "live_fuse"  # first cause kept
    # an hour later the same cause alerts again
    async with maker() as s:
        await s.execute(update(Activity).values(ts=Activity.ts - timedelta(hours=2)))
        await s.commit()
    assert await _trip(maker, pid, "fuse") is False
    assert len(await _activities(maker, "live.")) == 3

    with pytest.raises(ValueError):
        await _trip(maker, pid, "whatever")


async def test_trip_upgrades_a_daily_loss_and_marks_a_lock(maker):
    pid, _ = await _portfolio(maker)
    async with maker() as s:
        p = await s.get(Portfolio, pid)
        p.status = PortfolioStatus.reduce_only
        p.reduce_only_reason = DAILY_LOSS_REASON
        p.reduce_only_until = DAY
        await s.commit()
    assert await _trip(maker, pid, "cash_shortfall") is True
    p = await _get(maker, pid)
    assert (p.status, p.reduce_only_reason, p.reduce_only_until) == (
        PortfolioStatus.reduce_only, "live_cash_shortfall", None)

    pid2, _ = await _portfolio(maker)
    async with maker() as s:
        (await s.get(Portfolio, pid2)).status = PortfolioStatus.locked
        await s.commit()
    assert await _trip(maker, pid2, "health") is False
    p2 = await _get(maker, pid2)
    assert (p2.status, p2.reduce_only_reason) == (PortfolioStatus.locked, "live_health")
    assert len(await _activities(maker, "live.")) == 2  # both alerted


async def test_live_reduce_only_not_cleared_by_daily_reset_or_patch(maker):
    """Review Focus 5."""
    pid, _ = await _portfolio(maker)
    async with maker() as s:  # a daily loss first, then the live anomaly
        p = await s.get(Portfolio, pid)
        p.status = PortfolioStatus.reduce_only
        p.reduce_only_reason = DAILY_LOSS_REASON
        p.reduce_only_until = DAY
        await s.commit()
    await _trip(maker, pid, "reconcile_diff")
    async with maker() as s:  # the midnight reset leaves it alone
        assert await reset_daily(s, DAY + timedelta(days=30)) == 0
        await s.commit()
    assert (await _get(maker, pid)).status == PortfolioStatus.reduce_only

    async with maker() as s:  # the admin PATCH path
        p = await s.get(Portfolio, pid)
        with pytest.raises(SMTError) as e:
            set_portfolio_status(s, p, PortfolioStatus.active)
        assert e.value.code == ErrorCode.FORBIDDEN
        assert "Ho verificato, riprendi" in e.value.message
        set_portfolio_status(s, p, PortfolioStatus.locked)  # stricter is fine
        await s.commit()
    async with maker() as s:  # any path assigning the attributes (unarchive, unlock...)
        p = await s.get(Portfolio, pid)
        p.status = PortfolioStatus.active
        p.reduce_only_reason = None
        with pytest.raises(SMTError) as e:
            await s.flush()
        assert e.value.code == ErrorCode.FORBIDDEN
        await s.rollback()
    p = await _get(maker, pid)
    assert (p.status, p.reduce_only_reason) == (PortfolioStatus.locked, "live_reconcile_diff")

    async with maker() as s:  # only the TOTP resume passes allow_live
        p = await s.get(Portfolio, pid)
        set_portfolio_status(s, p, PortfolioStatus.active, allow_live=True)
        await s.commit()
    p = await _get(maker, pid)
    assert (p.status, p.reduce_only_reason, p.reduce_only_until) == (
        PortfolioStatus.active, None, None)


async def _live_blocked(maker) -> uuid.UUID:
    pid, _ = await _portfolio(maker)
    await _trip(maker, pid, "reconcile_diff")
    return pid


@pytest.mark.parametrize("new_reason", [None, DAILY_LOSS_REASON, "broker_liquidation"])
async def test_live_reason_cannot_be_replaced_or_cleared(maker, new_reason):
    """I3: first flush changes only the reason (the second would set active)."""
    pid = await _live_blocked(maker)
    async with maker() as s:
        p = await s.get(Portfolio, pid)
        p.reduce_only_reason = new_reason
        with pytest.raises(SMTError) as e:
            await s.flush()
        assert e.value.code == ErrorCode.FORBIDDEN
        await s.rollback()
    async with maker() as s:  # also when the old value was never loaded (expired)
        p = await s.get(Portfolio, pid)
        s.expire(p, ["reduce_only_reason", "status"])
        p.reduce_only_reason = new_reason
        with pytest.raises(SMTError):
            await s.flush()
        await s.rollback()
    async with maker() as s:  # bulk-free two-step: status first, then the reason
        p = await s.get(Portfolio, pid)
        s.expire(p, ["reduce_only_reason", "status"])
        p.status = PortfolioStatus.active
        with pytest.raises(SMTError):
            await s.flush()
        await s.rollback()
    p = await _get(maker, pid)
    assert (p.status, p.reduce_only_reason) == (PortfolioStatus.reduce_only,
                                                "live_reconcile_diff")


async def test_margin_lock_keeps_the_live_reason(maker):
    from types import SimpleNamespace

    from smtrading.risk.margin import _liquidate_at_broker

    pid = await _live_blocked(maker)
    valuation = SimpleNamespace(
        positions=[SimpleNamespace(symbol="AAPL", qty=Decimal(2),
                                   unrealized_pnl_eur=Decimal(-5))],
        equity_eur=Decimal(1), exposure_eur=Decimal(1), maint_req_eur=Decimal(1),
        warn_req_eur=Decimal(1))
    async with maker() as s:
        p = await s.get(Portfolio, pid)
        [order] = await _liquidate_at_broker(s, p, valuation)
        discard_post_commit(s)
        await s.commit()
    p = await _get(maker, pid)
    assert (p.status, p.reduce_only_reason) == (PortfolioStatus.locked,
                                                "live_reconcile_diff")
    assert order.reduce_only


async def test_trip_without_a_link_still_alerts(maker):
    pid, _ = await _portfolio(maker, kind=None)
    assert await _trip(maker, pid, "health") is True
    [alert] = await _activities(maker, "live.")
    assert alert.data["link_id"] is None
    async with maker() as s:
        assert list(await s.scalars(select(LiveAudit))) == []


async def test_patch_route_and_other_reasons_unchanged(maker):
    pid, _ = await _portfolio(maker, kind=None)
    async with maker() as s:
        p = await s.get(Portfolio, pid)
        p.status = PortfolioStatus.reduce_only
        p.reduce_only_reason = DAILY_LOSS_REASON
        await s.commit()
    async with maker() as s:  # a non-live reduce-only is cleared as before
        p = await s.get(Portfolio, pid)
        set_portfolio_status(s, p, PortfolioStatus.active)
        await s.commit()
    assert (await _get(maker, pid)).reduce_only_reason is None


# -- Telegram rules --------------------------------------------------------------------


async def _live_rows(maker, pid, link_id) -> None:
    order = await _order(maker, pid, link_id)
    async with maker() as s:
        await record_live_order_activity(s, order, "sent")
        bo = BrokerOrder(broker_order_id="b1", status="filled",
                         filled_qty=Decimal("0.5"), avg_fill_price=Decimal("200.10"),
                         message=None, fees=Decimal("0.02"))
        await record_live_order_activity(s, order, "filled", bo=bo)
        await live_alert(s, kind="armed", summary="Denaro reale attivato", portfolio_id=pid,
                         link_id=link_id, data={})
        await s.commit()


@pytest.mark.parametrize("cfg", [None, {"enabled": False}, {"bot_token": None}])
async def test_live_activity_enqueued_when_telegram_not_ready_and_at_night(maker, cfg):
    """Review Focus 4: not configured, disabled or without a token -> still in the outbox;
    23:30 Rome -> never digested."""
    if cfg is not None:
        await _configure(maker, **cfg)
    pid, link_id = await _portfolio(maker)
    paper, _ = await _portfolio(maker, kind=None)
    await _live_rows(maker, pid, link_id)
    async with maker() as s:  # a direct non-live event: skipped as before
        queue_event(s, "kill_switch", {"on": True, "canceled": 0})
        discard_post_commit(s)
        await s.commit()
    assert await TelegramPlanner(maker, clock=Clock(NIGHT), tz=TZ).tick() == 4
    rows = await _activities(maker)
    assert [(a.type, a.telegram) for a in rows] == [
        ("live.order.sent", "queued"), ("live.order.filled", "queued"),
        ("live.armed", "queued"), ("kill_switch", "skipped")]
    outbox = await _outbox(maker)
    assert [r.status for r in outbox] == ["pending"] * 3
    assert [r.id for r in outbox] == [a.outbox_id for a in rows[:3]]
    assert outbox[0].text.startswith("🔴 REALE · inviato\nReale ")
    assert "buy 0,5 AAPL · mercato" in outbox[0].text
    assert "Prezzo 200,1 USD · valore 100,05 USD · commissioni 0,02" in outbox[1].text
    assert outbox[2].level == "critical"
    assert {r.category for r in outbox} == {"live"}
    assert paper is not None


async def test_live_rows_paused_unreadable_and_stale(maker):
    pid, link_id = await _portfolio(maker)
    await _configure(maker, paused=True)
    await _live_rows(maker, pid, link_id)
    async with maker() as s:  # older than STALE_AFTER: still sent
        await s.execute(update(Activity).values(ts=Activity.ts - timedelta(days=3)))
        await s.commit()
    assert await TelegramPlanner(maker, clock=Clock(DAY), tz=TZ).tick() == 3
    assert [r.status for r in await _outbox(maker)] == ["paused"] * 3

    # unreadable token while enabled: other rows wait, live rows go on to the outbox
    async with maker() as s:
        await s.execute(update(AppSetting).where(AppSetting.key == "telegram").values(
            value={"token_enc": "AAAA", "chat_id": CHAT, "enabled": True}))
        for _ in range(3):  # more waiting rows ahead than a live row would need
            queue_event(s, "kill_switch", {"on": True, "canceled": 0})
        discard_post_commit(s)
        await s.commit()
    async with maker() as s:
        await live_alert(s, kind="disarmed", summary="Denaro reale disattivato",
                         portfolio_id=pid, link_id=link_id, data={})
        await s.commit()
    assert await TelegramPlanner(maker, clock=Clock(DAY), tz=TZ).tick() == 1
    rows = await _activities(maker)
    assert [(a.type, a.telegram) for a in rows][-4:] == [
        ("kill_switch", "pending")] * 3 + [("live.disarmed", "queued")]
    assert [r.status for r in await _outbox(maker)][-1] == "pending"


async def _event_order(maker, pid, link_id, *, synced: bool) -> uuid.UUID:
    async with maker() as s:
        o = Order(portfolio_id=pid, symbol="AAPL", side=OrderSide.buy,
                  type=OrderType.market, qty=Decimal(1), broker_link_id=link_id)
        if synced:
            o.broker_status = "filled"
            o.broker_sent_at = DAY
            o.broker_order_id = f"b-{uuid.uuid4().hex[:6]}"
        s.add(o)
        await s.commit()
        return o.id


async def test_order_events_of_live_portfolio_skipped(maker):
    """I1: only an order that reached BrokerSync is covered by its live.order.* twin; a
    pre-trade rejection keeps its own direct message."""
    await _configure(maker)
    live, live_link = await _portfolio(maker)
    demo, demo_link = await _portfolio(maker, kind="alpaca_paper", state=None)
    synced = await _event_order(maker, live, live_link, synced=True)
    refused = await _event_order(maker, live, live_link, synced=False)
    demo_synced = await _event_order(maker, demo, demo_link, synced=True)
    async with maker() as s:
        for pid, oid in ((live, synced), (demo, demo_synced)):
            queue_event(s, "order.filled", {
                "portfolio_id": pid, "order_id": oid, "fill_id": uuid.uuid4(),
                "side": "buy", "qty": Decimal(1), "symbol": "AAPL", "price": Decimal(10),
                "currency": "USD"})
        for oid in (refused, uuid.uuid4(), None):  # pre-trade, unknown order, no order id
            queue_event(s, "order.rejected", {
                "portfolio_id": live, "order_id": oid, "symbol": "AAPL",
                "message": "LIVE_CASH_LIMIT"})
        discard_post_commit(s)
        await s.commit()
    async with maker() as s:  # the live twin of the synced fill
        await record_live_order_activity(s, await s.get(Order, synced), "filled")
        await s.commit()
    assert await TelegramPlanner(maker, clock=Clock(DAY), tz=TZ).tick() == 6
    got = [(a.portfolio_id, a.type, a.telegram) for a in await _activities(maker)]
    assert got == [
        (live, "order.filled", "skipped"), (demo, "order.filled", "queued"),
        (live, "order.rejected", "queued"), (live, "order.rejected", "queued"),
        (live, "order.rejected", "queued"), (live, "live.order.filled", "queued"),
    ]


async def test_paused_live_rows_survive_resume_after_24h(maker):
    """I2: live outbox rows (category live) are never stale-skipped by a resume."""
    pid, link_id = await _portfolio(maker)
    await _configure(maker, paused=True)
    await _live_rows(maker, pid, link_id)
    async with maker() as s:  # a non-live, non-critical paused message too
        await enqueue(s, source="system", category="event", level="info", text="vecchia")
        await s.commit()
    assert await TelegramPlanner(maker, clock=Clock(DAY), tz=TZ).tick() == 3
    async with maker() as s:
        await s.execute(update(TelegramOutbox).values(
            created_at=TelegramOutbox.created_at - timedelta(days=3)))
        await s.commit()
    async with maker() as s:
        assert await resume_paused(s) == 3
        await s.commit()
    got = [(r.category, r.level, r.status) for r in await _outbox(maker)]
    assert got == [("event", "info", "skipped"), ("live", "info", "pending"),
                   ("live", "info", "pending"), ("live", "critical", "pending")]


async def test_live_order_at_night_sent_silent(maker):
    await _configure(maker)
    pid, link_id = await _portfolio(maker)
    order = await _order(maker, pid, link_id)
    async with maker() as s:
        await record_live_order_activity(s, order, "rejected", message="insufficient funds")
        await s.commit()
    clock = Clock(NIGHT)
    assert await TelegramPlanner(maker, clock=clock, tz=TZ).tick() == 1
    with respx.mock(assert_all_mocked=True, assert_all_called=True) as mock:
        route = mock.post(SEND_URL).mock(return_value=httpx.Response(
            200, json={"ok": True, "result": {"message_id": 5}}))
        async with httpx.AsyncClient() as http:
            assert await OutboxSender(maker, http, clock=clock, tz=TZ).tick() == 1
    [call] = route.calls
    body = json.loads(call.request.content)
    assert body["disable_notification"] is True
    assert body["text"].startswith("🔴 REALE · rifiutato")
    assert "Messaggio: insufficient funds" in body["text"]
    assert "Origine: admin" in body["text"]
    [a] = await _activities(maker, "live.")
    assert a.level == "warning"
