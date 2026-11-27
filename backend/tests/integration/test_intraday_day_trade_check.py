import hashlib
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal as D

import pytest
from sqlalchemy import event, select, update

from smtrading.brokers.models import BrokerLink
from smtrading.common.errors import ErrorCode, SMTError
from smtrading.engine.models import Fill, Order, OrderSide, OrderStatus, OrderType
from smtrading.engine.quotesource import FakeQuoteSource
from smtrading.engine.service import PlaceOrderRequest, place_order, preview_order
from smtrading.instruments.models import Instrument
from smtrading.ledger.models import Journal, JournalType
from smtrading.portfolios.models import Portfolio
from smtrading.portfolios.service import create_portfolio
from smtrading.positions.models import Position
from smtrading.seed import run_seed
from smtrading.telegram.models import TelegramOutbox

WED = datetime(2026, 11, 25, 17, 0, tzinfo=UTC)  # NYSE open
MON = datetime(2026, 11, 23, 17, 0, tzinfo=UTC)
TUE = datetime(2026, 11, 24, 17, 0, tzinfo=UTC)


def account(bp="100000", minutes_ago=1, *, multiplier="2", equity="10000", nmbp=None):
    """A broker account state as `account_reader` saves it, read `minutes_ago` before WED."""
    at = WED - timedelta(minutes=minutes_ago)
    return {"buying_power": bp, "non_marginable_buying_power": nmbp or bp,
            "multiplier": multiplier, "equity": equity, "trading_blocked": False,
            "account_blocked": False, "read_at": at.isoformat()}, at


@pytest.fixture
async def seeded(db_session):
    await run_seed(db_session)
    await db_session.flush()


@pytest.fixture
def qs():
    q = FakeQuoteSource()
    q.set_fx("EUR", "USD", D("1.25"))
    for s in ("AAPL", "MSFT", "NVDA"):
        q.set_quote(s, D("199.90"), D("200.00"), D("199.95"))
    q.set_quote("BTC-USD", D("999.90"), D("1000.00"), D("999.95"))
    return q


async def _pf(db_session, link=None, cash=100000, **kw):
    p = await create_portfolio(db_session, name=f"P-{uuid.uuid4().hex[:8]}",
                               initial_cash=D(cash), leverage=D(1))
    p.broker_link_id = link.id if link else None
    for k, v in kw.items():
        setattr(p, k, v)
    await db_session.flush()
    return p


_FRESH = object()


async def _link(db_session, acct=_FRESH, kind="alpaca_paper"):
    """`acct`: (state, read at) from `account`, or None for a link never read."""
    state, at = account() if acct is _FRESH else (acct or (None, None))
    link = BrokerLink(name=f"L-{uuid.uuid4().hex[:8]}", kind=kind, pdt_state=state,
                      pdt_state_at=at)
    db_session.add(link)
    await db_session.flush()
    return link


async def _order(db_session, p, side, qty, at, *, symbol="AAPL", day_trade=False,
                 status=OrderStatus.filled, fill=True, reduce_only=False):
    o = Order(portfolio_id=p.id, symbol=symbol, side=side, type=OrderType.market,
              qty=D(qty), day_trade=day_trade, status=status, reduce_only=reduce_only,
              broker_link_id=p.broker_link_id)
    db_session.add(o)
    await db_session.flush()
    if fill:
        h = hashlib.sha256(uuid.uuid4().bytes).hexdigest()
        j = Journal(type=JournalType.TRADE, author_type="system", prev_hash=h,
                    hash=hashlib.sha256(h.encode()).hexdigest())
        db_session.add(j)
        await db_session.flush()
        db_session.add(Fill(order_id=o.id, portfolio_id=p.id, symbol=symbol, side=side,
                            qty=D(qty), price=D(100), fee_currency="USD", journal_id=j.id,
                            created_at=at))
        await db_session.flush()
    return o


