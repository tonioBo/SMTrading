"""Reconciliation with the broker (plan 21, smtrading.reconciliation): positions, cash and
open orders of the linked portfolios against a fake broker account; the 2-check rule,
alert dedup, an unreadable broker, restarts and the job's schedule."""

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import ClassVar

import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import async_sessionmaker

from smtrading.activity.models import Activity
from smtrading.brokers import registry
from smtrading.brokers.base import AccountSnapshot, BrokerAdapter, BrokerOpenOrder
from smtrading.brokers.service import create_link, link_portfolio
from smtrading.common.db import make_engine
from smtrading.common.fingerprints import FINGERPRINT
from smtrading.engine.execution import execute_fill
from smtrading.engine.models import BrokerState, Order, OrderSide, OrderStatus, OrderType
from smtrading.engine.quotesource import FakeQuoteSource
from smtrading.events import discard_post_commit
from smtrading.instruments.models import AssetClass
from smtrading.ledger.models import Account as LedgerAccount
from smtrading.ledger.models import AccountBalance, AccountKind
from smtrading.portfolios.models import Portfolio, PortfolioStatus
from smtrading.portfolios.service import create_portfolio
from smtrading.reconciliation import job as job_module
from smtrading.reconciliation.job import ReconciliationJob, due
from smtrading.reconciliation.models import ReconciliationCheck
from smtrading.reconciliation.service import ALERT_TYPE, READ_ONLY_MESSAGE, check_all, check_link
from smtrading.risk.models import AppSetting
from smtrading.seed import run_seed
from smtrading.telegram.models import TelegramOutbox

D = Decimal
T0 = datetime(2026, 11, 10, 15, 0, tzinfo=UTC)  # Tuesday 16:00 Rome: sessions open


class Account:
    """The fake broker account, shared by every FakeReconAdapter instance."""

    def __init__(self) -> None:
        self.currency = "EUR"
        self.cash = D(100000)
        self.positions: dict[str, Decimal] = {}
        self.orders: list[BrokerOpenOrder] | None = []
        self.fail: str | None = None
        self.write_attempt = False  # tries a POST during a read (must be refused)
        self.calls: list[str] = []


ACCOUNT = Account()


@registry.register
class FakeReconAdapter(BrokerAdapter):
    kind: ClassVar[str] = "fake_recon"
    supported_types: ClassVar[frozenset[OrderType]] = frozenset(OrderType)
    supported_asset_classes: ClassVar[frozenset[AssetClass]] = frozenset(AssetClass)

    def _fail(self, call: str) -> None:
        ACCOUNT.calls.append(call)
        if ACCOUNT.fail:
            raise RuntimeError(ACCOUNT.fail)

    async def get_account(self) -> AccountSnapshot:
        self._fail("get_account")
        if ACCOUNT.write_attempt:
            await self.http.request("POST", "https://broker.example/v2/orders")
        return AccountSnapshot("acc-1", "cash", ACCOUNT.currency, ACCOUNT.cash,
                               len(ACCOUNT.positions), 0, None, None, {})

    async def get_positions(self) -> dict[str, Decimal]:
        self._fail("get_positions")
        return dict(ACCOUNT.positions)

    async def get_open_orders(self) -> list[BrokerOpenOrder] | None:
        self._fail("get_open_orders")
        return None if ACCOUNT.orders is None else list(ACCOUNT.orders)

    async def place_order(self, order, instrument):
        ACCOUNT.calls.append("place_order")
        raise AssertionError("a reconciliation never places orders")

    async def cancel_order(self, broker_order_id):
        ACCOUNT.calls.append("cancel_order")
        raise AssertionError("a reconciliation never cancels orders")


