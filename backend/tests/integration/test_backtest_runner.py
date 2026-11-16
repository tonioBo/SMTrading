"""Backtest enqueue limits and the BacktestRunner (Plan 6 Task 11)."""

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from smtrading.agents.service import create_agent
from smtrading.backtest.models import Backtest, BacktestStatus
from smtrading.backtest.runner import BacktestRunner, enqueue_backtest, zero_fee_rule
from smtrading.common.db import make_engine
from smtrading.common.errors import ErrorCode, SMTError
from smtrading.engine.quotesource import FakeMarketData
from smtrading.fees.models import FeeRule, FeeScope, FeeSide
from smtrading.ledger.models import Journal
from smtrading.marketdata.types import Bar
from smtrading.portfolios.models import Portfolio
from smtrading.seed import run_seed

D = Decimal
T0 = datetime(2024, 1, 1, tzinfo=UTC)
SYM = "AAPL"


@pytest.fixture
async def maker(fresh_db):
    engine = make_engine(fresh_db)
    sm = async_sessionmaker(engine, expire_on_commit=False)
    async with sm() as s:
        await run_seed(s)
        await s.commit()
    yield sm
    await engine.dispose()


def _bars(n=60):
    return [Bar(ts=T0 + timedelta(days=i), open=D(100 + i), high=D(101 + i), low=D(99 + i),
                close=D(100 + i), volume=D(1)) for i in range(n)]


async def _agent(maker, name="a"):
    async with maker() as s:
        agent, _ = await create_agent(s, name)
        await s.commit()
    return agent.id


async def _enqueue(maker, agent, **kw):
    args = {"strategy": "buy_and_hold", "symbols": [SYM], "timeframe": "1d", "start": T0,
            "end": T0 + timedelta(days=60), "initial_cash": D(10000), "params": {},
            "portfolio_id": None}
    args.update(kw)
    async with maker() as s:
        bt = await enqueue_backtest(s, agent, **args)
        await s.commit()
    return bt.id


async def test_queued_to_completed(maker):
    agent = await _agent(maker)
    bid = await _enqueue(maker, agent)
    md = FakeMarketData()
    md.set_bars(SYM, _bars())
    async with maker() as s:
        before = (await s.execute(select(Journal))).scalars().all()
    assert await BacktestRunner(maker, md).run_once() == 1
    async with maker() as s:
        bt = await s.get(Backtest, bid)
        assert bt.status == BacktestStatus.completed and bt.error is None
        assert bt.started_at and bt.finished_at
        assert bt.result["metrics"]["total_return_pct"].startswith("58.")
        assert len(bt.result["equity"]) == 60 and bt.result["slippage_bps"] == 5
        assert len((await s.execute(select(Journal))).scalars().all()) == len(before)
    assert await BacktestRunner(maker, md).run_once() == 0


async def test_error_marks_failed(maker):
    agent = await _agent(maker)
    bid = await _enqueue(maker, agent)
    await BacktestRunner(maker, FakeMarketData()).run_once()  # no bars: SYMBOL_NOT_FOUND
    async with maker() as s:
        bt = await s.get(Backtest, bid)
        assert bt.status == BacktestStatus.failed and "SYMBOL_NOT_FOUND" in bt.error
        assert bt.result is None


async def test_timeout_marks_failed(maker):
    agent = await _agent(maker)
    bid = await _enqueue(maker, agent)

    class Slow(FakeMarketData):
        async def get_bars(self, *a, **k):
            import asyncio
            await asyncio.sleep(5)

    await BacktestRunner(maker, Slow(), timeout_s=0.05).run_once()
    async with maker() as s:
        bt = await s.get(Backtest, bid)
        assert bt.status == BacktestStatus.failed and "timeout" in bt.error


async def test_limits(maker):
    agent = await _agent(maker)

    async def code(**kw):
        with pytest.raises(SMTError) as e:
            await _enqueue(maker, agent, **kw)
        return e.value.code

    assert await code(end=T0 + timedelta(days=6 * 366)) == ErrorCode.RISK_LIMIT_EXCEEDED
    assert await code(timeframe="1h", end=T0 + timedelta(days=91)) == ErrorCode.RISK_LIMIT_EXCEEDED
    assert await code(symbols=["NOPE"]) == ErrorCode.SYMBOL_NOT_FOUND
    assert await code(strategy="sma_cross", params={"fast": 9, "slow": 3}) \
        == ErrorCode.INVALID_ORDER
    await _enqueue(maker, agent, timeframe="1h", end=T0 + timedelta(days=90))
    await _enqueue(maker, agent, end=T0 + timedelta(days=5 * 366))
    assert await code() == ErrorCode.RATE_LIMITED
    other = await _agent(maker, "b")
    await _enqueue(maker, other)  # limits are per agent


async def test_runner_respects_running_cap_and_requeues_stale(maker):
    agent = await _agent(maker)
    a = await _enqueue(maker, agent)
    b = await _enqueue(maker, agent)
    async with maker() as s:
        for bid, age in ((a, 20), (b, 0)):
            bt = await s.get(Backtest, bid)
            bt.status = BacktestStatus.running
            bt.started_at = datetime.now(UTC) - timedelta(minutes=age)
        await s.commit()
    runner = BacktestRunner(maker, FakeMarketData())
    assert await runner.requeue_stale() == 1
    async with maker() as s:
        assert (await s.get(Backtest, a)).status == BacktestStatus.queued
        assert (await s.get(Backtest, b)).status == BacktestStatus.running


