"""Live reconciliation (positions and cash every 60 s and after each fill), account
fingerprint and health of the real-money links. Never reaches the network: the live
client's inner transport is the in-memory FakeLiveBroker, a failing MockTransport, or
respx (GET routes only)."""

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx
import pytest
import respx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from smtrading.activity.models import Activity
from smtrading.brokers import registry
from smtrading.brokers.base import BrokerAdapter
from smtrading.brokers.live.alpaca_live import AlpacaLiveAdapter
from smtrading.brokers.live.fake import FakeLiveBroker
from smtrading.brokers.live.gate import make_live_client
from smtrading.brokers.live.models import LiveState
from smtrading.brokers.live.monitor import HEALTH_FAILURES_ALERT, LiveMonitor
from smtrading.brokers.live.reconcile import (
    LIVE_CONFIG_AAD,
    account_fingerprint,
    cash_tolerance,
    reconcile_live,
)
from smtrading.brokers.models import BrokerLink, Reconciliation
from smtrading.common.crypto import encrypt_json
from smtrading.common.db import make_engine
from smtrading.engine.execution import execute_fill
from smtrading.engine.models import Order, OrderSide, OrderStatus, OrderType
from smtrading.engine.quotesource import FakeQuoteSource
from smtrading.events import discard_post_commit
from smtrading.jobs.scheduler import reconcile_paper_links
from smtrading.portfolios.models import Portfolio, PortfolioStatus
from smtrading.portfolios.service import create_portfolio
from smtrading.seed import run_seed

D = Decimal
MASTER = "test-master-key"
ALPACA_CFG = {"key_id": "CANARYKEYID0001", "secret_key": "CANARYSECRET-do-not-use"}
T212_CFG = {"api_key": "CANARYKEYID0001", "api_secret": "CANARYSECRET-do-not-use"}
ALPACA_ACCOUNT = "fake-alpaca-account"  # FakeLiveBroker's account ids
T212_ACCOUNT = "1000001"
T0 = datetime(2027, 3, 10, 9, 0, tzinfo=UTC)


class Clock:
    def __init__(self, t: datetime):
        self.t = t

    def __call__(self) -> datetime:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += timedelta(seconds=seconds)


@pytest.fixture
async def maker(fresh_db, redis_url, monkeypatch):
    monkeypatch.setenv("SMT_DATABASE_URL", fresh_db)
    monkeypatch.setenv("SMT_REDIS_URL", redis_url)
    monkeypatch.setenv("SMT_MASTER_KEY", MASTER)
    monkeypatch.setenv("SMT_ADMIN_EMAIL", "admin@example.com")
    monkeypatch.setenv("SMT_ADMIN_PASSWORD", "test-password")
    engine = make_engine(fresh_db)
    sm = async_sessionmaker(engine, expire_on_commit=False)
    async with sm() as s:
        await run_seed(s)
        await s.commit()
    yield sm
    await engine.dispose()


@pytest.fixture
def qs():
    q = FakeQuoteSource()
    q.set_fx("EUR", "USD", D("1.25"))
    q.set_quote("AAPL", D("99.90"), D("100.00"), D("99.95"))
    return q


@pytest.fixture
def fake():
    return FakeLiveBroker()


def _factory(maker, fake):
    return lambda link_id: make_live_client(maker, link_id, inner=fake.transport())


async def _live(maker, *, kind="alpaca_live", state="armed", cash="1000",
                ccy: str | None = None, account: str | None = None,
                portfolio: bool = True) -> tuple[uuid.UUID | None, uuid.UUID]:
    cfg = ALPACA_CFG if kind == "alpaca_live" else T212_CFG
    account = account or (ALPACA_ACCOUNT if kind == "alpaca_live" else T212_ACCOUNT)
    async with maker() as s:
        link = BrokerLink(
            name=f"L-{uuid.uuid4().hex[:8]}", kind=kind, live_state=state,
            config_encrypted=encrypt_json(cfg, MASTER, aad=LIVE_CONFIG_AAD),
            account_fingerprint=account_fingerprint(kind, account),
        )
        s.add(link)
        await s.flush()
        pid = None
        if portfolio:
            p = await create_portfolio(
                s, name=f"Reale {uuid.uuid4().hex[:6]}", initial_cash=D(cash),
                base_currency=ccy or ("USD" if kind == "alpaca_live" else "EUR"),
                leverage=D(1),
            )
            p.broker_link_id = link.id
            pid = p.id
        discard_post_commit(s)
        await s.commit()
        return pid, link.id


