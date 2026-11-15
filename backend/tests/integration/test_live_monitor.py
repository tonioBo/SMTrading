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
from smtrading.brokers.live import gate
from smtrading.brokers.live.account import LIVE_AAD, account_fingerprint
from smtrading.brokers.live.alpaca_live import AlpacaLiveAdapter
from smtrading.brokers.live.fake import FakeLiveBroker
from smtrading.brokers.live.gate import make_live_client
from smtrading.brokers.live.models import LiveAudit, LiveState
from smtrading.brokers.live.monitor import HEALTH_FAILURES_ALERT, LiveMonitor
from smtrading.brokers.live.reconcile import cash_tolerance, reconcile_live
from smtrading.brokers.models import BrokerLink, Reconciliation
from smtrading.brokers.sync import reconcile, reconcile_all
from smtrading.common.crypto import encrypt_json
from smtrading.common.db import make_engine
from smtrading.common.errors import ErrorCode
from smtrading.engine.execution import execute_fill
from smtrading.engine.models import BrokerState, Order, OrderSide, OrderStatus, OrderType
from smtrading.engine.quotesource import FakeQuoteSource
from smtrading.events import discard_post_commit
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
            config_encrypted=encrypt_json(cfg, MASTER, aad=LIVE_AAD),
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
    """R8.7: recorded on the first pass, reduce-only when the next pass finds it again."""
    pid, link_id = await _live(maker)
    fake.alpaca.positions["AAPL"] = D(2)
    row = await reconcile_live(maker, link_id, _factory(maker, fake), qs=qs)
    assert not row.ok
    assert {"symbol": "AAPL", "ours": "0", "broker": "2"} in row.diffs
    assert (await _get(maker, Portfolio, pid)).status == PortfolioStatus.active
    assert await _activities(maker, "live.reduce_only") == []
    assert [r.ok for r in await _reconciliations(maker, link_id)] == [False]
    row = await reconcile_live(maker, link_id, _factory(maker, fake), qs=qs)
    assert not row.ok
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

    fake.t212.cash = D("99.49")  # 0,51 below: reduce-only (on the second pass)
    row = await reconcile_live(maker, link_id, _factory(maker, fake), qs=qs)
    assert not row.ok
    assert (await _get(maker, Portfolio, pid)).status == PortfolioStatus.active
    row = await reconcile_live(maker, link_id, _factory(maker, fake), qs=qs)
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
    # 100 EUR = 125 USD; tolerance max(0,50 € = 0,625 USD, 0,125) = 0,625, plus the FX
    # band (R8.5): 1 % of the EUR cash, 1,25 USD -> 1,875
    pid, link_id = await _live(maker, cash="100", ccy="EUR")
    fake.alpaca.cash = D("123.20")
    assert (await reconcile_live(maker, link_id, _factory(maker, fake), qs=qs)).ok
    fake.alpaca.cash = D("123.10")
    row = await reconcile_live(maker, link_id, _factory(maker, fake), qs=qs)
    assert not row.ok and row.diffs[0]["cash"]["tolerance"] == "1.875"
    row = await reconcile_live(maker, link_id, _factory(maker, fake), qs=qs)
    assert row.diffs[0]["cash"]["currency"] == "USD"
    assert (await _get(maker, Portfolio, pid)).reduce_only_reason == "live_cash_shortfall"


async def test_inflight_buy_explained_by_the_band_no_false_alarm(maker, qs, fake):
    """R8.8: a buy of 2 at the broker (filled there, not yet synced): ours 0 is within
    [2 - 2, 2]; the cash band counts what the open buy may cost (2 x 90 + 2 %)."""
    pid, link_id = await _live(maker)
    async with maker() as s:
        s.add(Order(portfolio_id=pid, symbol="AAPL", side=OrderSide.buy,
                    type=OrderType.limit, limit_price=D(90), qty=D(2),
                    status=OrderStatus.pending, broker_link_id=link_id,
                    broker_order_id="fake-9", broker_sent_at=T0))
        await s.commit()
    fake.alpaca.positions["AAPL"] = D(2)  # filled at the broker, not yet synced
    fake.alpaca.cash = D("820")
    row = await reconcile_live(maker, link_id, _factory(maker, fake), qs=qs)
    assert row.ok
    (entry,) = row.diffs
    assert {k: entry[k] for k in ("symbol", "ours", "broker", "explained")} == {
        "symbol": "AAPL", "ours": "0", "broker": "2", "explained": "ordine in corso"}
    row = await reconcile_live(maker, link_id, _factory(maker, fake), qs=qs)
    assert row.ok
    assert (await _get(maker, Portfolio, pid)).status == PortfolioStatus.active
    assert await _activities(maker, "live.reduce_only") == []


