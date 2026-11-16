"""«Posizioni aperte» (positions.open_view): open positions of the non-archived portfolios
with their stop/target, the distance and the loss to the stop, the protection state and
per portfolio the risk at the stops against the daily loss limit."""

import uuid
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from smtrading.engine.execution import execute_fill
from smtrading.engine.models import Order, OrderSide, OrderStatus, OrderType, TimeInForce
from smtrading.engine.quotesource import FakeQuoteSource
from smtrading.instruments.market_hours import set_closures
from smtrading.instruments.models import AssetClass, Instrument
from smtrading.portfolios.models import Portfolio, PortfolioStatus
from smtrading.portfolios.service import create_portfolio
from smtrading.positions.open_view import open_positions
from smtrading.risk.models import EquitySnapshot, RiskProfile, SnapshotKind
from smtrading.seed import run_seed

D = Decimal
NOW = datetime(2026, 11, 17, 18, 0, tzinfo=UTC)  # Tuesday: NASDAQ open, MTA closed


@pytest.fixture(autouse=True)
def _no_closures():
    set_closures([])
    yield
    set_closures([])


class Env:
    def __init__(self, s):
        self.s = s
        self.qs = FakeQuoteSource()
        self.qs.set_fx("EUR", "USD", D("1.25"))  # 1 USD = 0.8 EUR

    async def setup(self):
        await run_seed(self.s)
        for sym, market, ccy in (("ZZAMZ", "NASDAQ", "USD"), ("ZZMSF", "NASDAQ", "USD"),
                                 ("ZZQQ", "NASDAQ", "USD"), ("ZZNOQ", "NASDAQ", "USD"),
                                 ("ZZSHO", "NASDAQ", "USD"), ("ZZENI.MI", "MTA", "EUR")):
            if await self.s.get(Instrument, sym) is None:
                self.s.add(Instrument(
                    symbol=sym, name=f"{sym} Inc", market=market, currency=ccy,
                    asset_class=AssetClass.eu_equity if ccy == "EUR" else AssetClass.us_equity,
                    shortable=True, lot_size=D("0.000001")))
        await self.s.flush()

    async def portfolio(self, name, **kw) -> Portfolio:
        return await create_portfolio(self.s, name=name, initial_cash=D(100000), **kw)

    async def fill(self, p, sym, side, qty, price):
        self.qs.set_quote(sym, D(price), D(price), D(price))
        o = Order(portfolio_id=p.id, symbol=sym, side=side, type=OrderType.market,
                  qty=D(qty), status=OrderStatus.new)
        self.s.add(o)
        await self.s.flush()
        quote = (await self.qs.get_quotes([sym]))[sym]
        assert await execute_fill(self.s, self.qs, o.id, quote=quote) is not None

    async def order(self, p, sym, side, type_, *, tif=TimeInForce.gtc, status=OrderStatus.pending,
                    qty="1", **kw) -> Order:
        o = Order(portfolio_id=p.id, symbol=sym, side=side, type=type_, tif=tif, qty=D(qty),
                  status=status, **kw)
        self.s.add(o)
        await self.s.flush()
        return o


@pytest.fixture
async def env(db_session):
    e = Env(db_session)
    await e.setup()
    return e


def _rows(out):
    return {(r["portfolio_name"], r["symbol"]): r for r in out["positions"]}


async def test_empty(env):
    out = await open_positions(env.s, env.qs, NOW)
    assert out["as_of"] == NOW
    assert [r for r in out["positions"] if r["symbol"].startswith("ZZ")] == []


