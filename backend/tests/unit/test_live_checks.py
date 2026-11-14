"""Pre-trade checks of a real-money portfolio (risk.live_checks): cash cap, fuse (R8.1),
no short, strict kill switch (R8.2). Pure where possible; the rest on a fresh database.
Nothing here talks to a broker."""

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

from smtrading.brokers.models import BrokerLink
from smtrading.common.db import make_engine
from smtrading.common.errors import ErrorCode, SMTError
from smtrading.engine.models import Order, OrderSide, OrderStatus, OrderType
from smtrading.engine.quotesource import FakeQuoteSource
from smtrading.engine.service import PlaceOrderRequest, place_order
from smtrading.instruments.models import Instrument
from smtrading.portfolios.models import Portfolio, PortfolioStatus
from smtrading.portfolios.service import create_portfolio
from smtrading.risk.checks import pre_trade_check
from smtrading.risk.live_checks import (
    SNAPSHOT_MAX_AGE_S,
    live_cash_needed,
    live_pre_trade,
    snapshot_cash,
)
from smtrading.risk.models import RiskProfile
from smtrading.risk.settings import set_kill_switch
from smtrading.seed import run_seed

D = Decimal
NOW = datetime(2026, 11, 10, 15, 0, tzinfo=UTC)  # Tuesday: NASDAQ open


def test_cash_needed_formula():
    assert live_cash_needed(D(100), D(1)) == D(103)  # 100 + 1 + 2% of 100
    assert live_cash_needed(D("0.5"), D(0)) == D("0.51")


def test_snapshot_cash_only_when_fresh():
    link = BrokerLink(name="x", kind="alpaca_live", live_state="armed",
                      last_account_snapshot={"currency": "USD", "cash_available": "12.5"},
                      last_snapshot_at=NOW - timedelta(seconds=SNAPSHOT_MAX_AGE_S))
    assert snapshot_cash(link, NOW) == (D("12.5"), "USD")
    link.last_snapshot_at = NOW - timedelta(seconds=SNAPSHOT_MAX_AGE_S + 1)
    assert snapshot_cash(link, NOW) is None
    link.last_snapshot_at, link.last_account_snapshot = NOW, None
    assert snapshot_cash(link, NOW) is None


@pytest.fixture(autouse=True)
def _env(monkeypatch, pg_url, redis_url):
    monkeypatch.setenv("SMT_DATABASE_URL", pg_url)
    monkeypatch.setenv("SMT_REDIS_URL", redis_url)
    monkeypatch.setenv("SMT_MASTER_KEY", "test-master-key")
    monkeypatch.setenv("SMT_ADMIN_EMAIL", "admin@example.com")
    monkeypatch.setenv("SMT_ADMIN_PASSWORD", "test-password")


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


async def _portfolio(maker, qs, *, state="armed", cash=1000, held=0, snapshot=None,
                     snapshot_age_s=0, profile_limit=None) -> tuple[uuid.UUID, uuid.UUID]:
    """A EUR portfolio linked to an alpaca_live link in `state`; `held` AAPL bought on
    the simulator first (then the link is set directly)."""
    async with maker() as s:
        risk_id = None
        if profile_limit is not None:
            profile = RiskProfile(name=f"R-{uuid.uuid4().hex[:6]}", short_allowed=False,
                                  max_live_orders_per_hour=profile_limit)
            s.add(profile)
            await s.flush()
            risk_id = profile.id
        p = await create_portfolio(s, name=f"P-{uuid.uuid4().hex[:8]}",
                                   initial_cash=D(cash), risk_profile_id=risk_id)
        await s.commit()
    if held:
        async with maker() as s:
            await place_order(s, qs, PlaceOrderRequest(
                portfolio_id=p.id, symbol="AAPL", side=OrderSide.buy,
                type=OrderType.market, qty=D(held)), now=NOW)
            await s.commit()
    async with maker() as s:
        link = BrokerLink(name=f"L-{uuid.uuid4().hex[:8]}", kind="alpaca_live",
                          live_state=state, last_account_snapshot=snapshot,
                          last_snapshot_at=None if snapshot is None
                          else NOW - timedelta(seconds=snapshot_age_s))
        s.add(link)
        await s.flush()
        (await s.get(Portfolio, p.id)).broker_link_id = link.id
        await s.commit()
        return p.id, link.id