async def test_link_without_portfolio_is_not_reconciled(maker, qs):
    _, link_id = await _live(maker, portfolio=False)

    def factory(link_id):
        raise AssertionError("no network without a linked portfolio")

    assert await reconcile_live(maker, link_id, factory, qs=qs) is None


async def test_fingerprint_change_detected_by_monitor(maker, qs, fake):
    pid, link_id = await _live(maker, account="another-account")
    async with maker() as s:  # never sent: rejected by the disarm (Task 5's flow)
        unsent = Order(portfolio_id=pid, symbol="AAPL", side=OrderSide.buy,
                       type=OrderType.market, qty=D(1), status=OrderStatus.pending,
                       broker_link_id=link_id, broker_status=BrokerState.TO_SEND)
        s.add(unsent)
        await s.commit()
        unsent_id = unsent.id
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
    # M4: one critical alert, not a disarm or a reduce-only one besides it
    assert await _activities(maker, "live.disarmed") == []
    assert await _activities(maker, "live.reduce_only") == []
    rows = await _reconciliations(maker, link_id)
    assert [r.ok for r in rows] == [False]
    assert rows[0].diffs == [{"account": "cambiato"}]
    # disarmed through disarm_live_link: audit (actor system), critical alert, orders
    o = await _get(maker, Order, unsent_id)
    assert (o.status, o.reject_code) == (OrderStatus.rejected, ErrorCode.LIVE_NOT_ARMED)
    async with maker() as s:
        audit = list(await s.scalars(select(LiveAudit).where(
            LiveAudit.link_id == link_id, LiveAudit.action == "disarmed")))
    assert [(a.actor, a.details["reason"]) for a in audit] == [("system", "conto cambiato")]
    # the next pass: already configured, no second disarm nor alert
    monitor._clock.advance(60)
    assert await monitor.tick() == 1
    assert len(await _activities(maker, "live.account_changed")) == 1
    assert len(await _reconciliations(maker, link_id)) == 1


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
    assert len(await _reconciliations(maker, link_id)) == 1  # unchanged: one row


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
    assert [r.ok for r in rows] == [True]  # still consistent: no new row
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

    assert await reconcile_all(maker, object()) == 0
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


async def test_reconcile_of_a_live_link_closes_the_client_it_builds(maker, qs, fake,
                                                                   monkeypatch):
    """brokers.sync.reconcile (admin «Riconcilia ora») on a live link: get_adapter builds
    a LiveClient of its own; it is closed after use (no leaked clients)."""
    pid, link_id = await _live(maker)
    built: list[httpx.AsyncClient] = []
    real = gate.make_live_client

    def recording(sm, lid, **kw):
        client = real(sm, lid, inner=fake.transport())
        built.append(client)
        return client

    monkeypatch.setattr(gate, "make_live_client", recording)
    async with httpx.AsyncClient() as http:
        rows = await reconcile(maker, link_id, http)
        assert not http.is_closed  # the caller's client is the caller's
    assert [r.portfolio_id for r in rows] == [pid]
    assert len(built) == 1 and built[0].is_closed


# -- fix round T5-T7 (plan 8) -------------------------------------------------------------

async def _sent(maker, pid, link_id, *, side=OrderSide.buy, qty="2", price="90"):
    async with maker() as s:
        s.add(Order(portfolio_id=pid, symbol="AAPL", side=side, type=OrderType.limit,
                    limit_price=D(price), qty=D(qty), status=OrderStatus.pending,
                    broker_link_id=link_id, broker_order_id=f"fake-{uuid.uuid4().hex[:6]}",
                    broker_sent_at=T0))
        await s.commit()


async def test_quantity_epsilon(maker, qs, fake):
    """R8.7: a difference of at most 1e-8 is no difference."""
    pid, link_id = await _live(maker)
    await _fill(maker, qs, pid, link_id)
    fake.alpaca.positions["AAPL"] = D("1.000000005")
    fake.alpaca.cash = D("900")
    row = await reconcile_live(maker, link_id, _factory(maker, fake), qs=qs)
    assert row.ok and row.diffs == []