async def _round_trips(db_session, p, n, at=MON, symbol="NVDA"):
    for _ in range(n):
        await _order(db_session, p, OrderSide.buy, 1, at, symbol=symbol, day_trade=True)
        await _order(db_session, p, OrderSide.sell, 1, at, symbol=symbol)


def _req(p, symbol="AAPL", side=OrderSide.buy, **kw):
    kw.setdefault("qty", D(1))
    return PlaceOrderRequest(portfolio_id=p.id, symbol=symbol, side=side,
                             type=OrderType.market, **kw)


async def _preview(db_session, qs, req):
    return await preview_order(db_session, qs, req, now=WED)


async def _capped(db_session, cap=1, acct=_FRESH, **kw):
    """A portfolio on an alpaca_paper link with its own cap used up (1 operation Mon 23)."""
    link = await _link(db_session, acct)
    p = await _pf(db_session, link, max_day_trades_5d=cap, **kw)
    await _round_trips(db_session, p, cap)
    return link, p


async def test_own_cap_entry_refused(db_session, seeded, qs):
    _, p = await _capped(db_session, intraday_enabled=True)
    r = await _preview(db_session, qs, _req(p, day_trade=True))
    assert not r.ok and r.code == ErrorCode.DAY_TRADE_LIMIT
    assert r.details == {"used": 1, "limit": 1, "remaining": 0, "frees_on": "2026-12-01"}
    assert "Tetto operazioni di giornata" in r.message
    assert "il 01/12" in r.message  # Mon 23 + 5 sessions (Thanksgiving)
    with pytest.raises(SMTError) as e:
        await place_order(db_session, qs, _req(p, day_trade=True), now=WED)
    assert e.value.code == ErrorCode.DAY_TRADE_LIMIT


async def test_auto_rules_portfolio_cap_refused(db_session, seeded, qs):
    _, p = await _capped(db_session, auto_rules_enabled=True)
    r = await _preview(db_session, qs, _req(p, day_trade=True))
    assert not r.ok and r.code == ErrorCode.DAY_TRADE_LIMIT


async def test_link_has_no_shared_budget(db_session, seeded, qs):
    link = await _link(db_session, account(multiplier="4", equity="10000"))
    a = await _pf(db_session, link, intraday_enabled=True)
    b = await _pf(db_session, link, auto_rules_enabled=True)
    await _round_trips(db_session, a, 3)
    await _round_trips(db_session, b, 3)
    for p in (a, b):  # the «fourth operation» no longer exists
        assert (await _preview(db_session, qs, _req(p, day_trade=True))).ok


async def test_entry_refused_when_pending_fill_the_cap(db_session, seeded, qs):
    link = await _link(db_session)
    p = await _pf(db_session, link, intraday_enabled=True, max_day_trades_5d=3)
    await _round_trips(db_session, p, 2)
    await _order(db_session, p, OrderSide.buy, 1, WED, symbol="MSFT", day_trade=True)
    db_session.add(Position(portfolio_id=p.id, symbol="MSFT", qty=D(1)))
    await db_session.flush()
    r = await _preview(db_session, qs, _req(p, symbol="AAPL", day_trade=True))
    assert not r.ok and r.code == ErrorCode.DAY_TRADE_LIMIT
    assert "più 1 già aperte" in r.message
    r = await _preview(db_session, qs, _req(p, symbol="MSFT", day_trade=True))
    assert r.ok


async def test_buying_power_refusal(db_session, seeded, qs):
    link = await _link(db_session, account(bp="500"))
    p = await _pf(db_session, link, intraday_enabled=True)
    r = await _preview(db_session, qs, _req(p, qty=D(5), day_trade=True))  # 1,000 $
    assert not r.ok and r.code == ErrorCode.INSUFFICIENT_BUYING_POWER
    assert r.details["reason"] == "broker_buying_power"
    assert r.details["notional"] == "1000" and r.details["buying_power"] == "500"
    assert r.details["read_at"] == (WED - timedelta(minutes=1)).isoformat()
    assert "potere d'acquisto del conto" in r.message and link.name in r.message
    assert "letto alle 17:59" in r.message  # the portfolio's time zone (Europe/Rome)
    link.pdt_state, link.pdt_state_at = account(bp="5000")
    await db_session.flush()
    assert (await _preview(db_session, qs, _req(p, qty=D(5), day_trade=True))).ok