ONE, HUNDRED, ZERO = D(1), D(100), D(0)


async def _check(maker, qs, pid, *, side=OrderSide.buy, qty=ONE, est_value=HUNDRED,
                 est_fees=ZERO, reduce_only=False, live_flatten=False, kill=False,
                 commit=True):
    async with maker() as s:
        if kill:
            await set_kill_switch(s, True)
        portfolio = await s.get(Portfolio, pid)
        link = await s.get(BrokerLink, portfolio.broker_link_id)
        try:
            await live_pre_trade(
                s, qs, portfolio=portfolio, link=link,
                instrument=await s.get(Instrument, "AAPL"), side=side, qty=qty,
                notional=None, est_value=est_value, est_fees=est_fees,
                reduce_only=reduce_only, live_flatten=live_flatten, now=NOW,
            )
        finally:
            if commit:
                await s.commit()


async def test_buy_over_min_cash_refused(maker, qs):
    """min(portfolio 1000 EUR, broker 625 USD = 500 EUR) = 500 EUR."""
    pid, _ = await _portfolio(maker, qs, snapshot={"currency": "USD", "cash_available": "625"})
    await _check(maker, qs, pid, est_value=D(480), est_fees=D(1))  # 490.6 <= 500
    with pytest.raises(SMTError) as e:
        await _check(maker, qs, pid, est_value=D(490), est_fees=D(1))  # 500.8 > 500
    assert e.value.code == ErrorCode.LIVE_CASH_LIMIT
    assert D(e.value.details["needed"]) == D("500.8")
    assert D(e.value.details["portfolio_cash"]) == D(1000)
    assert D(e.value.details["broker_cash"]) == D(500)


async def test_stale_snapshot_uses_portfolio_cash_only(maker, qs):
    pid, _ = await _portfolio(maker, qs, snapshot={"currency": "EUR", "cash_available": "10"},
                              snapshot_age_s=SNAPSHOT_MAX_AGE_S + 1)
    await _check(maker, qs, pid, est_value=D(900), est_fees=D(1))  # broker cash ignored
    with pytest.raises(SMTError) as e:
        await _check(maker, qs, pid, est_value=D(990), est_fees=D(1))  # 1010.8 > 1000
    assert e.value.code == ErrorCode.LIVE_CASH_LIMIT
    assert e.value.details["broker_cash"] is None


async def test_sell_beyond_position_is_short_not_allowed(maker, qs):
    pid, _ = await _portfolio(maker, qs, held=2)
    await _check(maker, qs, pid, side=OrderSide.sell, qty=D(2))
    with pytest.raises(SMTError) as e:
        await _check(maker, qs, pid, side=OrderSide.sell, qty=D(3))
    assert e.value.code == ErrorCode.SHORT_NOT_ALLOWED
    # a reduce-only order is clamped to the position: not a short
    await _check(maker, qs, pid, side=OrderSide.sell, qty=D(3), reduce_only=True)


async def _pre_trade(maker, qs, pid, *, side, qty, reduce_only=False, live_flatten=False):
    async with maker() as s:
        portfolio = await s.get(Portfolio, pid)
        result = await pre_trade_check(
            s, qs, portfolio=portfolio, instrument=await s.get(Instrument, "AAPL"),
            side=side, qty=qty, notional=None, order_type=OrderType.market,
            limit_price=None, stop_price=None, reduce_only=reduce_only,
            live_flatten=live_flatten, now=NOW,
        )
        await s.commit()
        return result


async def test_kill_switch_blocks_reducing_live_order(maker, qs):
    """Stricter than R25: with the kill switch on even a closing real order is refused."""
    pid, _ = await _portfolio(maker, qs, held=2)
    async with maker() as s:
        await set_kill_switch(s, True)
        await s.commit()
    for reduce_only in (False, True):
        result = await _pre_trade(maker, qs, pid, side=OrderSide.sell, qty=D(1),
                                  reduce_only=reduce_only)
        assert not result.ok and result.code == ErrorCode.KILL_SWITCH_ACTIVE