async def test_band_with_an_open_sell_and_beyond_it(maker, qs, fake):
    """R8.8: holding 1 with a sell of 1 at the broker: 0 there is explained; 3 there is
    outside [3 - 0, 3 + 1] for ours 1: a diff (reduce-only on the second pass)."""
    pid, link_id = await _live(maker)
    await _fill(maker, qs, pid, link_id)
    await _sent(maker, pid, link_id, side=OrderSide.sell, qty="1", price="110")
    fake.alpaca.cash = D("900")
    row = await reconcile_live(maker, link_id, _factory(maker, fake), qs=qs)  # broker 0
    assert row.ok and row.diffs[0]["explained"] == "ordine in corso"
    fake.alpaca.positions["AAPL"] = D(3)
    for _ in range(2):
        row = await reconcile_live(maker, link_id, _factory(maker, fake), qs=qs)
    assert not row.ok and {"symbol": "AAPL", "ours": "1", "broker": "3"} in row.diffs
    assert (await _get(maker, Portfolio, pid)).reduce_only_reason == "live_reconcile_diff"


async def test_open_sell_never_skips_the_cash_check(maker, qs, fake):
    """R8.8: an open sell at the broker does not hide a cash shortfall."""
    pid, link_id = await _live(maker)
    await _fill(maker, qs, pid, link_id)
    await _sent(maker, pid, link_id, side=OrderSide.sell, qty="1", price="110")
    fake.alpaca.positions["AAPL"] = D(1)
    fake.alpaca.cash = D("850")  # ours 900
    for _ in range(2):
        row = await reconcile_live(maker, link_id, _factory(maker, fake), qs=qs)
    assert not row.ok and row.diffs[-1]["cash"]["broker"] == "850"
    assert (await _get(maker, Portfolio, pid)).reduce_only_reason == "live_cash_shortfall"


async def test_unexplained_for_over_30_min_alerts_once(maker, qs, fake):
    """R8.8: an explained difference (or a skipped check) lasting > 30 min: one critical
    alert per episode; its `since` is carried across passes."""
    pid, link_id = await _live(maker)
    await _sent(maker, pid, link_id)
    fake.alpaca.positions["AAPL"] = D(2)
    fake.alpaca.cash = D("820")
    row = await reconcile_live(maker, link_id, _factory(maker, fake), qs=qs)
    since = row.diffs[0]["since"]
    row = await reconcile_live(maker, link_id, _factory(maker, fake), qs=qs)
    assert row.diffs[0]["since"] == since
    assert await _activities(maker, "live.reconcile_unexplained") == []
    old = (datetime.fromisoformat(since) - timedelta(minutes=31)).isoformat()
    async with maker() as s:  # as if the first pass had been 31 minutes ago
        (stored,) = await s.scalars(select(Reconciliation).where(
            Reconciliation.broker_link_id == link_id))
        stored.diffs = [{**stored.diffs[0], "since": old}]
        await s.commit()
    for _ in range(2):
        await reconcile_live(maker, link_id, _factory(maker, fake), qs=qs)
    (alert,) = await _activities(maker, "live.reconcile_unexplained")
    assert alert.level == "critical" and alert.data["key"] == "symbol:AAPL"
    assert alert.portfolio_id == pid


async def test_missing_fx_rate_skips_cash_then_alerts_after_30_min(maker, fake):
    """Minor: no EUR rate for a EUR portfolio on a USD account: the cash is skipped, and
    an alert follows after 30 minutes of it."""
    _, link_id = await _live(maker, cash="100", ccy="EUR")
    row = await reconcile_live(maker, link_id, _factory(maker, fake), qs=FakeQuoteSource())
    assert row.ok and row.diffs[0]["cash"] == {"skipped": "cambio non disponibile"}
    async with maker() as s:
        (stored,) = await s.scalars(select(Reconciliation).where(
            Reconciliation.broker_link_id == link_id))
        since = datetime.fromisoformat(stored.diffs[0]["since"]) - timedelta(minutes=31)
        stored.diffs = [{**stored.diffs[0], "since": since.isoformat()}]
        await s.commit()
    await reconcile_live(maker, link_id, _factory(maker, fake), qs=FakeQuoteSource())
    (alert,) = await _activities(maker, "live.reconcile_unexplained")
    assert alert.data["key"] == "cash"


async def test_rows_only_on_change_plus_hourly_heartbeat(maker, qs, fake):
    _, link_id = await _live(maker)
    for _ in range(3):
        await reconcile_live(maker, link_id, _factory(maker, fake), qs=qs)
    rows = await _reconciliations(maker, link_id)
    assert len(rows) == 1
    async with maker() as s:
        row = await s.get(Reconciliation, rows[0].id)
        row.ts = row.ts - timedelta(minutes=61)
        await s.commit()
    await reconcile_live(maker, link_id, _factory(maker, fake), qs=qs)
    assert len(await _reconciliations(maker, link_id)) == 2  # the heartbeat
    fake.alpaca.positions["AAPL"] = D(1)  # a change: a row at once
    await reconcile_live(maker, link_id, _factory(maker, fake), qs=qs)
    assert len(await _reconciliations(maker, link_id)) == 3