@pytest.fixture(autouse=True)
def _env(monkeypatch, pg_url, redis_url):
    global ACCOUNT
    monkeypatch.setenv("SMT_DATABASE_URL", pg_url)
    monkeypatch.setenv("SMT_REDIS_URL", redis_url)
    monkeypatch.setenv("SMT_MASTER_KEY", "test-master-key")
    monkeypatch.setenv("SMT_ADMIN_EMAIL", "admin@example.com")
    monkeypatch.setenv("SMT_ADMIN_PASSWORD", "test-password")
    ACCOUNT = Account()
    yield


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


async def _linked(maker, *, name="Alfa", cash=10000) -> tuple[uuid.UUID, uuid.UUID]:
    async with maker() as s:
        p = await create_portfolio(s, name=name, initial_cash=D(cash), leverage=D(1))
        link = await create_link(s, f"L-{uuid.uuid4().hex[:8]}", "fake_recon", {})
        await link_portfolio(s, p.id, link.id)
        discard_post_commit(s)
        await s.commit()
        return p.id, link.id


async def _fill(maker, qs, pid, link_id, qty="10", symbol="AAPL") -> None:
    async with maker() as s:
        o = Order(portfolio_id=pid, symbol=symbol, side=OrderSide.buy, type=OrderType.market,
                  qty=D(qty), status=OrderStatus.new, broker_link_id=link_id)
        s.add(o)
        await s.flush()
        quote = (await qs.get_quotes([symbol]))[symbol]
        await execute_fill(s, qs, o.id, quote=quote, source="broker", fill_qty=D(qty),
                           fill_price=D(100), final=True)
        discard_post_commit(s)
        await s.commit()


async def _open_order(maker, pid, link_id, *, broker_id="b1", qty="5", limit="90",
                      side=OrderSide.buy, updated=T0 - timedelta(hours=1), **kw) -> uuid.UUID:
    async with maker() as s:
        o = Order(portfolio_id=pid, symbol="AAPL", side=side, type=OrderType.limit,
                  qty=D(qty), limit_price=D(limit), status=OrderStatus.pending,
                  broker_link_id=link_id, broker_order_id=broker_id,
                  broker_status="accepted", broker_sent_at=T0 - timedelta(hours=1), **kw)
        s.add(o)
        await s.commit()
        await s.execute(update(Order).where(Order.id == o.id).values(updated_at=updated))
        await s.commit()
        return o.id


async def _cash(maker, pid) -> Decimal:
    """Our EUR cash."""
    async with maker() as s:
        return await s.scalar(
            select(AccountBalance.balance)
            .join(LedgerAccount, LedgerAccount.id == AccountBalance.account_id)
            .where(LedgerAccount.portfolio_id == pid, LedgerAccount.kind == AccountKind.cash,
                   LedgerAccount.asset == "EUR"))


async def _check(maker, qs, link_id, now=T0, **kw) -> ReconciliationCheck:
    rows = await check_link(maker, link_id, qs, clock=Clock(now), **kw)
    assert len(rows) == 1
    return rows[0]


def _items(row) -> dict[str, dict]:
    return {i["key"]: i for i in row.items}


async def _alerts(maker) -> list[Activity]:
    async with maker() as s:
        return list(await s.scalars(select(Activity).where(Activity.type == ALERT_TYPE)
                                    .order_by(Activity.id)))


async def _outbox(maker) -> list[TelegramOutbox]:
    async with maker() as s:
        return list(await s.scalars(select(TelegramOutbox).order_by(TelegramOutbox.id)))


# -- comparisons -----------------------------------------------------------------------