async def test_protected_long_with_oco_in_usd(env):
    prof = RiskProfile(name=f"R-{uuid.uuid4().hex[:6]}", max_daily_loss_eur=D(40))
    env.s.add(prof)
    await env.s.flush()
    p = await env.portfolio("ZZ Aggressivo", risk_profile_id=prof.id)
    await env.fill(p, "ZZAMZ", OrderSide.buy, "2", 200)
    env.qs.set_quote("ZZAMZ", D(210), D(210), D(210))
    group = uuid.uuid4()
    stop = await env.order(p, "ZZAMZ", OrderSide.sell, OrderType.stop, qty="2",
                           stop_price=D(190), oco_group_id=group)
    # a lower stop and a farther limit are not the chosen ones
    await env.order(p, "ZZAMZ", OrderSide.sell, OrderType.stop, stop_price=D(150))
    await env.order(p, "ZZAMZ", OrderSide.sell, OrderType.limit, limit_price=D(230))
    target = await env.order(p, "ZZAMZ", OrderSide.sell, OrderType.limit, qty="2",
                             limit_price=D(240), oco_group_id=group)

    out = await open_positions(env.s, env.qs, NOW)
    r = _rows(out)[("ZZ Aggressivo", "ZZAMZ")]
    assert r["portfolio_id"] == p.id and r["side"] == "long" and r["currency"] == "USD"
    assert r["qty"] == D(2) and r["avg_price"] == D(200) and r["last"] == D(210)
    assert r["value_eur"] == D("336.00") and r["pnl_eur"] == D("16.00")
    assert r["pnl_pct"] == D("5.00")
    assert r["stop"] == {"order_id": stop.id, "price": D(190), "tif": TimeInForce.gtc}
    assert r["target"] == {"order_id": target.id, "price": D(240), "tif": TimeInForce.gtc}
    assert r["distance_to_stop_pct"] == D("9.52")
    assert r["risk_at_stop_eur"] == D("32.00")  # (210 - 190) * 2 USD
    assert r["protection"] == "ok" and r["stale"] is False

    [t] = [t for t in out["portfolios"] if t["portfolio_id"] == p.id]
    assert t["positions"] == 1 and t["risk_at_stop_eur"] == D("32.00")
    assert t["daily_loss_limit_eur"] == D("40.00") and t["risk_pct_of_limit"] == D("80.00")
    assert t["unprotected"] == 0 and t["value_eur"] == D("336.00")
    assert t["mode"] is not None


async def test_unprotected_expiring_short_and_missing_quote(env):
    p = await env.portfolio("ZZ Misto")  # Default profile: 10% of the equity
    await env.fill(p, "ZZMSF", OrderSide.buy, "1", 100)
    # a bracket stop still waiting for its entry covers nothing
    await env.order(p, "ZZMSF", OrderSide.sell, OrderType.stop, stop_price=D(90),
                    status=OrderStatus.held)
    # a buy limit is not a target of a long
    await env.order(p, "ZZMSF", OrderSide.buy, OrderType.limit, limit_price=D(80))
    await env.fill(p, "ZZQQ", OrderSide.buy, "1", 50)
    await env.order(p, "ZZQQ", OrderSide.sell, OrderType.stop, stop_price=D(45),
                    tif=TimeInForce.day)
    await env.fill(p, "ZZENI.MI", OrderSide.buy, "10", 5)
    await env.order(p, "ZZENI.MI", OrderSide.sell, OrderType.stop, qty="10",
                    stop_price=D("4.5"), tif=TimeInForce.day)  # MTA closed: not today
    await env.fill(p, "ZZSHO", OrderSide.sell, "1", 100)
    await env.order(p, "ZZSHO", OrderSide.buy, OrderType.trailing_stop,
                    trail_pct=D(10), trail_ref_price=D(100))  # level 110
    await env.fill(p, "ZZNOQ", OrderSide.buy, "1", 20)
    await env.order(p, "ZZNOQ", OrderSide.sell, OrderType.stop, stop_price=D(18))
    env.qs._quotes.pop("ZZNOQ")
    # no valuation without a quote: the pct limit uses the latest snapshot
    env.s.add(EquitySnapshot(portfolio_id=p.id, ts=NOW, kind=SnapshotKind.periodic,
                             equity_eur=D(500), cash_eur=D(500), exposure_eur=D(0),
                             margin_used_eur=D(0)))

    out = await open_positions(env.s, env.qs, NOW)
    rows = _rows(out)
    msf = rows[("ZZ Misto", "ZZMSF")]
    assert msf["protection"] == "senza stop" and msf["stop"] is None
    assert msf["target"] is None and msf["risk_at_stop_eur"] is None
    assert rows[("ZZ Misto", "ZZQQ")]["protection"] == "scade oggi"
    eni = rows[("ZZ Misto", "ZZENI.MI")]
    assert eni["protection"] == "ok" and eni["risk_at_stop_eur"] == D("5.00")
    sho = rows[("ZZ Misto", "ZZSHO")]
    assert sho["side"] == "short" and sho["qty"] == D(-1)
    assert sho["stop"]["price"] == D(110) and sho["distance_to_stop_pct"] == D("10.00")
    assert sho["risk_at_stop_eur"] == D("8.00")  # 10 USD
    noq = rows[("ZZ Misto", "ZZNOQ")]
    for k in ("last", "value_eur", "pnl_eur", "pnl_pct", "distance_to_stop_pct",
              "risk_at_stop_eur"):
        assert noq[k] is None, k
    assert noq["stop"]["price"] == D(18) and noq["protection"] == "ok"

    [t] = [t for t in out["portfolios"] if t["portfolio_id"] == p.id]
    assert t["positions"] == 5 and t["unprotected"] == 1 and t["expiring_today"] == 1
    assert t["risk_unknown"] == 1 and t["value_eur"] is None  # one position without a quote
    # ZZQQ (50-45)*0.8 = 4 + ZZENI 5 + ZZSHO 8
    assert t["risk_at_stop_eur"] == D("17.00")
    assert t["daily_loss_limit_eur"] == D("50.00")  # Default 10% of 500
    assert t["risk_pct_of_limit"] == D("34.00")


