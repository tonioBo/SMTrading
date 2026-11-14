"""BrokerSync on real-money links (plan 8, Task 6): dry run, the gate closing after the
local checks, unknown outcomes (Trading 212, Ruling R7), broker fees, the cash recheck
and the fuse before sending, one live.order.* activity per outcome. Never reaches the
network: respx with assert_all_mocked (an unmocked request fails the test) and the
session socket guard of conftest."""

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx
import pytest
import respx
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from smtrading.activity.models import Activity
from smtrading.brokers import t212_core
from smtrading.brokers.alpaca_paper import AlpacaPaperAdapter
from smtrading.brokers.live.alpaca_live import AlpacaLiveAdapter
from smtrading.brokers.live.gate import LiveGateTransport
from smtrading.brokers.live.models import LiveAudit
from smtrading.brokers.live.t212_live import T212LiveAdapter
from smtrading.brokers.models import BrokerLink
from smtrading.brokers.service import create_link, link_portfolio
from smtrading.brokers.sync import BrokerSync
from smtrading.common.db import make_engine
from smtrading.common.errors import ErrorCode, SMTError
from smtrading.engine.models import BrokerState, Fill, Order, OrderSide, OrderStatus, OrderType
from smtrading.engine.quotesource import FakeQuoteSource
from smtrading.engine.service import PlaceOrderRequest, cancel_order, place_order
from smtrading.events import dispatch_post_commit
from smtrading.portfolios.models import Portfolio, PortfolioStatus
from smtrading.portfolios.service import create_portfolio
from smtrading.risk.settings import set_kill_switch
from smtrading.seed import run_seed

D = Decimal
TEN = D(10)
OPEN = datetime(2026, 11, 10, 15, 0, tzinfo=UTC)  # Tuesday: NASDAQ open
ALPACA = AlpacaLiveAdapter.BASE_URL
T212 = T212LiveAdapter.BASE_URL + "/api/v0/equity"
INSTRUMENTS = [{"ticker": "AAPL_US_EQ", "shortName": "AAPL", "type": "STOCK",
                "currencyCode": "USD"}]


def _mock(**kw):
    return respx.mock(assert_all_mocked=True, assert_all_called=kw.get("called", True))


@pytest.fixture(autouse=True)
def _env(monkeypatch, pg_url, redis_url):
    monkeypatch.setenv("SMT_DATABASE_URL", pg_url)
    monkeypatch.setenv("SMT_REDIS_URL", redis_url)
    monkeypatch.setenv("SMT_MASTER_KEY", "test-master-key")
    monkeypatch.setenv("SMT_ADMIN_EMAIL", "admin@example.com")
    monkeypatch.setenv("SMT_ADMIN_PASSWORD", "test-password")

    def reset():
        for cache in t212_core._instrument_caches.values():
            cache.data.update(at=0.0, items=None, failed_at=None)
            cache.reverse.clear()
        t212_core._rate_blocked_until.clear()

    reset()
    yield
    reset()


@pytest.fixture
async def maker(fresh_db):
    engine = make_engine(fresh_db)
    sm = async_sessionmaker(engine, expire_on_commit=False)
    async with sm() as s:
        await run_seed(s)
        await s.commit()
    yield sm
    await engine.dispose()


@pytest.fixture
async def redis(redis_url):
    r = Redis.from_url(redis_url)
    await r.flushdb()
    yield r
    await r.aclose()


@pytest.fixture
def qs():
    q = FakeQuoteSource()
    q.set_fx("EUR", "USD", D("1.25"))
    q.set_fx("USD", "EUR", D("0.8"))
    q.set_quote("AAPL", D("99.90"), D("100.00"), D("99.95"))
    return q


class Clock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


async def _live(maker, *, kind="alpaca_live", state="armed", cash="100000",
                ccy="USD", snapshot_at=OPEN) -> tuple[uuid.UUID, uuid.UUID]:
    """A EUR portfolio linked to a real-money link (no keys: canary-free, nothing real)."""
    async with maker() as s:
        p = await create_portfolio(s, name=f"P-{uuid.uuid4().hex[:8]}",
                                   initial_cash=D(10000))
        link = BrokerLink(name=f"L-{uuid.uuid4().hex[:8]}", kind=kind, live_state=state,
                          last_account_snapshot={"currency": ccy, "cash_available": cash},
                          last_snapshot_at=snapshot_at)
        s.add(link)
        await s.flush()
        p.broker_link_id = link.id
        await s.commit()
        return p.id, link.id


async def _place(maker, redis, qs, pid, *, side=OrderSide.buy, qty=TEN, now=OPEN) -> uuid.UUID:
    async with maker() as s:
        order = await place_order(s, qs, PlaceOrderRequest(
            portfolio_id=pid, symbol="AAPL", side=side, type=OrderType.market, qty=qty,
            agent_id="agent-1"), now=now)
        await s.commit()
        await dispatch_post_commit(s, redis)
        return order.id


