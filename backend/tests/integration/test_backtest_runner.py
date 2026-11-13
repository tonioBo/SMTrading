"""Backtest enqueue limits and the BacktestRunner (Plan 6 Task 11)."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from smtrading.agents.service import create_agent
from smtrading.backtest.models import Backtest, BacktestStatus
from smtrading.backtest.runner import BacktestRunner, enqueue_backtest
from smtrading.common.db import make_engine
from smtrading.common.errors import ErrorCode, SMTError
from smtrading.engine.quotesource import FakeMarketData
from smtrading.ledger.models import Journal
from smtrading.marketdata.types import Bar
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
