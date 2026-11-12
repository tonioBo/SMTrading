import inspect
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from smtrading.common.errors import ErrorCode, SMTError
from smtrading.engine.quotesource import FakeQuoteSource, QuoteSource
from smtrading.instruments.models import AssetClass, Instrument
from smtrading.ledger.models import AccountKind, JournalType
from smtrading.ledger.service import LineSpec, get_or_create_account, post_journal
from smtrading.marketdata.router import MarketDataRouter
from smtrading.marketdata.types import CalendarEvent
from smtrading.portfolios.service import create_portfolio
from smtrading.positions.models import Position
from smtrading.valuation.service import value_portfolio

D = Decimal


# ---------------------------------------------------------------- QuoteSource protocol


def test_market_data_router_satisfies_quote_source():
    router = MarketDataRouter(None, None, None)  # type: ignore[arg-type]
    qs: QuoteSource = router
    assert isinstance(qs, QuoteSource)
    for name in ("get_quotes", "get_fx_rate", "get_calendar"):
        proto = inspect.signature(getattr(QuoteSource, name))
        impl = inspect.signature(getattr(MarketDataRouter, name))
        assert list(proto.parameters.values()) == list(impl.parameters.values()), name
        assert inspect.iscoroutinefunction(getattr(MarketDataRouter, name))


def test_fake_quote_source_satisfies_quote_source():
    assert isinstance(FakeQuoteSource(), QuoteSource)


async def test_fake_quotes():
    qs = FakeQuoteSource()
    ts = datetime(2026, 1, 2, 15, 0, tzinfo=UTC)
    qs.set_quote("AAPL", D("199"), D("201"), D("200"), ts=ts)
    qs.set_quote("MSFT", None, None, D("400"))
    quotes = await qs.get_quotes(["AAPL", "MSFT"])
    assert quotes["AAPL"].bid == D("199") and quotes["AAPL"].ts == ts
    assert quotes["MSFT"].last == D("400") and quotes["MSFT"].ts.tzinfo is not None
    with pytest.raises(SMTError) as e:
        await qs.get_quotes(["AAPL", "NVDA"])
    assert e.value.code == ErrorCode.PROVIDER_UNAVAILABLE


async def test_fake_fx_inverse_and_same_currency():
    qs = FakeQuoteSource()
    qs.set_fx("EUR", "USD", D("1.25"))
    assert await qs.get_fx_rate("EUR", "USD") == D("1.25")
    assert await qs.get_fx_rate("USD", "EUR") == D("0.8")
    assert await qs.get_fx_rate("GBP", "GBP") == D(1)
    with pytest.raises(SMTError) as e:
        await qs.get_fx_rate("GBP", "EUR")
    assert e.value.code == ErrorCode.PROVIDER_UNAVAILABLE


async def test_fake_calendar_filters_symbols_and_dates():
    qs = FakeQuoteSource()
    ev1 = CalendarEvent("AAPL", "dividend", date(2026, 2, 1), {"amount": "0.25"})
    ev2 = CalendarEvent("AAPL", "split", date(2026, 5, 1))
    ev3 = CalendarEvent("MSFT", "earnings", date(2026, 2, 1))
    qs.set_calendar([ev2, ev3, ev1])
    got = await qs.get_calendar(["AAPL"], date(2026, 1, 1), date(2026, 3, 1))
    assert got == [ev1]


# ---------------------------------------------------------------- valuation


@pytest.fixture
async def aapl(db_session):
    instr = await db_session.get(Instrument, "AAPL")
    if instr is None:
        db_session.add(Instrument(
            symbol="AAPL", name="Apple", asset_class=AssetClass.us_equity, market="NASDAQ",
            currency="USD", lot_size=D("0.000001"), tick_size=D("0.01"), shortable=True,
        ))
        await db_session.flush()


@pytest.fixture
def qs():
    q = FakeQuoteSource()
    q.set_fx("EUR", "USD", D("1.25"))
    return q


async def _post(session, portfolio_id, kind, asset, qty):
    """Book `qty` of `asset` into the portfolio account of `kind`, against system_equity."""
    acct = await get_or_create_account(
        session, portfolio_id=portfolio_id, asset=asset, kind=kind
    )
    eq = await get_or_create_account(
        session, portfolio_id=None, asset=asset, kind=AccountKind.system_equity
    )
    await post_journal(
        session, type=JournalType.TRADE,
        lines=[LineSpec(acct.id, qty), LineSpec(eq.id, qty.copy_negate())],
        author_type="system", author_id=None, allow_negative=frozenset({kind}),
    )


async def test_cash_only(db_session, qs):
    p = await create_portfolio(db_session, name="V-cash", initial_cash=D(100000))
    v = await value_portfolio(db_session, qs, p.id)
    assert v.portfolio_id == p.id and v.base_currency == "EUR"
    assert v.cash == {"EUR": D(100000)}
    assert v.positions == []
    assert v.equity_eur == D("100000.00")
    assert v.cash_eur == D("100000.00")
    assert v.exposure_eur == D("0.00")
    assert v.buying_power_eur == D("100000.00")
    assert v.initial_req_eur == D("0.00")
    assert v.ts.tzinfo is not None