async def _order(maker, oid) -> Order:
    async with maker() as s:
        return await s.get(Order, oid)


async def _activities(maker, oid) -> list[Activity]:
    async with maker() as s:
        return list(await s.scalars(
            select(Activity).where(Activity.type.startswith("live.order."),
                                   Activity.data["order_id"].astext == str(oid))
            .order_by(Activity.id)))


async def _types(maker, oid) -> list[str]:
    return [a.type for a in await _activities(maker, oid)]


async def _audit(maker, oid) -> list[LiveAudit]:
    async with maker() as s:
        return list(await s.scalars(select(LiveAudit).where(LiveAudit.order_id == oid)))


def _sync(maker, redis, qs, clock: Clock | None = None) -> BrokerSync:
    return BrokerSync(maker, redis, qs, None, clock=clock or Clock(OPEN))


def _alpaca_order(status="new", filled="0", avg=None, commission=None) -> dict:
    return {"id": "bo-1", "client_order_id": "x", "symbol": "AAPL", "side": "buy",
            "qty": "10", "status": status, "filled_qty": filled, "filled_avg_price": avg,
            "commission": commission}


async def test_dry_run_end_to_end(maker, redis, qs):
    """Agent order -> risk -> BrokerSync -> rejected LIVE_DRY_RUN with its payload; no
    request reached the (mocked) broker."""
    pid, _ = await _live(maker, state="dry_run")
    oid = await _place(maker, redis, qs, pid)
    assert (await _order(maker, oid)).broker_status == BrokerState.TO_SEND
    with _mock(called=False) as mock:
        await _sync(maker, redis, qs).tick()
    assert mock.calls.call_count == 0
    o = await _order(maker, oid)
    assert o.status == OrderStatus.rejected and o.reject_code == ErrorCode.LIVE_DRY_RUN
    assert o.reject_message == "prova a secco: ordine non inviato"
    assert o.broker_sent_at is None and o.broker_order_id is None
    acts = await _activities(maker, oid)
    assert [a.type for a in acts] == ["live.order.dry_run"]
    payload = acts[0].data["payload"]
    assert payload["symbol"] == "AAPL" and payload["qty"] == "10" and payload["side"] == "buy"
    assert "CANARY" not in str(acts[0].data)


async def test_armed_send_records_sent_activity_and_audit(maker, redis, qs):
    pid, link_id = await _live(maker)
    oid = await _place(maker, redis, qs, pid)
    with _mock() as mock:
        post = mock.post(f"{ALPACA}/v2/orders").mock(
            return_value=httpx.Response(200, json=_alpaca_order()))
        await _sync(maker, redis, qs).tick()
    assert post.call_count == 1
    assert "smt_order_id" in post.calls.last.request.extensions
    o = await _order(maker, oid)
    assert o.broker_order_id == "bo-1" and o.broker_status == "accepted"
    assert await _types(maker, oid) == ["live.order.sent"]
    (audit,) = await _audit(maker, oid)
    assert audit.action == "order_sent" and audit.link_id == link_id
    assert audit.details["payload"]["symbol"] == "AAPL"
    assert audit.details["response"]["id"] == "bo-1"
    assert "APCA" not in str(audit.details)  # never the request headers


@pytest.mark.parametrize("change", ["kill_switch", "disarmed", "locked"])
async def test_gate_closed_after_must_stop_is_rejected_not_unknown(
        maker, redis, qs, monkeypatch, change):
    """Review Focus 2: the state changes after BrokerSync's own checks (here: while the
    order is claimed, in `prepare`); the gate reads it again and the POST never reaches
    the transport. The order is rejected with the gate's code, never `unknown`."""
    pid, link_id = await _live(maker)
    oid = await _place(maker, redis, qs, pid)

    async def flip(self, instruments=()):
        async with maker() as s:
            if change == "kill_switch":
                await set_kill_switch(s, True)
            elif change == "disarmed":
                (await s.get(BrokerLink, link_id)).live_state = "configured"
            else:
                (await s.get(Portfolio, pid)).status = PortfolioStatus.locked
            await s.commit()

    monkeypatch.setattr(AlpacaLiveAdapter, "prepare", flip)
    with _mock(called=False) as mock:
        await _sync(maker, redis, qs).tick()
    assert mock.calls.call_count == 0
    o = await _order(maker, oid)
    expected = {"kill_switch": ErrorCode.KILL_SWITCH_ACTIVE,
                "disarmed": ErrorCode.LIVE_NOT_ARMED,
                "locked": ErrorCode.PORTFOLIO_LOCKED}[change]
    assert o.status == OrderStatus.rejected and o.reject_code == expected
    assert o.broker_status != BrokerState.UNKNOWN and o.broker_sent_at is None
    assert await _types(maker, oid) == ["live.order.rejected"]