async def test_stale_requeue_is_periodic_without_restart(maker):
    agent = await _agent(maker)
    bid = await _enqueue(maker, agent)
    md = FakeMarketData()
    md.set_bars(SYM, _bars())
    async with maker() as s:
        bt = await s.get(Backtest, bid)
        bt.status = BacktestStatus.running
        bt.started_at = datetime.now(UTC) - timedelta(minutes=7)  # past timeout + margin
        await s.commit()
    runner = BacktestRunner(maker, md, poll_s=0.02, requeue_every_s=0.05)
    task = asyncio.create_task(runner.run())
    try:
        for _ in range(100):
            await asyncio.sleep(0.05)
            async with maker() as s:
                if (await s.get(Backtest, bid)).status == BacktestStatus.completed:
                    break
        else:
            pytest.fail("stale backtest never requeued and completed")
    finally:
        task.cancel()


async def test_one_claim_per_iteration(maker):
    agent = await _agent(maker)
    a = await _enqueue(maker, agent)
    b = await _enqueue(maker, agent)
    md = FakeMarketData()
    md.set_bars(SYM, _bars())
    runner = BacktestRunner(maker, md)
    assert await runner.run_once() == 1
    async with maker() as s:
        assert (await s.get(Backtest, a)).status == BacktestStatus.completed
        assert (await s.get(Backtest, b)).status == BacktestStatus.queued
    assert await runner.run_once() == 1


async def test_slippage_precedence_explicit_portfolio_default(maker):
    agent = await _agent(maker)
    async with maker() as s:
        pf = (await s.execute(select(Portfolio))).scalars().first()
        pf.slippage_bps = 100
        await s.commit()
        pid = pf.id
    md = FakeMarketData()
    md.set_bars(SYM, _bars())
    runner = BacktestRunner(maker, md)

    async def slip(**kw):
        bid = await _enqueue(maker, agent, **kw)
        await runner.run_once()
        async with maker() as s:
            bt = await s.get(Backtest, bid)
            assert bt.status == BacktestStatus.completed, bt.error
            return bt.result["slippage_bps"]

    assert await slip(portfolio_id=pid) == 100
    assert await slip(portfolio_id=pid, params={"slippage_bps": 7}) == 7
    assert await slip() == 5


async def test_per_symbol_fee_rules_and_short_span_warning(maker):
    agent = await _agent(maker)
    async with maker() as s:
        s.add(FeeRule(name="crypto", scope=FeeScope.asset_class, scope_ref="crypto",
                      side=FeeSide.both, currency="EUR", pct=D("0.5")))
        await s.commit()
    md = FakeMarketData()
    md.set_bars(SYM, _bars())
    md.set_bars("BTC-USD", _bars())
    bid = await _enqueue(maker, agent, symbols=[SYM, "BTC-USD"])
    await BacktestRunner(maker, md).run_once()
    async with maker() as s:
        bt = await s.get(Backtest, bid)
        assert bt.status == BacktestStatus.completed, bt.error
        ids = bt.result["fee_rule_ids"]
        assert ids[SYM] != ids["BTC-USD"]
        assert "cagr_annualised_from_short_span" in bt.warnings
        assert "fee_currency_unconverted:EUR->USD" in bt.warnings  # no EUR/USD in the fake


async def test_backtest_fee_rule_per_portfolio_asset_class(maker):
    """The backtest resolves fees per symbol with the same resolver: the portfolio rule of
    the symbol's asset class wins over the portfolio's plain rule."""
    agent = await _agent(maker)
    async with maker() as s:
        pf = (await s.execute(select(Portfolio))).scalars().first()
        plain = FeeRule(name="tutte", scope=FeeScope.portfolio, scope_ref=str(pf.id),
                        side=FeeSide.both, currency="EUR", pct=D("0.1"))
        crypto = FeeRule(name="crypto", scope=FeeScope.portfolio, scope_ref=str(pf.id),
                         asset_class="crypto", side=FeeSide.both, currency="EUR", pct=D("1"))
        s.add_all([plain, crypto])
        await s.commit()
        pid, plain_id, crypto_id = pf.id, str(plain.id), str(crypto.id)
    md = FakeMarketData()
    md.set_bars(SYM, _bars())
    md.set_bars("BTC-USD", _bars())
    bid = await _enqueue(maker, agent, symbols=[SYM, "BTC-USD"], portfolio_id=pid)
    await BacktestRunner(maker, md).run_once()
    async with maker() as s:
        bt = await s.get(Backtest, bid)
        assert bt.status == BacktestStatus.completed, bt.error
        assert bt.result["fee_rule_ids"] == {SYM: plain_id, "BTC-USD": crypto_id}


async def test_fee_currency_conversion(maker):
    md = FakeMarketData()
    md.set_fx("EUR", "USD", D("1.25"))
    runner = BacktestRunner(maker, md)
    rule = replace(zero_fee_rule(), fixed=D(2), pct=D(1), fx_pct=D("0.25"), min_fee=D(1),
                   max_fee=D(20))
    warnings: list[str] = []
    out = await runner._in_instrument_currency(rule, "USD", warnings)
    assert (out.currency, out.fixed, out.pct, out.min_fee, out.max_fee) == (
        "USD", D("2.50"), D("1.25"), D("1.25"), D("25.00"))
    assert warnings == []
    same = await runner._in_instrument_currency(rule, "EUR", warnings)
    assert same is rule