class _MarginFake(FakeLiveBroker):
    def _alpaca(self, method, path, params, body):
        resp = super()._alpaca(method, path, params, body)
        if (method, path) == ("GET", "/v2/account"):
            return httpx.Response(200, json={**resp.json(), "multiplier": "2"})
        return resp


async def test_alpaca_margin_account_trips_reduce_only(maker, qs):
    """R8.10: the account became a margin account: reduce-only with a critical alert."""
    pid, link_id = await _live(maker)
    await reconcile_live(maker, link_id, _factory(maker, _MarginFake()), qs=qs)
    p = await _get(maker, Portfolio, pid)
    assert (p.status, p.reduce_only_reason) == (PortfolioStatus.reduce_only,
                                                "live_margin_account")
    assert [a.data["cause"] for a in await _activities(maker, "live.reduce_only")] == [
        "margin_account"]


def _status_factory(maker, status: int):
    def handler(request):
        return httpx.Response(status, json={"message": "no"})

    return lambda link_id: make_live_client(maker, link_id, inner=httpx.MockTransport(handler))


async def test_credentials_rejected_has_its_own_alert(maker, qs):
    """Minor: HTTP 401: «credenziali rifiutate» at the first failure, once; no
    `unreachable` alert for it."""
    _, link_id = await _live(maker)
    clock = Clock(T0)
    monitor = LiveMonitor(maker, http_factory=_status_factory(maker, 401), qs=qs, clock=clock)
    for _ in range(HEALTH_FAILURES_ALERT + 1):
        await monitor.tick()
        clock.advance(60)
    (alert,) = await _activities(maker, "live.credentials_rejected")
    assert "credenziali rifiutate" in alert.summary
    assert await _activities(maker, "live.unreachable") == []
    assert (await _get(maker, BrokerLink, link_id)).health_failures == HEALTH_FAILURES_ALERT + 1


async def test_rate_limit_is_not_a_health_failure(maker, qs):
    _, link_id = await _live(maker)
    clock = Clock(T0)
    monitor = LiveMonitor(maker, http_factory=_status_factory(maker, 429), qs=qs, clock=clock)
    for _ in range(HEALTH_FAILURES_ALERT + 1):
        await monitor.tick()
        clock.advance(60)
    assert (await _get(maker, BrokerLink, link_id)).health_failures == 0
    assert await _activities(maker, "live.unreachable") == []


async def test_internal_failures_alert_after_three(maker, qs, fake, monkeypatch):
    """Minor: a failure of the monitor itself (not the broker): the link waits its next
    turn (`_last`), and the third one in a row sends a critical alert."""
    from smtrading.brokers.live import monitor as monitor_module

    await _live(maker)
    calls = []

    async def broken(*a, **kw):
        calls.append(1)
        raise ValueError("bug")

    monkeypatch.setattr(monitor_module, "reconcile_live", broken)
    clock = Clock(T0)
    monitor = LiveMonitor(maker, http_factory=_factory(maker, fake), qs=qs, clock=clock)
    await monitor.tick()
    clock.advance(10)
    await monitor.tick()  # not due again yet
    assert len(calls) == 1
    for _ in range(HEALTH_FAILURES_ALERT):
        clock.advance(60)
        await monitor.tick()
    (alert,) = await _activities(maker, "live.monitor_failed")
    assert alert.level == "critical"


async def test_t212_positions_read_once_per_pass(maker, qs):
    class Counting(FakeLiveBroker):
        positions_reads = 0

        def _t212(self, method, path, body):
            if (method, path) == ("GET", "/api/v0/equity/positions"):
                Counting.positions_reads += 1
            return super()._t212(method, path, body)

    fake = Counting()
    _, link_id = await _live(maker, kind="t212_live")
    assert (await reconcile_live(maker, link_id, _factory(maker, fake), qs=qs)).ok
    assert Counting.positions_reads == 1