async def test_gate_failure_is_not_sent(maker, redis, qs, monkeypatch):
    """An error inside the gate itself (e.g. its database read): nothing was sent; the
    order is rejected (never `unknown`, never sent again blindly)."""
    pid, _ = await _live(maker)
    oid = await _place(maker, redis, qs, pid)

    async def broken(self, request, method):
        raise ConnectionError("database unavailable")

    monkeypatch.setattr(LiveGateTransport, "_check", broken)
    with _mock(called=False) as mock:
        await _sync(maker, redis, qs).tick()
        await _sync(maker, redis, qs).tick()
    assert mock.calls.call_count == 0
    o = await _order(maker, oid)
    assert o.status == OrderStatus.rejected and o.reject_code == ErrorCode.PROVIDER_UNAVAILABLE
    assert "non inviato" in o.reject_message
    assert o.broker_status != BrokerState.UNKNOWN and o.broker_sent_at is None
    assert await _types(maker, oid) == ["live.order.rejected"]


async def test_t212_live_unknown_outcome_no_resend_reduce_only(maker, redis, qs):
    """Review Focus 1: a timeout after the POST: no automatic resend, find_order, and at
    `_give_up` the portfolio goes reduce-only (🔴 esito sconosciuto); a new order of the
    agent on the symbol is accepted only if it reduces."""
    pid, _ = await _live(maker, kind="t212_live", ccy="EUR")
    oid = await _place(maker, redis, qs, pid)
    clock = Clock(OPEN)
    with _mock() as mock:
        mock.get(f"{T212}/metadata/instruments").mock(
            return_value=httpx.Response(200, json=INSTRUMENTS))
        post = mock.post(f"{T212}/orders/market").mock(side_effect=httpx.ReadTimeout("t"))
        mock.get(f"{T212}/orders").mock(return_value=httpx.Response(200, json=[]))
        mock.get(f"{T212}/history/orders", params={"limit": "50"}).mock(
            return_value=httpx.Response(200, json={"items": []}))
        sync = _sync(maker, redis, qs, clock)
        await sync.tick()
        o = await _order(maker, oid)
        assert o.broker_status == BrokerState.UNKNOWN and o.broker_sent_at is not None
        clock.now = OPEN + timedelta(minutes=1)
        await sync.tick()  # looked up, not found, still within the window
        assert (await _order(maker, oid)).status == OrderStatus.pending
        clock.now = OPEN + timedelta(minutes=6)
        await sync.tick()
    assert post.call_count == 1  # never sent again
    o = await _order(maker, oid)
    assert o.status == OrderStatus.rejected and o.reject_code == ErrorCode.PROVIDER_UNAVAILABLE
    assert await _types(maker, oid) == ["live.order.rejected"]
    async with maker() as s:
        p = await s.get(Portfolio, pid)
        assert (p.status, p.reduce_only_reason) == (PortfolioStatus.reduce_only,
                                                    "live_order_unknown")
        alert = await s.scalar(select(Activity).where(Activity.type == "live.reduce_only",
                                                      Activity.portfolio_id == pid))
        assert alert is not None and alert.data["cause"] == "order_unknown"
    with pytest.raises(SMTError) as e:
        await _place(maker, redis, qs, pid)
    assert e.value.code == ErrorCode.REDUCE_ONLY


async def test_broker_fee_recorded_as_actual_fee(maker, redis, qs):
    pid, _ = await _live(maker)
    oid = await _place(maker, redis, qs, pid)
    with _mock() as mock:
        mock.post(f"{ALPACA}/v2/orders").mock(
            return_value=httpx.Response(200, json=_alpaca_order()))
        mock.get(f"{ALPACA}/v2/orders/bo-1").mock(side_effect=[
            httpx.Response(200, json=_alpaca_order("partially_filled", "4", "100.00", "0.10")),
            httpx.Response(200, json=_alpaca_order("filled", "10", "100.00", "0.37")),
        ])
        sync = _sync(maker, redis, qs)
        await sync.tick()  # sent
        await sync.tick()  # 4 filled
        await sync.tick()  # 10 filled
    o = await _order(maker, oid)
    assert o.status == OrderStatus.filled and o.filled_qty == TEN
    async with maker() as s:
        fills = list(await s.scalars(select(Fill).where(Fill.order_id == oid)
                                     .order_by(Fill.qty)))
    # the broker's cumulative commission: 0.10 with the first fill, 0.27 more with the rest
    assert [(f.qty, f.fee_amount, f.fee_currency, f.fee_rule_id) for f in fills] == [
        (D(4), D("0.10"), "USD", None), (D(6), D("0.27"), "USD", None)]
    acts = await _activities(maker, oid)
    assert [a.type for a in acts] == ["live.order.sent", "live.order.partial",
                                      "live.order.filled"]
    assert D(acts[-1].data["fees"]) == D("0.37") and D(acts[-1].data["price"]) == D(100)