async def _get(maker, model, key):
    async with maker() as s:
        return await s.get(model, key)


async def _activities(maker, type_: str) -> list[Activity]:
    async with maker() as s:
        return list(await s.scalars(
            select(Activity).where(Activity.type == type_).order_by(Activity.id)))


async def _reconciliations(maker, link_id) -> list[Reconciliation]:
    async with maker() as s:
        return list(await s.scalars(
            select(Reconciliation).where(Reconciliation.broker_link_id == link_id)
            .order_by(Reconciliation.ts, Reconciliation.id)))


async def _fill(maker, qs, pid, link_id, qty="1") -> None:
    async with maker() as s:
        o = Order(portfolio_id=pid, symbol="AAPL", side=OrderSide.buy,
                  type=OrderType.market, qty=D(qty), status=OrderStatus.new,
                  broker_link_id=link_id)
        s.add(o)
        await s.flush()
        quote = (await qs.get_quotes(["AAPL"]))["AAPL"]
        await execute_fill(s, qs, o.id, quote=quote, source="broker", fill_qty=D(qty),
                           fill_price=D(100), final=True)
        discard_post_commit(s)
        await s.commit()


# -- pure ----------------------------------------------------------------------------

def test_cash_tolerance_formula():
    assert cash_tolerance(D("100"), D("1")) == D("0.50")
    assert cash_tolerance(D("1000"), D("1")) == D("1.000")
    assert cash_tolerance(D("100"), D("1.25")) == D("0.625")


# -- reconciliation ------------------------------------------------------------------

async def test_matching_account_is_ok_and_snapshot_saved(maker, qs, fake):
    pid, link_id = await _live(maker)
    row = await reconcile_live(maker, link_id, _factory(maker, fake), qs=qs)
    assert row is not None and row.ok and row.diffs == []
    assert row.portfolio_id == pid
    link = await _get(maker, BrokerLink, link_id)
    assert D(link.last_account_snapshot["cash_available"]) == D(1000)
    assert link.last_account_snapshot["currency"] == "USD"
    assert "raw" not in link.last_account_snapshot
    assert ALPACA_ACCOUNT not in str(link.last_account_snapshot)
    assert link.last_snapshot_at is not None
    assert (await _get(maker, Portfolio, pid)).status == PortfolioStatus.active


async def test_position_diff_trips_reduce_only_and_alert(maker, qs, fake):
    pid, link_id = await _live(maker)
    fake.alpaca.positions["AAPL"] = D(2)
    row = await reconcile_live(maker, link_id, _factory(maker, fake), qs=qs)
    assert not row.ok
    assert {"symbol": "AAPL", "ours": "0", "broker": "2"} in row.diffs
    p = await _get(maker, Portfolio, pid)
    assert p.status == PortfolioStatus.reduce_only
    assert p.reduce_only_reason == "live_reconcile_diff"
    alerts = await _activities(maker, "live.reduce_only")
    assert [a.data["cause"] for a in alerts] == ["reconcile_diff"]
    assert alerts[0].level == "critical"
    assert (await _activities(maker, "broker.reconciliation_diff")) == []


async def test_cash_shortfall_beyond_tolerance(maker, qs, fake):
    # 100 EUR: tolerance max(0,50 €, 0,1%) = 0,50
    pid, link_id = await _live(maker, kind="t212_live", cash="100")
    fake.t212.cash = D("99.51")  # 0,49 below: ok
    row = await reconcile_live(maker, link_id, _factory(maker, fake), qs=qs)
    assert row.ok, row.diffs
    assert (await _get(maker, Portfolio, pid)).status == PortfolioStatus.active

    fake.t212.cash = D("99.49")  # 0,51 below: reduce-only
    row = await reconcile_live(maker, link_id, _factory(maker, fake), qs=qs)
    assert not row.ok
    assert row.diffs == [{"cash": {"ours": "100", "broker": "99.49", "currency": "EUR",
                                   "tolerance": "0.5"}}]
    p = await _get(maker, Portfolio, pid)
    assert (p.status, p.reduce_only_reason) == (PortfolioStatus.reduce_only,
                                                "live_cash_shortfall")