async def test_a_send_after_the_read_keeps_the_snapshot_stale(maker, qs, fake):
    """C1: a buy sent after the monitor read the account (snapshot marked stale) is not
    hidden by the monitor's older answer: the stale mark stays."""
    from smtrading.risk.live_checks import STALE_KEY

    _, link_id = await _live(maker)
    future = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    async with maker() as s:
        link = await s.get(BrokerLink, link_id)
        link.last_account_snapshot = {"currency": "USD", "cash_available": "1", STALE_KEY: future}
        await s.commit()
    await reconcile_live(maker, link_id, _factory(maker, fake), qs=qs)
    assert (await _get(maker, BrokerLink, link_id)).last_account_snapshot[STALE_KEY] == future
    past = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
    async with maker() as s:
        link = await s.get(BrokerLink, link_id)
        link.last_account_snapshot = {**link.last_account_snapshot, STALE_KEY: past}
        await s.commit()
    await reconcile_live(maker, link_id, _factory(maker, fake), qs=qs)
    assert STALE_KEY not in (await _get(maker, BrokerLink, link_id)).last_account_snapshot


async def test_reconcile_locks_the_link_before_the_portfolio(maker, qs, fake):
    """R8.9: link FOR NO KEY UPDATE, then the portfolio (trip_reduce_only)."""
    from sqlalchemy import event

    _, link_id = await _live(maker)
    fake.alpaca.positions["AAPL"] = D(2)
    await reconcile_live(maker, link_id, _factory(maker, fake), qs=qs)
    seen: list[str] = []

    def record(conn, cursor, statement, *args):
        if "FOR NO KEY UPDATE" in statement and "broker_links" in statement:
            seen.append("link")
        elif "FOR UPDATE" in statement and "FROM portfolios" in statement:
            seen.append("portfolio")

    engine = maker.kw["bind"].sync_engine
    event.listen(engine, "before_cursor_execute", record)
    try:
        await reconcile_live(maker, link_id, _factory(maker, fake), qs=qs)  # trips now
    finally:
        event.remove(engine, "before_cursor_execute", record)
    assert seen[:2] == ["link", "portfolio"], seen



# -- re-review (M-a, M-c, M-f, M-g) -------------------------------------------------------

async def test_debounce_by_symbol_and_sign(maker, qs, fake):
    """M-a: the broker's quantity moves between the passes (2 then 3, both above ours):
    the same diff for the debounce: reduce-only on the second pass."""
    pid, link_id = await _live(maker)
    fake.alpaca.positions["AAPL"] = D(2)
    await reconcile_live(maker, link_id, _factory(maker, fake), qs=qs)
    assert (await _get(maker, Portfolio, pid)).status == PortfolioStatus.active
    fake.alpaca.positions["AAPL"] = D(3)
    await reconcile_live(maker, link_id, _factory(maker, fake), qs=qs)
    assert (await _get(maker, Portfolio, pid)).reduce_only_reason == "live_reconcile_diff"


class _NoMultiplierFake(FakeLiveBroker):
    def _alpaca(self, method, path, params, body):
        resp = super()._alpaca(method, path, params, body)
        if (method, path) == ("GET", "/v2/account"):
            data = resp.json()
            data.pop("multiplier")
            return httpx.Response(200, json=data)
        return resp


async def test_alpaca_account_without_multiplier_trips_reduce_only(maker, qs):
    """M-c: not provably a cash account (no multiplier): reduce-only too."""
    pid, link_id = await _live(maker)
    await reconcile_live(maker, link_id, _factory(maker, _NoMultiplierFake()), qs=qs)
    assert (await _get(maker, Portfolio, pid)).reduce_only_reason == "live_margin_account"


async def test_rate_limit_for_over_15_min_alerts_once(maker, qs):
    """M-f: a rate limit lasting more than 15 minutes: one alert per episode."""
    _, link_id = await _live(maker)
    clock = Clock(T0)
    monitor = LiveMonitor(maker, http_factory=_status_factory(maker, 429), qs=qs, clock=clock)
    for _ in range(14):
        await monitor.tick()
        clock.advance(60)
    assert await _activities(maker, "live.unreachable") == []
    for _ in range(10):
        await monitor.tick()
        clock.advance(60)
    (alert,) = await _activities(maker, "live.unreachable")
    assert "limite di frequenza" in alert.summary
    assert (await _get(maker, BrokerLink, link_id)).health_failures == 0


async def test_t212_account_positions_are_consumed_not_reused(maker, monkeypatch):
    """M-g: positions_after_account reuses the account read once, and only when recent."""
    from smtrading.brokers import t212_core
    from smtrading.brokers.live.t212_live import T212LiveAdapter

    class Counting(FakeLiveBroker):
        reads = 0

        def _t212(self, method, path, body):
            if (method, path) == ("GET", "/api/v0/equity/positions"):
                Counting.reads += 1
            return super()._t212(method, path, body)

    _, link_id = await _live(maker, kind="t212_live")
    async with make_live_client(maker, link_id, inner=Counting().transport()) as http:
        adapter = T212LiveAdapter(dict(T212_CFG), http)
        await adapter.get_account()
        await adapter.positions_after_account()
        assert Counting.reads == 1  # reused once
        await adapter.positions_after_account()
        assert Counting.reads == 2  # consumed: read again
        await adapter.get_account()
        monkeypatch.setattr(t212_core, "_ACCOUNT_POSITIONS_MAX_AGE_S", -1)
        await adapter.positions_after_account()
        assert Counting.reads == 4  # too old: read again