async def test_matching_account_is_ok(maker, qs):
    pid, link_id = await _linked(maker)
    await _fill(maker, qs, pid, link_id)
    ACCOUNT.positions = {"AAPL": D(10)}
    ACCOUNT.cash = await _cash(maker, pid)
    await _open_order(maker, pid, link_id)
    ACCOUNT.orders = [BrokerOpenOrder("b1", "AAPL", "buy", D(5), D(90), None)]
    ACCOUNT.cash -= D(360)  # the broker reserves the open buy (450 USD at 0,8)
    row = await _check(maker, qs, link_id)
    assert row.status == "ok", [(i["key"], i["status"], i["ours"], i["broker"]) for i in row.items]
    items = _items(row)
    assert set(items) == {"position:AAPL", "cash:EUR", "order:b1"}
    assert items["position:AAPL"]["ours"] == items["position:AAPL"]["broker"] == "10"
    assert row.meta["orders_checked"] is True and row.meta["currency"] == "EUR"
    assert row.trigger == "schedule" and row.portfolio_id == pid
    assert "place_order" not in ACCOUNT.calls and "cancel_order" not in ACCOUNT.calls


async def test_fractional_rounding_is_within_tolerance(maker, qs):
    pid, link_id = await _linked(maker)
    await _fill(maker, qs, pid, link_id, qty="0.123456789")
    ACCOUNT.positions = {"AAPL": D("0.1234568")}
    row = await _check(maker, qs, link_id)
    assert _items(row)["position:AAPL"]["status"] == "ok"


async def test_qty_mismatch_and_unknown_position(maker, qs):
    pid, link_id = await _linked(maker)
    await _fill(maker, qs, pid, link_id)
    ACCOUNT.positions = {"AAPL": D(7), "MSFT": D(2)}
    row = await _check(maker, qs, link_id)
    items = _items(row)
    assert row.status == "mismatch"
    assert (items["position:AAPL"]["status"], items["position:AAPL"]["diff"]) == ("mismatch", "3")
    assert items["position:MSFT"]["status"] == "mismatch"
    assert items["position:MSFT"]["note"] == "posizione al broker sconosciuta a SMTrading"
    assert items["position:AAPL"]["streak"] == 1 and await _alerts(maker) == []


async def test_difference_explained_by_an_open_order_is_a_warning(maker, qs):
    pid, link_id = await _linked(maker)
    await _fill(maker, qs, pid, link_id)
    await _open_order(maker, pid, link_id, qty="5")
    ACCOUNT.positions = {"AAPL": D(13)}  # 3 of the 5 filled at the broker, not synced yet
    ACCOUNT.orders = [BrokerOpenOrder("b1", "AAPL", "buy", D(2), D(90), None)]
    row = await _check(maker, qs, link_id)
    items = _items(row)
    assert items["position:AAPL"]["status"] == "warning"
    assert items["order:b1"]["status"] == "mismatch"  # 5 open here, 2 at the broker
    assert "quantità" in items["order:b1"]["note"]


async def test_missing_order_and_unknown_broker_order(maker, qs):
    pid, link_id = await _linked(maker)
    await _open_order(maker, pid, link_id, broker_id="b1")
    ACCOUNT.orders = [BrokerOpenOrder("zz9", "AAPL", "sell", D(1), D(120), None)]
    row = await _check(maker, qs, link_id)
    items = _items(row)
    assert items["order:b1"]["note"] == "aperto in SMTrading, assente al broker"
    assert items["order:b1"]["status"] == "mismatch"
    assert items["order:zz9"]["note"] == "ordine al broker sconosciuto a SMTrading"
    assert items["order:zz9"]["status"] == "mismatch"


async def test_order_closed_here_but_open_at_the_broker(maker, qs):
    pid, link_id = await _linked(maker)
    oid = await _open_order(maker, pid, link_id, broker_id="b1")
    async with maker() as s:
        await s.execute(update(Order).where(Order.id == oid).values(
            status=OrderStatus.canceled, updated_at=T0 - timedelta(hours=1)))
        await s.commit()
    ACCOUNT.orders = [BrokerOpenOrder("b1", "AAPL", "buy", D(5), D(90), None)]
    items = _items(await _check(maker, qs, link_id))
    assert items["order:b1"]["note"] == "chiuso in SMTrading, aperto al broker"