async def test_broker_cash_surplus_is_ok(maker, qs, fake):
    pid, link_id = await _live(maker, cash="500")
    fake.alpaca.cash = D("5000")
    row = await reconcile_live(maker, link_id, _factory(maker, fake), qs=qs)
    assert row.ok and row.diffs == []
    assert (await _get(maker, Portfolio, pid)).status == PortfolioStatus.active


async def test_cash_converted_with_fx_for_alpaca(maker, qs, fake):
    # 100 EUR = 125 USD; tolerance max(0,50 € = 0,625 USD, 0,125) = 0,625
    pid, link_id = await _live(maker, cash="100", ccy="EUR")
    fake.alpaca.cash = D("124.40")
    assert (await reconcile_live(maker, link_id, _factory(maker, fake), qs=qs)).ok
    fake.alpaca.cash = D("124.30")
    row = await reconcile_live(maker, link_id, _factory(maker, fake), qs=qs)
    assert not row.ok
    assert row.diffs[0]["cash"]["currency"] == "USD"
    assert (await _get(maker, Portfolio, pid)).reduce_only_reason == "live_cash_shortfall"


async def test_inflight_symbol_skipped_no_false_alarm(maker, qs, fake):
    pid, link_id = await _live(maker)
    async with maker() as s:
        s.add(Order(portfolio_id=pid, symbol="AAPL", side=OrderSide.buy,
                    type=OrderType.limit, limit_price=D(90), qty=D(2),
                    status=OrderStatus.pending, broker_link_id=link_id,
                    broker_order_id="fake-9", broker_sent_at=T0))
        await s.commit()
    fake.alpaca.positions["AAPL"] = D(2)  # filled at the broker, not yet synced
    fake.alpaca.cash = D("800")
    row = await reconcile_live(maker, link_id, _factory(maker, fake), qs=qs)
    assert row.ok
    assert {"symbol": "AAPL", "skipped": "ordine in corso"} in row.diffs
    assert {"cash": {"skipped": "ordine in corso"}} in row.diffs
    assert (await _get(maker, Portfolio, pid)).status == PortfolioStatus.active
    assert await _activities(maker, "live.reduce_only") == []


async def test_link_without_portfolio_is_not_reconciled(maker, qs):
    _, link_id = await _live(maker, portfolio=False)

    def factory(link_id):
        raise AssertionError("no network without a linked portfolio")

    assert await reconcile_live(maker, link_id, factory, qs=qs) is None


async def test_fingerprint_change_detected_by_monitor(maker, qs, fake):
    pid, link_id = await _live(maker, account="another-account")
    monitor = LiveMonitor(maker, http_factory=_factory(maker, fake), qs=qs,
                          clock=Clock(T0))
    assert await monitor.tick() == 1
    link = await _get(maker, BrokerLink, link_id)
    assert link.live_state == LiveState.configured
    assert link.disarmed_reason == "conto cambiato"
    assert link.account_fingerprint == account_fingerprint("alpaca_live", "another-account")
    p = await _get(maker, Portfolio, pid)
    assert (p.status, p.reduce_only_reason) == (PortfolioStatus.reduce_only,
                                                "live_fingerprint_changed")
    assert len(await _activities(maker, "live.account_changed")) == 1
    rows = await _reconciliations(maker, link_id)
    assert [r.ok for r in rows] == [False]
    assert rows[0].diffs == [{"account": "cambiato"}]


# -- monitor -------------------------------------------------------------------------

async def test_monitor_reconciles_every_60s(maker, qs, fake):
    _, link_id = await _live(maker)
    clock = Clock(T0)
    monitor = LiveMonitor(maker, http_factory=_factory(maker, fake), qs=qs, clock=clock)
    assert await monitor.tick() == 1
    clock.advance(30)
    assert await monitor.tick() == 0
    clock.advance(30)
    assert await monitor.tick() == 1
    assert len(await _reconciliations(maker, link_id)) == 2


async def test_reconcile_after_fill_without_waiting_60s(maker, qs, fake):
    pid, link_id = await _live(maker)
    clock = Clock(T0)
    monitor = LiveMonitor(maker, http_factory=_factory(maker, fake), qs=qs, clock=clock)
    assert await monitor.tick() == 1
    clock.advance(10)
    assert await monitor.tick() == 0
    await _fill(maker, qs, pid, link_id)
    fake.alpaca.positions["AAPL"] = D(1)
    clock.advance(5)
    assert await monitor.tick() == 1
    rows = await _reconciliations(maker, link_id)
    assert [r.ok for r in rows] == [True, True]
    clock.advance(5)
    assert await monitor.tick() == 0  # that fill was already reconciled