async def test_flatten_passes_kill_switch(maker, qs):
    pid, _ = await _portfolio(maker, qs, held=2)
    async with maker() as s:
        await set_kill_switch(s, True)
        await s.commit()
    result = await _pre_trade(maker, qs, pid, side=OrderSide.sell, qty=D(2),
                              reduce_only=True, live_flatten=True)
    assert result.ok, result.message
    # the flag alone does not let a buy through (R25 still applies to it)
    result = await _pre_trade(maker, qs, pid, side=OrderSide.buy, qty=D(1), live_flatten=True)
    assert not result.ok and result.code == ErrorCode.KILL_SWITCH_ACTIVE


async def test_configured_is_live_not_armed(maker, qs):
    pid, _ = await _portfolio(maker, qs, state="configured")
    with pytest.raises(SMTError) as e:
        await _check(maker, qs, pid)
    assert e.value.code == ErrorCode.LIVE_NOT_ARMED
    result = await _pre_trade(maker, qs, pid, side=OrderSide.buy, qty=D(1))
    assert not result.ok and result.code == ErrorCode.LIVE_NOT_ARMED
    # dry run: the order is accepted (BrokerSync intercepts it)
    pid, _ = await _portfolio(maker, qs, state="dry_run")
    assert (await _pre_trade(maker, qs, pid, side=OrderSide.buy, qty=D(1))).ok


async def _sent_orders(maker, pid, link_id, n: int, *, ago=timedelta(minutes=10)):
    async with maker() as s:
        for _ in range(n):
            s.add(Order(portfolio_id=pid, symbol="AAPL", side=OrderSide.buy,
                        type=OrderType.market, qty=D(1), status=OrderStatus.filled,
                        broker_link_id=link_id, broker_sent_at=NOW - ago))
        await s.commit()


async def _status(maker, pid) -> tuple[str, str | None]:
    async with maker() as s:
        p = await s.get(Portfolio, pid)
        return p.status, p.reduce_only_reason


@pytest.mark.parametrize(("limit", "n"), [(None, 30), (5, 5)])
async def test_fuse_31st_order_trips_reduce_only(maker, qs, limit, n):
    """Default 30 real orders sent per sliding hour; a profile at 5: the sixth is refused."""
    pid, link_id = await _portfolio(maker, qs, profile_limit=limit)
    await _sent_orders(maker, pid, link_id, n - 1)
    await _sent_orders(maker, pid, link_id, 3, ago=timedelta(minutes=61))  # out of the hour
    await _check(maker, qs, pid)  # n - 1 sent: still allowed
    assert await _status(maker, pid) == (PortfolioStatus.active, None)
    await _sent_orders(maker, pid, link_id, 1)
    with pytest.raises(SMTError) as e:
        await _check(maker, qs, pid)
    assert e.value.code == ErrorCode.RISK_LIMIT_EXCEEDED
    assert "fusibile" in e.value.message
    assert await _status(maker, pid) == (PortfolioStatus.reduce_only, "live_fuse")
    # an exit is never refused by the fuse; a flatten order neither
    await _check(maker, qs, pid, side=OrderSide.sell, qty=D(1), reduce_only=True)
    await _check(maker, qs, pid, side=OrderSide.sell, qty=D(1), reduce_only=True,
                 live_flatten=True)


async def test_fuse_rejection_is_stored_with_the_trip(maker, qs):
    """Through place_order: the refused order is stored as rejected and the trip is kept
    (callers commit on a persisted rejection)."""
    pid, link_id = await _portfolio(maker, qs, profile_limit=2)
    await _sent_orders(maker, pid, link_id, 2)
    async with maker() as s:
        with pytest.raises(SMTError) as e:
            await place_order(s, qs, PlaceOrderRequest(
                portfolio_id=pid, symbol="AAPL", side=OrderSide.buy,
                type=OrderType.market, qty=D(1)), now=NOW)
        assert e.value.code == ErrorCode.RISK_LIMIT_EXCEEDED and e.value.persisted
        await s.commit()
    assert await _status(maker, pid) == (PortfolioStatus.reduce_only, "live_fuse")