# -- follow-up FR2 ---------------------------------------------------------------------

async def test_reconcile_survives_an_account_change(maker, qs, fake):
    """FR2 I1: after an account change (row diffs [{"account": "cambiato"}]) and a new
    test (the fingerprint now matches), the next passes reconcile normally: that row is
    not a previous diff (no debounce against it), a normal row is written each time."""
    pid, link_id = await _live(maker, account="another-account")
    monitor = LiveMonitor(maker, http_factory=_factory(maker, fake), qs=qs, clock=Clock(T0))
    assert await monitor.tick() == 1
    assert [r.diffs for r in await _reconciliations(maker, link_id)] == [
        [{"account": "cambiato"}]]
    async with maker() as s:  # a new test of the keys stores the new fingerprint
        link = await s.get(BrokerLink, link_id)
        link.account_fingerprint = account_fingerprint("alpaca_live", ALPACA_ACCOUNT)
        await s.commit()
    fake.alpaca.positions["AAPL"] = D(2)
    row = await reconcile_live(maker, link_id, _factory(maker, fake), qs=qs)
    assert not row.ok and {"symbol": "AAPL", "ours": "0", "broker": "2"} in row.diffs
    assert await _activities(maker, "live.reduce_only") == []  # a first pass only
    fake.alpaca.positions.pop("AAPL")
    row = await reconcile_live(maker, link_id, _factory(maker, fake), qs=qs)
    assert row.ok and row.diffs == []
    rows = await _reconciliations(maker, link_id)
    assert [r.ok for r in rows] == [False, False, True]
    assert len(await _activities(maker, "live.account_changed")) == 1
    assert (await _get(maker, Portfolio, pid)).status == PortfolioStatus.reduce_only


async def test_reconcile_after_account_change_debounces_from_scratch(maker, qs, fake):
    """FR2 I1: a diff found right after the account-changed row is a first pass (R8.7):
    the second pass with the same diff confirms it."""
    pid, link_id = await _live(maker, account="another-account")
    await reconcile_live(maker, link_id, _factory(maker, fake), qs=qs)
    async with maker() as s:
        link = await s.get(BrokerLink, link_id)
        link.account_fingerprint = account_fingerprint("alpaca_live", ALPACA_ACCOUNT)
        from smtrading.portfolios.service import set_portfolio_status

        # the admin resolved the account change (as resume_live_portfolio does)
        set_portfolio_status(s, await s.get(Portfolio, pid), PortfolioStatus.active,
                             allow_live=True)
        await s.commit()
    fake.alpaca.positions["AAPL"] = D(2)
    await reconcile_live(maker, link_id, _factory(maker, fake), qs=qs)
    assert (await _get(maker, Portfolio, pid)).status == PortfolioStatus.active
    await reconcile_live(maker, link_id, _factory(maker, fake), qs=qs)
    assert (await _get(maker, Portfolio, pid)).reduce_only_reason == "live_reconcile_diff"


async def test_monitor_prunes_the_state_of_links_no_longer_live(maker, qs, fake):
    """FR2 M5: the per-link maps (rate limit episode, internal failures, auth alert, last
    pass) only keep links still live and enabled."""
    _, link_id = await _live(maker)
    clock = Clock(T0)
    monitor = LiveMonitor(maker, http_factory=_status_factory(maker, 429), qs=qs, clock=clock)
    await monitor.tick()
    assert link_id in monitor._rate_limited and link_id in monitor._last
    monitor._internal[link_id] = 2
    monitor._auth_alerted.add(link_id)
    async with maker() as s:
        (await s.get(BrokerLink, link_id)).live_state = None  # unlinked from real money
        await s.commit()
    clock.advance(60)
    await monitor.tick()
    assert link_id not in monitor._rate_limited
    assert link_id not in monitor._internal
    assert link_id not in monitor._auth_alerted
    assert link_id not in monitor._last