async def test_three_health_failures_alert_once_then_reset(maker, qs, fake):
    pid, link_id = await _live(maker)
    down = {"on": True}

    def failing(request):
        raise httpx.ConnectError("connection refused")

    def factory(link_id):
        inner = httpx.MockTransport(failing) if down["on"] else fake.transport()
        return make_live_client(maker, link_id, inner=inner)

    clock = Clock(T0)
    monitor = LiveMonitor(maker, http_factory=factory, qs=qs, clock=clock)
    for expected in range(1, 5):
        await monitor.tick()
        assert (await _get(maker, BrokerLink, link_id)).health_failures == expected
        clock.advance(60)
    alerts = await _activities(maker, "live.unreachable")
    assert len(alerts) == 1
    assert alerts[0].summary == "broker non raggiungibile"
    assert alerts[0].portfolio_id == pid
    link = await _get(maker, BrokerLink, link_id)
    assert (link.live_state, link.status) == (LiveState.armed, "error")
    assert (await _get(maker, Portfolio, pid)).status == PortfolioStatus.active

    down["on"] = False
    await monitor.tick()
    link = await _get(maker, BrokerLink, link_id)
    assert (link.health_failures, link.status) == (0, "ok")

    down["on"] = True
    for _ in range(HEALTH_FAILURES_ALERT):
        clock.advance(60)
        await monitor.tick()
    assert len(await _activities(maker, "live.unreachable")) == 2  # reachable again in between


class _PaperFake(BrokerAdapter):
    kind = "fake_monitor_test"
    BASE_URL = "http://paper.invalid"

    def __init__(self, config, http):
        self.config = config

    async def get_positions(self):
        return {}


@pytest.fixture
def paper_kind():
    registry._load()
    registry.ADAPTERS[_PaperFake.kind] = _PaperFake
    yield _PaperFake.kind
    registry.ADAPTERS.pop(_PaperFake.kind, None)


async def test_paper_links_not_in_live_monitor_and_live_not_in_300s_job(
    maker, qs, fake, paper_kind
):
    _, live_id = await _live(maker)
    async with maker() as s:
        paper = BrokerLink(name="paper", kind=paper_kind)
        s.add(paper)
        await s.flush()
        p = await create_portfolio(s, name="Paper", initial_cash=D(100))
        p.broker_link_id = paper.id
        discard_post_commit(s)
        await s.commit()
        paper_id = paper.id

    seen: list[uuid.UUID] = []

    def factory(link_id):
        seen.append(link_id)
        return make_live_client(maker, link_id, inner=fake.transport())

    monitor = LiveMonitor(maker, http_factory=factory, qs=qs, clock=Clock(T0))
    assert await monitor.tick() == 1
    assert seen == [live_id]

    assert await reconcile_paper_links(maker, object()) == 0
    assert len(await _reconciliations(maker, paper_id)) == 1
    assert len(await _reconciliations(maker, live_id)) == 1  # only the monitor's
    live = await _get(maker, BrokerLink, live_id)
    assert live.status == "ok"


async def test_monitor_uses_only_get(maker, qs):
    _, link_id = await _live(maker)
    base = AlpacaLiveAdapter.BASE_URL
    with respx.mock(assert_all_mocked=True, assert_all_called=True) as mock:
        mock.get(f"{base}/v2/account").respond(200, json={
            "id": ALPACA_ACCOUNT, "currency": "USD", "cash": "1000", "buying_power": "1000",
            "multiplier": "1", "crypto_status": "INACTIVE"})
        mock.get(f"{base}/v2/positions").respond(200, json=[])
        mock.get(f"{base}/v2/orders").respond(200, json=[])
        monitor = LiveMonitor(
            maker, http_factory=lambda link_id: make_live_client(maker, link_id), qs=qs,
            clock=Clock(T0))
        assert await monitor.tick() == 1
        assert {c.request.method for c in mock.calls} == {"GET"}
    assert (await _reconciliations(maker, link_id))[0].ok