async def test_long_position_in_usd(db_session, qs, aapl):
    p = await create_portfolio(db_session, name="V-long", initial_cash=D(98400))
    await _post(db_session, p.id, AccountKind.position, "AAPL", D(10))
    db_session.add(Position(portfolio_id=p.id, symbol="AAPL", qty=D(10), avg_price=D(180)))
    await db_session.flush()
    qs.set_quote("AAPL", D("199"), D("201"), D("150"))  # mid 200 wins over last
    v = await value_portfolio(db_session, qs, p.id)
    [pos] = v.positions
    assert (pos.symbol, pos.qty, pos.avg_price, pos.mark, pos.currency) == (
        "AAPL", D(10), D(180), D(200), "USD"
    )
    assert pos.market_value == D(2000)
    assert pos.market_value_eur == D(1600)
    assert pos.unrealized_pnl == D(200)
    assert pos.unrealized_pnl_eur == D(160)
    assert v.equity_eur == D("100000.00")
    assert v.cash_eur == D("98400.00")
    assert v.exposure_eur == D("1600.00")
    assert v.initial_req_eur == D("1600.00")
    assert v.maint_req_eur == D("800.00")
    assert v.warn_req_eur == D("1200.00")
    assert v.buying_power_eur == D("98400.00")


async def test_short_with_collateral(db_session, qs, aapl):
    p = await create_portfolio(db_session, name="V-short", initial_cash=D(10000))
    await _post(db_session, p.id, AccountKind.position, "AAPL", D(-5))
    await _post(db_session, p.id, AccountKind.collateral, "USD", D(1000))
    db_session.add(Position(portfolio_id=p.id, symbol="AAPL", qty=D(-5), avg_price=D(210)))
    await db_session.flush()
    qs.set_quote("AAPL", D("199"), D("201"), None)
    v = await value_portfolio(db_session, qs, p.id)
    [pos] = v.positions
    assert pos.qty == D(-5)
    assert pos.market_value == D(-1000)
    assert pos.unrealized_pnl == D(50)  # sold at 210, now 200
    assert v.collateral == {"USD": D(1000)}
    assert v.equity_eur == D("10000.00")  # collateral 800 EUR - short 800 EUR = 0
    assert v.exposure_eur == D("800.00")


async def test_margin_loan_counts_in_equity(db_session, qs):
    p = await create_portfolio(db_session, name="V-loan", initial_cash=D(1000))
    await _post(db_session, p.id, AccountKind.margin_loan, "USD", D(-500))
    v = await value_portfolio(db_session, qs, p.id)
    assert v.margin_loan == {"USD": D(-500)}
    assert v.equity_eur == D("600.00")


async def test_last_used_when_bid_or_ask_missing(db_session, qs, aapl):
    p = await create_portfolio(db_session, name="V-last", initial_cash=D(0))
    await _post(db_session, p.id, AccountKind.position, "AAPL", D(2))
    qs.set_quote("AAPL", D("199"), None, D("190"))
    v = await value_portfolio(db_session, qs, p.id)
    [pos] = v.positions
    assert pos.mark == D(190)
    assert pos.avg_price == D(0)  # no projection row
    assert v.exposure_eur == D("304.00")


async def test_leverage_two(db_session, qs, aapl):
    p = await create_portfolio(
        db_session, name="V-lev", initial_cash=D(98400), leverage=D(2)
    )
    await _post(db_session, p.id, AccountKind.position, "AAPL", D(10))
    qs.set_quote("AAPL", D("199"), D("201"), None)
    v = await value_portfolio(db_session, qs, p.id)
    assert v.equity_eur == D("100000.00")
    assert v.initial_req_eur == D("800.00")
    assert v.maint_req_eur == D("400.00")
    assert v.warn_req_eur == D("600.00")
    assert v.buying_power_eur == D("198400.00")  # 100000 * 2 - 1600


async def test_missing_quote(db_session, qs, aapl):
    p = await create_portfolio(db_session, name="V-noquote", initial_cash=D(1000))
    await _post(db_session, p.id, AccountKind.position, "AAPL", D(1))
    with pytest.raises(SMTError) as e:
        await value_portfolio(db_session, qs, p.id)
    assert e.value.code == ErrorCode.PROVIDER_UNAVAILABLE


async def test_quote_without_any_price(db_session, qs, aapl):
    p = await create_portfolio(db_session, name="V-noprice", initial_cash=D(1000))
    await _post(db_session, p.id, AccountKind.position, "AAPL", D(1))
    qs.set_quote("AAPL", None, None, None)
    with pytest.raises(SMTError) as e:
        await value_portfolio(db_session, qs, p.id)
    assert e.value.code == ErrorCode.PROVIDER_UNAVAILABLE
    assert e.value.details == {"symbol": "AAPL"}


async def test_unknown_portfolio(db_session, qs):
    import uuid

    with pytest.raises(SMTError) as e:
        await value_portfolio(db_session, qs, uuid.uuid4())
    assert e.value.code == ErrorCode.NOT_FOUND