async def test_monitor_forgets_the_link_state_when_no_portfolio_is_linked(maker, qs, fake):
    """FR2 M5: a live link whose portfolio was unlinked (no broker call): its failure and
    alert state is dropped too (a new episode starts if it is linked again)."""
    pid, link_id = await _live(maker)
    monitor = LiveMonitor(maker, http_factory=_factory(maker, fake), qs=qs, clock=Clock(T0))
    monitor._internal[link_id] = 2
    monitor._auth_alerted.add(link_id)
    monitor._rate_limited[link_id] = (T0, False)
    async with maker() as s:
        (await s.get(Portfolio, pid)).broker_link_id = None
        await s.commit()
    assert await monitor.tick() == 0
    assert link_id not in monitor._internal
    assert link_id not in monitor._auth_alerted
    assert link_id not in monitor._rate_limited


# -- several currencies (plan 9, Task 7): FX-only drift is an alert, never reduce-only ----

def _eur_usd(rate: str) -> FakeQuoteSource:
    q = FakeQuoteSource()
    q.set_fx("EUR", "USD", D(rate))
    q.set_quote("AAPL", D("99.90"), D("100.00"), D("99.95"))
    return q


async def _ref(maker, link_id) -> dict[str, Decimal] | None:
    refs = (await _get(maker, BrokerLink, link_id)).fx_ref_rates
    return None if refs is None else {c: D(r) for c, r in refs.items()}


async def _drifted(maker, fake) -> tuple[uuid.UUID, uuid.UUID]:
    """1000 EUR on a USD account holding 1250 USD, matched at 1.25 (the reference)."""
    pid, link_id = await _live(maker, cash="1000", ccy="EUR")
    fake.alpaca.cash = D("1250")
    assert (await reconcile_live(maker, link_id, _factory(maker, fake),
                                 qs=_eur_usd("1.25"))).ok
    return pid, link_id


async def test_fx_only_drift_alerts_once_per_day_no_reduce_only(maker, fake):
    """At 1.30 our 1000 EUR are 1300 USD: 1250 USD is short by more than the tolerance
    (14.30) but within it plus the drift (1000 x 0.05 = 50 USD): one alert per day, the
    portfolio stays active, the row is ok."""
    pid, link_id = await _drifted(maker, fake)
    for _ in range(2):
        row = await reconcile_live(maker, link_id, _factory(maker, fake), qs=_eur_usd("1.30"))
        assert row.ok
        assert row.diffs == [{"cash": {"fx_drift": "50", "ours": "1300", "broker": "1250",
                                       "currency": "USD"}}]
    (alert,) = await _activities(maker, "live.fx_drift")
    assert alert.portfolio_id == pid and alert.level == "critical"
    assert alert.summary == ("Scarto di cambio sul portafoglio reale: 1300.00 contro"
                             " 1250.00 USD")
    p = await _get(maker, Portfolio, pid)
    assert p.status == PortfolioStatus.active and p.reduce_only_reason is None
    assert await _ref(maker, link_id) == {"EUR": D("1.25")}  # not a matching pass

    async with maker() as s:  # the next local day: one more
        a = await s.get(Activity, alert.id)
        a.ts = a.ts - timedelta(days=1)
        await s.commit()
    await reconcile_live(maker, link_id, _factory(maker, fake), qs=_eur_usd("1.30"))
    assert len(await _activities(maker, "live.fx_drift")) == 2


async def test_drift_plus_real_shortfall_trips_after_debounce(maker, fake):
    """1200 USD at 1.30: short beyond the tolerance plus the FX drift (1235.70)."""
    pid, link_id = await _drifted(maker, fake)
    fake.alpaca.cash = D("1200")
    row = await reconcile_live(maker, link_id, _factory(maker, fake), qs=_eur_usd("1.30"))
    assert not row.ok and "fx_drift" not in row.diffs[0]["cash"]
    assert (await _get(maker, Portfolio, pid)).status == PortfolioStatus.active
    await reconcile_live(maker, link_id, _factory(maker, fake), qs=_eur_usd("1.30"))
    p = await _get(maker, Portfolio, pid)
    assert (p.status, p.reduce_only_reason) == (PortfolioStatus.reduce_only,
                                                "live_cash_shortfall")
    assert await _activities(maker, "live.fx_drift") == []


async def test_drift_with_a_position_diff_is_a_cash_shortfall(maker, fake):
    """The drift excuses the cash only when the positions match."""
    _, link_id = await _drifted(maker, fake)
    fake.alpaca.positions["AAPL"] = D(3)
    row = await reconcile_live(maker, link_id, _factory(maker, fake), qs=_eur_usd("1.30"))
    assert not row.ok
    assert next(d for d in row.diffs if "cash" in d)["cash"]["tolerance"] == "14.3"
    assert await _activities(maker, "live.fx_drift") == []