async def test_recent_order_change_and_price_mismatch(maker, qs):
    pid, link_id = await _linked(maker)
    await _open_order(maker, pid, link_id, broker_id="b1", updated=T0 - timedelta(minutes=1))
    await _open_order(maker, pid, link_id, broker_id="b2", limit="80", client_order_id="x2")
    ACCOUNT.orders = [BrokerOpenOrder("b2", "AAPL", "buy", D(5), D("80.5"), None)]
    items = _items(await _check(maker, qs, link_id))
    assert items["order:b1"]["status"] == "warning"  # being synced
    assert (items["order:b2"]["status"], items["order:b2"]["note"]) == (
        "mismatch", "diverso: prezzo limite")


async def test_cash_shortfall_tolerance_and_surplus(maker, qs):
    pid, link_id = await _linked(maker)
    ours = await _cash(maker, pid)
    ACCOUNT.cash = ours - D(9)  # within 0,1 % of 10.000
    assert _items(await _check(maker, qs, link_id))["cash:EUR"]["status"] == "ok"
    ACCOUNT.cash = ours + D(5000)  # a surplus is fine
    item = _items(await _check(maker, qs, link_id))["cash:EUR"]
    assert (item["status"], item["note"]) == ("ok", "più liquidità al broker")
    ACCOUNT.cash = ours - D(11)
    item = _items(await _check(maker, qs, link_id))["cash:EUR"]
    assert item["status"] == "mismatch" and item["diff"] == "11"


async def test_cash_in_another_currency_is_converted(maker, qs):
    pid, link_id = await _linked(maker)
    ACCOUNT.currency = "USD"
    ACCOUNT.cash = await _cash(maker, pid) * D("1.25")
    item = _items(await _check(maker, qs, link_id))["cash:USD"]
    assert item["status"] == "ok" and item["broker"] == item["ours"]
    qs._fx.pop(("EUR", "USD"))
    item = _items(await _check(maker, qs, link_id))["cash:USD"]
    assert item["status"] == "unverifiable" and item["note"] == "cambio non disponibile"


async def test_broker_without_an_open_orders_list(maker, qs):
    pid, link_id = await _linked(maker)
    await _open_order(maker, pid, link_id)
    ACCOUNT.orders = None
    ACCOUNT.cash = D(10**6)
    row = await _check(maker, qs, link_id)
    assert row.meta["orders_checked"] is False
    assert not [k for k in _items(row) if k.startswith("order:")]


# -- persistence, alerts --------------------------------------------------------------

async def test_mismatch_alerts_on_the_second_check_once_per_day(maker, qs):
    pid, link_id = await _linked(maker)
    await _fill(maker, qs, pid, link_id)
    ACCOUNT.positions = {"AAPL": D(7)}
    first = await _check(maker, qs, link_id)
    assert _items(first)["position:AAPL"]["streak"] == 1
    assert await _alerts(maker) == [] and await _outbox(maker) == []

    second = await _check(maker, qs, link_id, now=T0 + timedelta(minutes=15))
    item = _items(second)["position:AAPL"]
    assert item["streak"] == 2 and item["since"] == T0.isoformat() and item["alerted"]
    alerts = await _alerts(maker)
    assert len(alerts) == 1
    a = alerts[0]
    assert a.portfolio_id == pid and a.level == "warning" and a.telegram is None
    assert a.data["key"] == "position:AAPL" and a.data["check_id"] == second.id
    assert "SMTrading 10, broker 7" in a.summary and "Risk Manager" in a.summary
    assert not FINGERPRINT.search(a.summary + str(a.data))  # no simulation words
    outbox = await _outbox(maker)
    assert len(outbox) == 1 and outbox[0].category == "system"
    assert "Alfa" in outbox[0].text and "AAPL" in outbox[0].text

    third = await _check(maker, qs, link_id, now=T0 + timedelta(minutes=30))
    assert _items(third)["position:AAPL"]["streak"] == 3
    assert len(await _alerts(maker)) == 1 and len(await _outbox(maker)) == 1  # same day

    await _check(maker, qs, link_id, now=T0 + timedelta(days=1))  # next day: again
    assert len(await _alerts(maker)) == 2 and len(await _outbox(maker)) == 2