async def test_buying_power_in_usd_for_another_currency(db_session, seeded, qs):
    link = await _link(db_session, account(bp="1000"))
    p = await _pf(db_session, link, intraday_enabled=True)
    inst = await db_session.get(Instrument, "AAPL")
    inst.currency = "EUR"  # 5 x 200 EUR = 1,000 EUR = 1,250 $
    await db_session.flush()
    r = await _preview(db_session, qs, _req(p, qty=D(5), day_trade=True))
    assert not r.ok and r.details["reason"] == "broker_buying_power"
    assert r.details["notional"] == "1250"


async def test_crypto_uses_non_marginable_buying_power(db_session, seeded, qs):
    link = await _link(db_session, account(bp="100000", nmbp="500"))
    p = await _pf(db_session, link, intraday_enabled=True)
    r = await _preview(db_session, qs, _req(p, symbol="BTC-USD", qty=D(1), day_trade=True))
    assert not r.ok and r.details["reason"] == "broker_buying_power"
    assert r.details["buying_power"] == "500"
    link.pdt_state, link.pdt_state_at = account(bp="0", nmbp="5000")
    await db_session.flush()
    r = await _preview(db_session, qs, _req(p, symbol="BTC-USD", qty=D(1), day_trade=True))
    assert r.ok


async def _levered(db_session, leverage, kind="alpaca_paper", **kw):
    """10,000 EUR of equity, no position, `leverage` written in the database."""
    link = await _link(db_session, kind=kind)
    p = await _pf(db_session, link, cash=10000, intraday_enabled=True, **kw)
    await db_session.execute(update(Portfolio).where(Portfolio.id == p.id)
                             .values(leverage=D(leverage)))
    await db_session.refresh(p)
    return p


# AAPL at 200 $, 1 EUR = 1.25 $: 156 shares = 24,960 EUR, 118 = 18,880, 93 = 14,880


async def test_day_leverage_capped_at_two_on_paper(db_session, seeded, qs):
    p = await _levered(db_session, 3)
    r = await _preview(db_session, qs, _req(p, qty=D(156), day_trade=True))
    assert not r.ok and r.code == ErrorCode.INSUFFICIENT_BUYING_POWER
    assert r.details == {"reason": "day_trade_leverage", "leverage": "2"}
    assert "leva ammessa per le operazioni di giornata (2)" in r.message
    assert (await _preview(db_session, qs, _req(p, qty=D(118), day_trade=True))).ok
    # not a day trade: step 10 does not apply, step 9 judges it with leverage 3
    assert (await _preview(db_session, qs, _req(p, qty=D(156), day_trade=False))).ok


async def test_live_link_day_leverage_one(db_session, seeded, qs, monkeypatch):
    from smtrading.risk import checks

    async def no_live_checks(*a, **k):  # arming, cash cap, fuse: not under test here
        return None

    monkeypatch.setattr(checks, "live_pre_trade", no_live_checks)
    p = await _levered(db_session, 2, kind="alpaca_live")
    r = await _preview(db_session, qs, _req(p, qty=D(93), day_trade=True))
    assert not r.ok and r.code == ErrorCode.INSUFFICIENT_BUYING_POWER
    assert r.details == {"reason": "day_trade_leverage", "leverage": "1"}
    assert (await _preview(db_session, qs, _req(p, qty=D(93), day_trade=False))).ok


async def test_leverage_two_paper_allowed(db_session, seeded, qs):
    p = await _levered(db_session, 2)
    assert (await _preview(db_session, qs, _req(p, qty=D(118), day_trade=True))).ok