async def test_day_open_equity_sets_the_pct_limit_and_archived_is_hidden(env):
    prof = RiskProfile(name=f"R-{uuid.uuid4().hex[:6]}", max_daily_loss_pct=D(5),
                       max_daily_loss_eur=D(1000))
    env.s.add(prof)
    await env.s.flush()
    p = await env.portfolio("ZZ Prudente", risk_profile_id=prof.id)
    await env.fill(p, "ZZAMZ", OrderSide.buy, "1", 100)
    await env.order(p, "ZZAMZ", OrderSide.sell, OrderType.stop, stop_price=D(90))
    env.s.add(EquitySnapshot(portfolio_id=p.id, ts=NOW, kind=SnapshotKind.day_open,
                             local_date=NOW.date(), equity_eur=D(800), cash_eur=D(800),
                             exposure_eur=D(0), margin_used_eur=D(0)))
    old = await env.portfolio("ZZ Archivio")
    await env.fill(old, "ZZAMZ", OrderSide.buy, "1", 100)
    old.status = PortfolioStatus.archived
    await env.s.flush()

    out = await open_positions(env.s, env.qs, NOW)
    [t] = [t for t in out["portfolios"] if t["portfolio_id"] == p.id]
    assert t["daily_loss_limit_eur"] == D("40.00")  # 5% of 800 < 1000
    assert t["risk_at_stop_eur"] == D("8.00") and t["risk_pct_of_limit"] == D("20.00")
    assert all(r["portfolio_id"] != old.id for r in out["positions"])
    assert all(t["portfolio_id"] != old.id for t in out["portfolios"])


async def test_protection_matches_the_alarm(env):
    """Same rules as the protection alarm: partial cover, a stop being canceled protects
    nothing, one being modified does."""
    p = await env.portfolio("ZZ Allineato")
    await env.fill(p, "ZZAMZ", OrderSide.buy, "2", 200)
    await env.order(p, "ZZAMZ", OrderSide.sell, OrderType.stop, qty="1", stop_price=D(190))
    await env.fill(p, "ZZMSF", OrderSide.buy, "1", 100)
    await env.order(p, "ZZMSF", OrderSide.sell, OrderType.stop, stop_price=D(90),
                    broker_status="cancel_requested")
    await env.fill(p, "ZZQQ", OrderSide.buy, "1", 50)
    await env.order(p, "ZZQQ", OrderSide.sell, OrderType.stop, stop_price=D(45),
                    broker_status="replace_requested")
    rows = _rows(await open_positions(env.s, env.qs, NOW))
    assert rows[("ZZ Allineato", "ZZAMZ")]["protection"] == "copre parziale"
    assert rows[("ZZ Allineato", "ZZMSF")]["protection"] == "senza stop"
    assert rows[("ZZ Allineato", "ZZQQ")]["protection"] == "ok"
    out = await open_positions(env.s, env.qs, NOW)
    [t] = [t for t in out["portfolios"] if t["portfolio_id"] == p.id]
    assert t["partial_cover"] == 1 and t["unprotected"] == 1


async def test_pct_limit_from_the_current_equity_without_a_second_valuation(env, monkeypatch):
    from smtrading.positions import open_view
    from smtrading.valuation import service
    from smtrading.valuation.service import value_portfolio

    p = await env.portfolio("ZZ Equity")  # Default profile: 10% of the equity
    await env.fill(p, "ZZAMZ", OrderSide.buy, "2", 200)
    env.qs.set_quote("ZZAMZ", D(210), D(210), D(210))
    equity = (await value_portfolio(env.s, env.qs, p.id)).equity_raw

    async def no_valuation(*_a, **_k):
        raise AssertionError("value_portfolio called again")

    monkeypatch.setattr(service, "value_portfolio", no_valuation)
    monkeypatch.setattr(open_view, "value_portfolio", no_valuation, raising=False)
    out = await open_positions(env.s, env.qs, NOW)
    [t] = [t for t in out["portfolios"] if t["portfolio_id"] == p.id]
    assert t["daily_loss_limit_eur"] == (equity / 10).quantize(D("0.01"))