async def test_a_resolved_mismatch_starts_over(maker, qs):
    pid, link_id = await _linked(maker)
    await _fill(maker, qs, pid, link_id)
    ACCOUNT.positions = {"AAPL": D(7)}
    await _check(maker, qs, link_id)
    ACCOUNT.positions = {"AAPL": D(10)}
    assert (await _check(maker, qs, link_id, now=T0 + timedelta(minutes=15))).status == "ok"
    ACCOUNT.positions = {"AAPL": D(7)}
    row = await _check(maker, qs, link_id, now=T0 + timedelta(minutes=30))
    assert _items(row)["position:AAPL"]["streak"] == 1 and await _alerts(maker) == []


async def test_adapter_error_is_unverifiable_without_false_alarm(maker, qs):
    pid, link_id = await _linked(maker)
    await _fill(maker, qs, pid, link_id)
    ACCOUNT.positions = {"AAPL": D(7)}
    await _check(maker, qs, link_id)
    ACCOUNT.fail = "fake: HTTP 503: down"
    row = await _check(maker, qs, link_id, now=T0 + timedelta(minutes=15))
    assert row.status == "unverifiable" and row.items == [] and "503" in row.error
    assert await _alerts(maker) == []
    ACCOUNT.fail = None  # the streak carries over the unverifiable check
    row = await _check(maker, qs, link_id, now=T0 + timedelta(minutes=30))
    assert _items(row)["position:AAPL"]["streak"] == 2 and len(await _alerts(maker)) == 1


async def test_a_write_during_the_check_is_refused(maker, qs):
    _, link_id = await _linked(maker)
    ACCOUNT.write_attempt = True
    row = await _check(maker, qs, link_id)
    assert row.status == "unverifiable" and READ_ONLY_MESSAGE in row.error


async def test_restart_does_not_alert_twice(maker, qs):
    """A new process (new job, new engine state) reads the streaks and the alerts sent
    from the database."""
    pid, link_id = await _linked(maker)
    await _fill(maker, qs, pid, link_id)
    ACCOUNT.positions = {"AAPL": D(7)}
    await _check(maker, qs, link_id)
    await _check(maker, qs, link_id, now=T0 + timedelta(minutes=15))
    assert len(await _alerts(maker)) == 1
    await _check(maker, qs, link_id, now=T0 + timedelta(minutes=16))  # "after a restart"
    assert len(await _alerts(maker)) == 1 and len(await _outbox(maker)) == 1


async def test_two_portfolios_on_one_link(maker, qs):
    pid, link_id = await _linked(maker)
    async with maker() as s:
        other = await create_portfolio(s, name="Beta", initial_cash=D(10000), leverage=D(1))
        await link_portfolio(s, other.id, link_id)
        discard_post_commit(s)
        await s.commit()
    await _open_order(maker, pid, link_id, broker_id="b1")
    ACCOUNT.positions = {"MSFT": D(1)}
    rows = await check_link(maker, link_id, qs, clock=Clock(T0))
    by = {r.portfolio_id: _items(r) for r in rows}
    assert set(by) == {pid, other.id}
    assert "order:b1" in by[pid] and "order:b1" not in by[other.id]
    assert "position:MSFT" in by[pid] and "position:MSFT" in by[other.id]
    await check_link(maker, link_id, qs, clock=Clock(T0 + timedelta(minutes=15)))
    assert len(await _alerts(maker)) == 3  # MSFT and b1 on Alfa, MSFT on Beta
    assert len({a.portfolio_id for a in await _alerts(maker)}) == 2
    texts = [o.text for o in await _outbox(maker)]
    assert sum("MSFT" in t for t in texts) == 1  # one Telegram per link item


