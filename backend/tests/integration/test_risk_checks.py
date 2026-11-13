import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from smtrading.common.errors import ErrorCode
from smtrading.engine.models import OrderSide, OrderType
from smtrading.engine.quotesource import FakeQuoteSource
from smtrading.instruments.models import Instrument
from smtrading.ledger.models import AccountKind, JournalType
from smtrading.ledger.service import LineSpec, get_or_create_account, post_journal
from smtrading.portfolios.models import PortfolioStatus
from smtrading.portfolios.service import create_portfolio
from smtrading.positions.models import Position
from smtrading.risk.checks import pre_trade_check
from smtrading.risk.models import RiskProfile
from smtrading.risk.settings import set_kill_switch
from smtrading.seed import run_seed

D = Decimal


@pytest.fixture
async def seeded(db_session):
    await run_seed(db_session)


@pytest.fixture
def qs():
    q = FakeQuoteSource()
    q.set_fx("EUR", "USD", D("1.25"))
    q.set_quote("AAPL", D("199.90"), D("200.00"), D("199.95"))
    q.set_quote("ENI.MI", D("14.990"), D("15.000"), D("14.995"))
    q.set_quote("BTC-USD", D("50000"), D("50010"), D("50005"))
    return q


async def _portfolio(session, cash=100000, profile=None, **kw):
    p = await create_portfolio(
        session, name=f"R-{uuid.uuid4().hex[:8]}", initial_cash=D(cash), **kw
    )
    if profile is not None:
        session.add(profile)
        await session.flush()
        p.risk_profile_id = profile.id
        await session.flush()
    return p


def _profile(**kw):
    return RiskProfile(name=f"P-{uuid.uuid4().hex[:8]}", **kw)


async def _book(session, portfolio_id, kind, asset, qty):
    acct = await get_or_create_account(session, portfolio_id=portfolio_id, asset=asset, kind=kind)
    eq = await get_or_create_account(
        session, portfolio_id=None, asset=asset, kind=AccountKind.system_equity
    )
    await post_journal(
        session, type=JournalType.TRADE,
        lines=[LineSpec(acct.id, qty), LineSpec(eq.id, qty.copy_negate())],
        author_type="system", author_id=None, allow_negative=frozenset({kind}),
    )


async def _position(session, portfolio, symbol, qty, avg):
    await _book(session, portfolio.id, AccountKind.position, symbol, qty)
    session.add(Position(portfolio_id=portfolio.id, symbol=symbol, qty=qty, avg_price=avg))
    await session.flush()


async def _check(session, qs, portfolio, symbol, side, *, qty=None, notional=None,
                 order_type=OrderType.market, limit_price=None, stop_price=None):
    instrument = await session.get(Instrument, symbol)
    return await pre_trade_check(
        session, qs, portfolio=portfolio, instrument=instrument, side=side, qty=qty,
        notional=notional, order_type=order_type, limit_price=limit_price,
        stop_price=stop_price,
    )


def _rejected(res, code):
    assert res.ok is False
    assert res.code == code
    assert res.message


async def test_ok_usd_buy_populates_estimates(db_session, seeded, qs):
    p = await _portfolio(db_session)
    res = await _check(db_session, qs, p, "AAPL", OrderSide.buy, qty=D(10))
    assert res.ok is True and res.code is None and res.message is None
    assert res.estimated_price == D("200.00")
    assert res.estimated_notional_eur == D(1600)
    # default rule: 1 EUR + 0.1% of 1600 EUR
    assert res.estimated_fee.amount == D("2.60") and res.estimated_fee.currency == "EUR"
    assert res.estimated_fee.rule_id is not None
    # 2000 USD bought with 1600 EUR at 0.25%
    assert res.estimated_fx_fee_eur == D("4.00")
    assert res.buying_power_after_eur == D("98393.40")


async def test_ok_notional_order_estimates_qty(db_session, seeded, qs):
    p = await _portfolio(db_session)
    res = await _check(db_session, qs, p, "ENI.MI", OrderSide.buy, notional=D(1000))
    assert res.ok is True
    assert res.estimated_price == D("15.000")
    # floor(1000 / 15) = 66 shares
    assert res.estimated_notional_eur == D(990)
    assert res.estimated_fx_fee_eur == D(0)