async def test_fx_ref_updated_only_on_ok_pass(maker, fake):
    _, link_id = await _live(maker, cash="1000", ccy="EUR")
    assert await _ref(maker, link_id) is None
    fake.alpaca.cash = D("1250")
    await reconcile_live(maker, link_id, _factory(maker, fake), qs=_eur_usd("1.25"))
    assert await _ref(maker, link_id) == {"EUR": D("1.25")}
    await reconcile_live(maker, link_id, _factory(maker, fake), qs=_eur_usd("1.30"))  # drift
    assert await _ref(maker, link_id) == {"EUR": D("1.25")}
    fake.alpaca.cash = D("1100")  # a real shortfall
    await reconcile_live(maker, link_id, _factory(maker, fake), qs=_eur_usd("1.30"))
    assert await _ref(maker, link_id) == {"EUR": D("1.25")}
    fake.alpaca.cash = D("1300")  # matching again at 1.30
    assert (await reconcile_live(maker, link_id, _factory(maker, fake),
                                 qs=_eur_usd("1.30"))).ok
    assert await _ref(maker, link_id) == {"EUR": D("1.30")}
    await reconcile_live(maker, link_id, _factory(maker, fake), qs=FakeQuoteSource())  # no FX
    assert await _ref(maker, link_id) == {"EUR": D("1.30")}


async def test_reconcile_no_fx_skipped_no_alert(maker, fake):
    """Review Focus 3: no FX rate on a EUR portfolio on a USD account: the cash is
    skipped, no reduce-only, no FX drift alert, the reference left alone."""
    pid, link_id = await _drifted(maker, fake)
    fake.alpaca.cash = D("900")
    for _ in range(2):
        row = await reconcile_live(maker, link_id, _factory(maker, fake), qs=FakeQuoteSource())
        assert row.ok and row.diffs[0]["cash"]["skipped"] == "cambio non disponibile"
    assert (await _get(maker, Portfolio, pid)).status == PortfolioStatus.active
    assert await _activities(maker, "live.fx_drift") == []
    assert await _ref(maker, link_id) == {"EUR": D("1.25")}


def _rates(eur: str, gbp: str) -> FakeQuoteSource:
    q = _eur_usd(eur)
    q.set_fx("GBP", "USD", D(gbp))
    return q


async def test_opposite_fx_moves_cancel_and_a_real_loss_trips(maker, fake):
    """R9.2: 1000 EUR + 1000 GBP matched at 1.25 / 1.50 (2750 USD); EUR to 1.30 and GBP
    to 1.45 cancel out (signed drift 0), so 80 USD missing is a real shortfall."""
    from smtrading.portfolios.service import deposit

    pid, link_id = await _live(maker, cash="1000", ccy="EUR")
    async with maker() as s:
        await deposit(s, pid, D(1000), "GBP", author_id=None)
        discard_post_commit(s)
        await s.commit()
    fake.alpaca.cash = D("2750")
    assert (await reconcile_live(maker, link_id, _factory(maker, fake),
                                 qs=_rates("1.25", "1.50"))).ok
    fake.alpaca.cash = D("2670")
    for _ in range(2):
        row = await reconcile_live(maker, link_id, _factory(maker, fake),
                                   qs=_rates("1.30", "1.45"))
        assert not row.ok and "fx_drift" not in row.diffs[0]["cash"]
    p = await _get(maker, Portfolio, pid)
    assert (p.status, p.reduce_only_reason) == (PortfolioStatus.reduce_only,
                                                "live_cash_shortfall")
    assert await _activities(maker, "live.fx_drift") == []


async def test_adverse_fx_move_never_explains_a_loss(maker, fake):
    """R9.2: EUR falls 1.25 -> 1.20 (our 1000 EUR lose D = 50 USD) and the broker lost
    L = 80 USD, with D + tolerance (63.20) < L <= 2D + tolerance (113.20): a shortfall."""
    pid, link_id = await _drifted(maker, fake)
    fake.alpaca.cash = D("1170")
    for _ in range(2):
        row = await reconcile_live(maker, link_id, _factory(maker, fake), qs=_eur_usd("1.20"))
        assert not row.ok and row.diffs[0]["cash"]["tolerance"] == "13.2"
    p = await _get(maker, Portfolio, pid)
    assert (p.status, p.reduce_only_reason) == (PortfolioStatus.reduce_only,
                                                "live_cash_shortfall")
    assert await _activities(maker, "live.fx_drift") == []