async def test_stale_or_missing_account_state_no_refusal(db_session, seeded, qs):
    for acct in (account(bp="0", minutes_ago=20), None,
                 ({"daytrade_count": 3, "equity": "1000"}, WED)):  # an old PDT state
        link = await _link(db_session, acct)
        p = await _pf(db_session, link, intraday_enabled=True)
        assert (await _preview(db_session, qs, _req(p, qty=D(5), day_trade=True))).ok


async def test_sells_never_refused(db_session, seeded, qs):
    link = await _link(db_session, account(bp="0"))
    p = await _pf(db_session, intraday_enabled=True, max_day_trades_5d=0)
    held = await place_order(db_session, qs, _req(p, qty=D(5)), now=WED)  # a local fill
    assert held.status == OrderStatus.filled
    p.broker_link_id = link.id
    await db_session.flush()
    r = await _preview(db_session, qs, _req(p, side=OrderSide.sell, reduce_only=True,
                                            day_trade=True))
    assert r.ok


async def test_non_day_trade_buy_not_checked(db_session, seeded, qs):
    link = await _link(db_session, account(bp="0"))
    p = await _pf(db_session, link, intraday_enabled=True, max_day_trades_5d=0)
    assert (await _preview(db_session, qs, _req(p, day_trade=False))).ok


class _Count:
    def __init__(self, engine):
        self.engine, self.n = engine, 0

    def __enter__(self):
        event.listen(self.engine, "before_cursor_execute", self._hit)
        return self

    def __exit__(self, *a):
        event.remove(self.engine, "before_cursor_execute", self._hit)

    def _hit(self, *args):
        self.n += 1


async def test_off_portfolio_never_refused(db_session, seeded, qs):
    link = await _link(db_session, account(bp="0"))
    on = await _pf(db_session, link, intraday_enabled=True)
    await _round_trips(db_session, on, 3)
    off = await _pf(db_session, link, max_day_trades_5d=0)  # both switches off
    engine = db_session.bind.sync_engine
    with _Count(engine) as plain:
        assert (await _preview(db_session, qs, _req(off, day_trade=False))).ok
    with _Count(engine) as flagged:
        assert (await _preview(db_session, qs, _req(off, day_trade=True))).ok
    assert flagged.n == plain.n  # no day-trade query for a portfolio out of day_scope


async def _outbox(db_session):
    return list((await db_session.scalars(select(TelegramOutbox))).all())


async def test_no_outbox_on_day_trade_sells(db_session, seeded, qs, monkeypatch):
    from smtrading.engine import service as svc

    async def activate(session, qs, order, adm):  # broker routing is not under test
        return None

    link = await _link(db_session)
    on = await _pf(db_session, link, intraday_enabled=True)
    off = await _pf(db_session)
    await _round_trips(db_session, on, 2)
    await place_order(db_session, qs, _req(off, qty=D(5)), now=WED)  # local fill today
    await db_session.execute(update(Fill).where(Fill.portfolio_id == off.id)
                             .values(created_at=WED))
    off.broker_link_id = link.id
    await db_session.flush()
    monkeypatch.setattr(svc, "_activate", activate)
    sell = await place_order(db_session, qs, _req(off, side=OrderSide.sell, qty=D(5)), now=WED)
    assert sell.status != OrderStatus.rejected
    assert await _outbox(db_session) == []  # the sell warning is gone


async def test_preview_reports_day_trade_limit(db_session, seeded, qs):
    _, p = await _capped(db_session, intraday_enabled=True)
    r = await _preview(db_session, qs, _req(p, day_trade=True))
    assert r.ok is False and r.code == "DAY_TRADE_LIMIT"
    assert await _outbox(db_session) == []


# automatic entries (auto_rule_id): step 10's leverage and broker buying power apply to
# every buy entry, crypto included (not flagged day_trade); the own cap stays day_trade only