async def test_kill_switch(db_session, seeded, qs):
    p = await _portfolio(db_session)
    await set_kill_switch(db_session, True)
    res = await _check(db_session, qs, p, "AAPL", OrderSide.buy, qty=D(1))
    _rejected(res, ErrorCode.KILL_SWITCH_ACTIVE)


async def test_portfolio_locked(db_session, seeded, qs):
    p = await _portfolio(db_session)
    p.status = PortfolioStatus.locked
    await db_session.flush()
    res = await _check(db_session, qs, p, "AAPL", OrderSide.buy, qty=D(1))
    _rejected(res, ErrorCode.PORTFOLIO_LOCKED)


@pytest.mark.parametrize("field,value", [
    ("allowed_symbols", ["MSFT"]),
    ("allowed_asset_classes", ["eu_equity"]),
    ("allowed_markets", ["XETRA"]),
])
async def test_symbol_not_allowed(db_session, seeded, qs, field, value):
    p = await _portfolio(db_session, profile=_profile(**{field: value}))
    res = await _check(db_session, qs, p, "AAPL", OrderSide.buy, qty=D(1))
    _rejected(res, ErrorCode.SYMBOL_NOT_ALLOWED)


async def test_whitelist_match_allows(db_session, seeded, qs):
    p = await _portfolio(db_session, profile=_profile(
        allowed_symbols=["AAPL"], allowed_asset_classes=["us_equity"], allowed_markets=["NASDAQ"],
    ))
    res = await _check(db_session, qs, p, "AAPL", OrderSide.buy, qty=D(1))
    assert res.ok is True


@pytest.mark.parametrize("kw", [
    {"qty": None, "notional": None},
    {"qty": D(1), "notional": D(100)},
    {"qty": D("1.5")},  # ENI.MI lot is 1
    {"qty": D(0)},
    {"notional": D(-5)},
    {"qty": D(1), "order_type": OrderType.limit, "limit_price": D("15.0005")},  # tick 0.001
    {"qty": D(1), "order_type": OrderType.limit},  # limit price missing
    {"qty": D(1), "order_type": OrderType.stop},  # stop price missing
    {"qty": D(1), "order_type": OrderType.stop_limit, "stop_price": D(15)},
    {"notional": D(1)},  # less than one lot
])
async def test_invalid_order(db_session, seeded, qs, kw):
    p = await _portfolio(db_session)
    res = await _check(db_session, qs, p, "ENI.MI", OrderSide.buy, **kw)
    _rejected(res, ErrorCode.INVALID_ORDER)


async def test_stale_quote_market(db_session, seeded, qs):
    p = await _portfolio(db_session)
    old = datetime.now(UTC) - timedelta(seconds=121)  # us_equity threshold 120s
    qs.set_quote("AAPL", D("199.90"), D("200.00"), D("199.95"), ts=old)
    res = await _check(db_session, qs, p, "AAPL", OrderSide.buy, qty=D(1))
    _rejected(res, ErrorCode.STALE_QUOTE)


async def test_stale_quote_limit_uses_limit_price(db_session, seeded, qs):
    p = await _portfolio(db_session)
    old = datetime.now(UTC) - timedelta(hours=1)
    qs.set_quote("AAPL", D("199.90"), D("200.00"), D("199.95"), ts=old)
    res = await _check(
        db_session, qs, p, "AAPL", OrderSide.buy, qty=D(10),
        order_type=OrderType.limit, limit_price=D("190.00"),
    )
    assert res.ok is True
    assert res.estimated_price == D("190.00")
    assert res.estimated_notional_eur == D(1520)


async def test_missing_quote_market(db_session, seeded, qs):
    p = await _portfolio(db_session)
    res = await _check(db_session, qs, p, "MSFT", OrderSide.buy, qty=D(1))
    _rejected(res, ErrorCode.PROVIDER_UNAVAILABLE)


async def test_missing_quote_stop_uses_stop_price(db_session, seeded, qs):
    p = await _portfolio(db_session)
    res = await _check(
        db_session, qs, p, "MSFT", OrderSide.buy, qty=D(2),
        order_type=OrderType.stop, stop_price=D("400.00"),
    )
    assert res.ok is True
    assert res.estimated_price == D("400.00")
    assert res.estimated_notional_eur == D(640)