async def test_cash_recheck_before_send_with_fresh_snapshot(maker, redis, qs):
    """A snapshot older than 60 s: BrokerSync reads the account (GET only) before sending;
    with too little cash at the broker the order is rejected, nothing is POSTed."""
    pid, link_id = await _live(maker, snapshot_at=OPEN - timedelta(minutes=5))
    oid = await _place(maker, redis, qs, pid)  # placement: portfolio cash only
    with _mock() as mock:
        mock.get(f"{ALPACA}/v2/account").mock(return_value=httpx.Response(200, json={
            "id": "acct-1", "cash": "500", "buying_power": "500", "currency": "USD",
            "multiplier": "1"}))
        mock.get(f"{ALPACA}/v2/positions").mock(return_value=httpx.Response(200, json=[]))
        mock.get(f"{ALPACA}/v2/orders").mock(return_value=httpx.Response(200, json=[]))
        await _sync(maker, redis, qs).tick()
    o = await _order(maker, oid)
    assert o.status == OrderStatus.rejected and o.reject_code == ErrorCode.LIVE_CASH_LIMIT
    assert o.broker_sent_at is None
    assert await _types(maker, oid) == ["live.order.rejected"]
    async with maker() as s:
        link = await s.get(BrokerLink, link_id)
        assert link.last_snapshot_at == OPEN
        assert D(link.last_account_snapshot["cash_available"]) == D(500)
        assert "raw" not in link.last_account_snapshot


async def test_fuse_recheck_before_send(maker, redis, qs):
    pid, link_id = await _live(maker)
    oid = await _place(maker, redis, qs, pid)
    async with maker() as s:  # 30 real orders sent meanwhile
        for _ in range(30):
            s.add(Order(portfolio_id=pid, symbol="AAPL", side=OrderSide.buy,
                        type=OrderType.market, qty=D(1), status=OrderStatus.filled,
                        broker_link_id=link_id, broker_sent_at=OPEN - timedelta(minutes=1)))
        await s.commit()
    with _mock(called=False):
        await _sync(maker, redis, qs).tick()
    o = await _order(maker, oid)
    assert o.status == OrderStatus.rejected and o.reject_code == ErrorCode.RISK_LIMIT_EXCEEDED
    assert await _types(maker, oid) == ["live.order.rejected"]
    async with maker() as s:
        assert (await s.get(Portfolio, pid)).reduce_only_reason == "live_fuse"


async def test_cancel_of_sent_order_records_canceled(maker, redis, qs):
    pid, _ = await _live(maker)
    oid = await _place(maker, redis, qs, pid)
    with _mock() as mock:
        mock.post(f"{ALPACA}/v2/orders").mock(
            return_value=httpx.Response(200, json=_alpaca_order()))
        delete = mock.delete(f"{ALPACA}/v2/orders/bo-1").mock(
            return_value=httpx.Response(204))
        mock.get(f"{ALPACA}/v2/orders/bo-1").mock(
            return_value=httpx.Response(200, json=_alpaca_order("canceled")))
        sync = _sync(maker, redis, qs)
        await sync.tick()
        async with maker() as s:
            await cancel_order(s, oid, by="agent")
            await s.commit()
        await sync.tick()
    assert delete.call_count == 1
    assert (await _order(maker, oid)).status == OrderStatus.canceled
    assert await _types(maker, oid) == ["live.order.sent", "live.order.canceled"]


async def test_paper_paths_unchanged(maker, redis, qs):
    """An alpaca_paper order follows the plan-6 path: no live activity, no live audit."""
    async with maker() as s:
        p = await create_portfolio(s, name="paper", initial_cash=D(100000))
        link = await create_link(s, "paper-link", "alpaca_paper",
                                 {"key_id": "PK", "secret_key": "SK"})
        await link_portfolio(s, p.id, link.id)
        await s.commit()
    oid = await _place(maker, redis, qs, p.id)
    async with httpx.AsyncClient() as http:
        with _mock() as mock:
            post = mock.post(f"{AlpacaPaperAdapter.BASE_URL}/v2/orders").mock(
                return_value=httpx.Response(200, json=_alpaca_order()))
            await BrokerSync(maker, redis, qs, http, clock=Clock(OPEN)).tick()
    assert post.call_count == 1
    o = await _order(maker, oid)
    assert o.broker_order_id == "bo-1" and o.broker_status == "accepted"
    assert await _types(maker, oid) == [] and await _audit(maker, oid) == []