AUTO_BY = "admin@example.com"


async def _auto_rule(db_session, p, symbol="BTC-USD"):
    from smtrading.autorules import rules

    rule = await rules.create_rule(db_session, portfolio_id=p.id, symbol=symbol,
                                   model="breakout", params={"lookback_min": 20},
                                   qty=D(1), by=AUTO_BY)
    await rules.activate(db_session, rule.id, by=AUTO_BY, now=WED)
    await db_session.flush()
    return rule


async def test_crypto_auto_entry_over_non_marginable_buying_power(db_session, seeded, qs):
    link = await _link(db_session, account(bp="100000", nmbp="500"))
    p = await _pf(db_session, link, auto_rules_enabled=True)
    rule = await _auto_rule(db_session, p)
    r = await _preview(db_session, qs, _req(p, symbol="BTC-USD", qty=D(1),
                                            auto_rule_id=rule.id))
    assert not r.ok and r.code == ErrorCode.INSUFFICIENT_BUYING_POWER
    assert r.details["reason"] == "broker_buying_power"
    assert r.details["buying_power"] == "500"
    link.pdt_state, link.pdt_state_at = account(bp="0", nmbp="5000")
    await db_session.flush()
    assert (await _preview(db_session, qs, _req(p, symbol="BTC-USD", qty=D(1),
                                                auto_rule_id=rule.id))).ok


async def _levered_auto(db_session, leverage, kind="alpaca_paper"):
    p = await _levered(db_session, leverage, kind=kind)
    p.intraday_enabled, p.auto_rules_enabled = False, True
    await db_session.flush()
    return p, await _auto_rule(db_session, p)


# BTC-USD at 1,000 $, 1 EUR = 1.25 $: 1 BTC = 800 EUR; 10,000 EUR of equity:
# 26 = 20,800 EUR (over 2), 24 = 19,200 (within 2); 13 = 10,400 (over 1), 12 = 9,600


async def test_crypto_auto_entry_over_day_leverage_on_paper(db_session, seeded, qs):
    p, rule = await _levered_auto(db_session, 3)
    r = await _preview(db_session, qs, _req(p, symbol="BTC-USD", qty=D(26),
                                            auto_rule_id=rule.id))
    assert not r.ok and r.code == ErrorCode.INSUFFICIENT_BUYING_POWER
    assert r.details == {"reason": "day_trade_leverage", "leverage": "2"}
    assert (await _preview(db_session, qs, _req(p, symbol="BTC-USD", qty=D(24),
                                                auto_rule_id=rule.id))).ok


async def test_live_crypto_auto_entry_capped_at_leverage_one(db_session, seeded, qs,
                                                             monkeypatch):
    from smtrading.risk import checks

    async def no_live_checks(*a, **k):  # arming, cash cap, fuse: not under test here
        return None

    monkeypatch.setattr(checks, "live_pre_trade", no_live_checks)
    p, rule = await _levered_auto(db_session, 2, kind="alpaca_live")
    r = await _preview(db_session, qs, _req(p, symbol="BTC-USD", qty=D(13),
                                            auto_rule_id=rule.id))
    assert not r.ok and r.code == ErrorCode.INSUFFICIENT_BUYING_POWER
    assert r.details == {"reason": "day_trade_leverage", "leverage": "1"}
    assert (await _preview(db_session, qs, _req(p, symbol="BTC-USD", qty=D(12),
                                                auto_rule_id=rule.id))).ok


async def test_crypto_auto_entry_skips_the_own_day_trade_cap(db_session, seeded, qs):
    link = await _link(db_session)
    p = await _pf(db_session, link, auto_rules_enabled=True, max_day_trades_5d=1)
    await _round_trips(db_session, p, 1)
    rule = await _auto_rule(db_session, p)
    assert (await _preview(db_session, qs, _req(p, symbol="BTC-USD", qty=D(1),
                                                auto_rule_id=rule.id))).ok