async def test_reduce_only(db_session, seeded, qs):
    p = await _portfolio(db_session)
    await _position(db_session, p, "ENI.MI", D(10), D(15))
    p.status = PortfolioStatus.reduce_only
    await db_session.flush()

    ok = await _check(db_session, qs, p, "ENI.MI", OrderSide.sell, qty=D(4))
    assert ok.ok is True
    ok = await _check(db_session, qs, p, "ENI.MI", OrderSide.sell, qty=D(10))
    assert ok.ok is True
    _rejected(
        await _check(db_session, qs, p, "ENI.MI", OrderSide.buy, qty=D(1)),
        ErrorCode.REDUCE_ONLY,
    )
    _rejected(  # crosses zero: long 10 -> short 5
        await _check(db_session, qs, p, "ENI.MI", OrderSide.sell, qty=D(15)),
        ErrorCode.REDUCE_ONLY,
    )
    _rejected(  # flat symbol: any order opens exposure
        await _check(db_session, qs, p, "AAPL", OrderSide.buy, qty=D(1)),
        ErrorCode.REDUCE_ONLY,
    )


async def test_short_not_allowed_by_instrument(db_session, seeded, qs):
    p = await _portfolio(db_session)  # BTC-USD is not shortable
    res = await _check(db_session, qs, p, "BTC-USD", OrderSide.sell, qty=D("0.1"))
    _rejected(res, ErrorCode.SHORT_NOT_ALLOWED)


async def test_short_not_allowed_by_profile(db_session, seeded, qs):
    p = await _portfolio(db_session, profile=_profile(short_allowed=False))
    res = await _check(db_session, qs, p, "AAPL", OrderSide.sell, qty=D(1))
    _rejected(res, ErrorCode.SHORT_NOT_ALLOWED)


async def test_short_allowed(db_session, seeded, qs):
    p = await _portfolio(db_session)
    res = await _check(db_session, qs, p, "AAPL", OrderSide.sell, qty=D(1))
    assert res.ok is True
    assert res.estimated_price == D("199.90")


async def test_max_order_notional(db_session, seeded, qs):
    p = await _portfolio(db_session, profile=_profile(max_order_notional_eur=D(1500)))
    _rejected(
        await _check(db_session, qs, p, "AAPL", OrderSide.buy, qty=D(10)),  # 1600 EUR
        ErrorCode.RISK_LIMIT_EXCEEDED,
    )
    assert (await _check(db_session, qs, p, "AAPL", OrderSide.buy, qty=D(9))).ok is True


async def test_max_position_pct(db_session, seeded, qs):
    p = await _portfolio(db_session, profile=_profile(max_position_pct=D(10)))
    # weight uses the mark (mid 14.995): 600 * 14.995 = 8997 EUR = 9% of equity
    assert (await _check(db_session, qs, p, "ENI.MI", OrderSide.buy, qty=D(600))).ok is True
    _rejected(  # 700 * 14.995 = 10496.5 EUR > 10% of equity 100000
        await _check(db_session, qs, p, "ENI.MI", OrderSide.buy, qty=D(700)),
        ErrorCode.RISK_LIMIT_EXCEEDED,
    )


async def test_insufficient_buying_power(db_session, seeded, qs):
    p = await _portfolio(db_session, cash=1000)
    res = await _check(db_session, qs, p, "ENI.MI", OrderSide.buy, qty=D(67))  # 1005 EUR
    _rejected(res, ErrorCode.INSUFFICIENT_BUYING_POWER)
    # 66 shares: 990 + 1.99 fee = 991.99 <= 1000
    assert (await _check(db_session, qs, p, "ENI.MI", OrderSide.buy, qty=D(66))).ok is True


async def test_reducing_order_ok_with_negative_buying_power(db_session, seeded, qs):
    p = await _portfolio(db_session, cash=0)
    await _position(db_session, p, "ENI.MI", D(1000), D(15))
    await _book(db_session, p.id, AccountKind.cash, "EUR", D(-20000))
    # equity = -20000 + 14995 < 0 -> buying power negative

    res = await _check(db_session, qs, p, "ENI.MI", OrderSide.sell, qty=D(100))
    assert res.ok is True
    assert res.buying_power_after_eur < 0
    _rejected(
        await _check(db_session, qs, p, "ENI.MI", OrderSide.buy, qty=D(1)),
        ErrorCode.INSUFFICIENT_BUYING_POWER,
    )