async def test_archived_and_unlinked_portfolios_are_left_out(maker, qs):
    pid, link_id = await _linked(maker)
    async with maker() as s:
        await create_portfolio(s, name="Solo", initial_cash=D(1000))
        await s.commit()
    assert await check_all(maker, qs, clock=Clock(T0)) == 0
    async with maker() as s:
        await s.execute(update(Portfolio).where(Portfolio.id == pid)
                        .values(status=PortfolioStatus.archived))
        await s.commit()
    assert await check_link(maker, link_id, qs, clock=Clock(T0)) == []
    async with maker() as s:
        assert len(list(await s.scalars(select(ReconciliationCheck)))) == 1


# -- the job --------------------------------------------------------------------------

def test_due_every_15_minutes_in_session_and_once_after_the_us_close():
    assert due(T0, {}) == (True, None)
    assert due(T0, {"last_run_at": (T0 - timedelta(minutes=10)).isoformat()}) == (False, None)
    assert due(T0, {"last_run_at": (T0 - timedelta(minutes=15)).isoformat()}) == (True, None)
    after = datetime(2026, 11, 10, 21, 15, tzinfo=UTC)  # NYSE closed 21:00 UTC
    assert due(after, {"last_run_at": T0.isoformat()}) == (True, "2026-11-10")
    assert due(after, {"after_close_day": "2026-11-10"}) == (False, None)
    night = datetime(2026, 11, 11, 3, 0, tzinfo=UTC)
    assert due(night, {"after_close_day": "2026-11-10"}) == (False, None)
    saturday = datetime(2026, 11, 14, 15, 0, tzinfo=UTC)
    assert due(saturday, {"after_close_day": "2026-11-13"}) == (False, None)


async def test_job_persists_its_state_across_restarts(maker, qs):
    await _linked(maker)
    clock = Clock(T0)
    assert await ReconciliationJob(maker, qs, None, clock=clock).tick() is True
    clock.now = T0 + timedelta(minutes=5)
    assert await ReconciliationJob(maker, qs, None, clock=clock).tick() is False  # restart
    clock.now = T0 + timedelta(minutes=15)
    assert await ReconciliationJob(maker, qs, None, clock=clock).tick() is True
    async with maker() as s:
        assert len(list(await s.scalars(select(ReconciliationCheck)))) == 2
        state = (await s.get(AppSetting, job_module.STATE_KEY)).value
    assert state["last_run_at"] == clock.now.isoformat()


async def test_job_skips_a_disabled_link(maker, qs):
    _, link_id = await _linked(maker)
    from smtrading.brokers.models import BrokerLink

    async with maker() as s:
        await s.execute(update(BrokerLink).where(BrokerLink.id == link_id)
                        .values(enabled=False))
        await s.commit()
    assert await ReconciliationJob(maker, qs, None, clock=Clock(T0)).tick() is True
    async with maker() as s:
        assert list(await s.scalars(select(ReconciliationCheck))) == []


async def test_unsent_order_explains_an_unknown_broker_order(maker, qs):
    pid, link_id = await _linked(maker)
    async with maker() as s:
        s.add(Order(portfolio_id=pid, symbol="AAPL", side=OrderSide.buy, type=OrderType.limit,
                    qty=D(5), limit_price=D(90), status=OrderStatus.pending,
                    broker_link_id=link_id, broker_status=BrokerState.SENDING,
                    broker_sent_at=T0))
        await s.commit()
    ACCOUNT.orders = [BrokerOpenOrder("new1", "AAPL", "buy", D(5), D(90), None)]
    ACCOUNT.cash = D(10**6)
    items = _items(await _check(maker, qs, link_id))
    assert (items["order:new1"]["status"], items["order:new1"]["note"]) == (
        "warning", "invio in corso")
