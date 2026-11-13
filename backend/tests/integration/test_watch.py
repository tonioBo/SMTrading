"""Watchlists, price alerts and the AlertEvaluator (Plan 6 Task 10)."""

import asyncio
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from redis.asyncio import Redis
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from smtrading.agents.service import create_agent
from smtrading.common.db import make_engine
from smtrading.common.errors import ErrorCode, SMTError
from smtrading.engine.quotesource import FakeQuoteSource
from smtrading.events import dispatch_post_commit
from smtrading.marketdata.streaming import TRACKED_KEY
from smtrading.marketdata.types import Quote
from smtrading.risk.models import Notification
from smtrading.seed import run_seed
from smtrading.watch import service as watch
from smtrading.watch.evaluator import AlertEvaluator
from smtrading.watch.models import Alert, AlertCondition

D = Decimal
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


@pytest.fixture
async def redis(redis_url):
    r = Redis.from_url(redis_url)
    await r.flushdb()
    yield r
    await r.aclose()


@pytest.fixture
def qs():
    q = FakeQuoteSource()
    q.set_quote(SYM, D("99.90"), D("100.10"), D(100))
    q.set_quote("MSFT", D("399"), D("401"), None)
    return q


async def _agent(maker, name="agent-a"):
    async with maker() as s:
        agent, _ = await create_agent(s, name)
        await s.commit()
    return agent.id


async def _commit(maker, redis, fn):
    """Run `fn(session)` in a transaction and dispatch its post-commit effects."""
    async with maker() as s:
        result = await fn(s)
        await s.commit()
        await dispatch_post_commit(s, redis)
    return result


def _quote(last=None, bid=None, ask=None, symbol=SYM) -> Quote:
    return Quote(symbol=symbol, bid=bid, ask=ask, last=last, ts=datetime.now(UTC),
                 provider="fake")


async def _tracked(redis, source: str) -> set[str]:
    return {m.decode() for m in await redis.smembers(f"{TRACKED_KEY}:{source}")}


async def _alert(maker, alert_id) -> Alert:
    async with maker() as s:
        return await s.get(Alert, alert_id)


async def _price_alerts(maker) -> list[Notification]:
    async with maker() as s:
        return list(await s.scalars(
            select(Notification).where(Notification.kind == "price_alert")
            .order_by(Notification.id)
        ))


# -- watchlists ------------------------------------------------------------------------


async def test_add_to_watchlist_tracks_and_remove_untracks(maker, redis):
    aid = await _agent(maker)
    wl = await _commit(maker, redis, lambda s: watch.create_watchlist(s, aid, "tech"))
    other = await _commit(maker, redis, lambda s: watch.create_watchlist(s, aid, "other"))

    out = await _commit(maker, redis, lambda s: watch.add_to_watchlist(
        s, aid, wl["id"], ["aapl", "BTC-USD", "AAPL"]))
    assert out["symbols"] == ["AAPL", "BTC-USD"]
    assert await _tracked(redis, "watchlists") == {"AAPL", "BTC-USD"}
    assert {"AAPL", "BTC-USD"} <= {m.decode() for m in await redis.smembers(TRACKED_KEY)}

    await _commit(maker, redis, lambda s: watch.add_to_watchlist(s, aid, other["id"], ["AAPL"]))
    await _commit(maker, redis, lambda s: watch.remove_from_watchlist(
        s, aid, wl["id"], ["AAPL", "BTC-USD"]))
    assert await _tracked(redis, "watchlists") == {"AAPL"}  # still in "other"

    await _commit(maker, redis, lambda s: watch.delete_watchlist(s, aid, other["id"]))
    assert await _tracked(redis, "watchlists") == set()

    async with maker() as s:
        lists = await watch.list_watchlists(s, aid)
    assert [(w["name"], w["symbols"]) for w in lists] == [("tech", [])]


async def test_watchlist_errors_and_limits(maker, redis, monkeypatch):
    aid = await _agent(maker)
    bid = await _agent(maker, "agent-b")
    wl = await _commit(maker, redis, lambda s: watch.create_watchlist(s, aid, "tech"))

    async with maker() as s:
        with pytest.raises(SMTError) as e:
            await watch.create_watchlist(s, aid, "tech")
        assert e.value.code == ErrorCode.INVALID_ORDER
    async with maker() as s:
        with pytest.raises(SMTError) as e:
            await watch.add_to_watchlist(s, bid, wl["id"], ["AAPL"])
        assert e.value.code == ErrorCode.FORBIDDEN
    async with maker() as s:
        with pytest.raises(SMTError) as e:
            await watch.add_to_watchlist(s, aid, wl["id"], ["NOPE-XYZ"])
        assert e.value.code == ErrorCode.SYMBOL_NOT_FOUND

    monkeypatch.setattr(watch, "MAX_WATCHLIST_SYMBOLS", 1)
    await _commit(maker, redis, lambda s: watch.add_to_watchlist(s, aid, wl["id"], ["AAPL"]))
    async with maker() as s:
        with pytest.raises(SMTError) as e:
            await watch.add_to_watchlist(s, aid, wl["id"], ["BTC-USD"])
        assert e.value.code == ErrorCode.RISK_LIMIT_EXCEEDED

    monkeypatch.setattr(watch, "MAX_WATCHLISTS", 1)
    async with maker() as s:
        with pytest.raises(SMTError) as e:
            await watch.create_watchlist(s, aid, "second")
        assert e.value.code == ErrorCode.RISK_LIMIT_EXCEEDED


# -- alerts ----------------------------------------------------------------------------


async def test_create_alert_sets_ref_price_and_tracks(maker, redis, qs):
    aid = await _agent(maker)
    alert = await _commit(maker, redis, lambda s: watch.create_alert(
        s, qs, aid, "aapl", AlertCondition.change_pct_below, D(-5), note="dip"))
    assert alert.symbol == SYM and alert.ref_price == D(100) and alert.status == "active"
    assert await _tracked(redis, "alerts") == {SYM}

    # mid when there is no last
    msft = await _commit(maker, redis, lambda s: watch.create_alert(
        s, qs, aid, "MSFT", AlertCondition.change_pct_above, D(5)))
    assert msft.ref_price == D(400)

    async with maker() as s:
        with pytest.raises(SMTError) as e:  # no quote for a change_pct alert
            await watch.create_alert(s, qs, aid, "BTC-USD", AlertCondition.change_pct_above,
                                     D(5))
        assert e.value.code == ErrorCode.INVALID_ORDER
    async with maker() as s:
        with pytest.raises(SMTError) as e:
            await watch.create_alert(s, qs, aid, SYM, AlertCondition.price_above, D(0))
        assert e.value.code == ErrorCode.INVALID_ORDER


async def test_alert_of_another_agent_is_forbidden(maker, redis, qs):
    aid = await _agent(maker)
    bid = await _agent(maker, "agent-b")
    alert = await _commit(maker, redis, lambda s: watch.create_alert(
        s, qs, aid, SYM, AlertCondition.price_above, D(110)))
    async with maker() as s:
        with pytest.raises(SMTError) as e:
            await watch.delete_alert(s, bid, alert.id)
        assert e.value.code == ErrorCode.FORBIDDEN
        assert await watch.list_alerts(s, bid) == []

    await _commit(maker, redis, lambda s: watch.delete_alert(s, aid, alert.id))
    assert (await _alert(maker, alert.id)).status == "deleted"
    assert await _tracked(redis, "alerts") == set()
    async with maker() as s:
        assert await watch.list_alerts(s, aid) == []


async def test_active_alert_limit(maker, redis, qs, monkeypatch):
    aid = await _agent(maker)
    assert watch.MAX_ACTIVE_ALERTS == 100
    for i in range(100):
        await _commit(maker, redis, lambda s, i=i: watch.create_alert(
            s, qs, aid, SYM, AlertCondition.price_above, D(200 + i)))
    async with maker() as s:
        with pytest.raises(SMTError) as e:
            await watch.create_alert(s, qs, aid, SYM, AlertCondition.price_above, D(999))
        assert e.value.code == ErrorCode.RISK_LIMIT_EXCEEDED

    # a deleted alert frees a slot
    async with maker() as s:
        first = (await watch.list_alerts(s, aid, status="active"))[0]
    await _commit(maker, redis, lambda s: watch.delete_alert(s, aid, first.id))
    await _commit(maker, redis, lambda s: watch.create_alert(
        s, qs, aid, SYM, AlertCondition.price_above, D(999)))


# -- evaluator -------------------------------------------------------------------------


async def test_price_above_triggers_on_tick_once(maker, redis, qs):
    aid = await _agent(maker)
    alert = await _commit(maker, redis, lambda s: watch.create_alert(
        s, qs, aid, SYM, AlertCondition.price_above, D(105)))
    ev = AlertEvaluator(maker, redis, qs)
    await ev.sweep()  # price 100: nothing, but learns the active symbols
    assert await _price_alerts(maker) == []

    assert await ev.on_tick(SYM, _quote(last=D("104.99"))) == 0
    assert await ev.on_tick(SYM, _quote(last=D("105.5"))) == 1
    assert await ev.on_tick(SYM, _quote(last=D(106))) == 0  # once only

    row = await _alert(maker, alert.id)
    assert row.status == "triggered" and row.triggered_price == D("105.5")
    assert row.triggered_at is not None
    [n] = await _price_alerts(maker)
    assert n.agent_id == str(aid) and n.portfolio_id is None
    assert n.payload == {"alert_id": str(alert.id), "symbol": SYM,
                         "condition": "price_above", "value": "105", "price": "105.5"}
    assert await _tracked(redis, "alerts") == set()


async def test_change_pct_below_uses_ref_price(maker, redis, qs):
    aid = await _agent(maker)
    alert = await _commit(maker, redis, lambda s: watch.create_alert(
        s, qs, aid, SYM, AlertCondition.change_pct_below, D(-5)))
    ev = AlertEvaluator(maker, redis, qs)
    await ev.sweep()
    assert await ev.on_tick(SYM, _quote(last=D("95.01"))) == 0  # -4.99%
    # no last: mid of 94.9/95.1 = 95 -> -5%
    assert await ev.on_tick(SYM, _quote(bid=D("94.9"), ask=D("95.1"))) == 1
    row = await _alert(maker, alert.id)
    assert row.status == "triggered" and row.triggered_price == D(95)


async def test_symbol_without_ticks_triggers_via_sweep(maker, redis, qs):
    aid = await _agent(maker)
    alert = await _commit(maker, redis, lambda s: watch.create_alert(
        s, qs, aid, SYM, AlertCondition.price_below, D(90)))
    ev = AlertEvaluator(maker, redis, qs)
    await ev.sweep()
    assert (await _alert(maker, alert.id)).status == "active"
    qs.set_quote(SYM, D("88.9"), D("89.1"), D(89))
    await ev.sweep()
    assert (await _alert(maker, alert.id)).status == "triggered"
    assert len(await _price_alerts(maker)) == 1


async def test_concurrent_evaluators_trigger_once(maker, redis, qs):
    aid = await _agent(maker)
    await _commit(maker, redis, lambda s: watch.create_alert(
        s, qs, aid, SYM, AlertCondition.price_above, D(101)))
    evs = [AlertEvaluator(maker, redis, qs) for _ in range(4)]
    for ev in evs:
        await ev.sweep()
    q = _quote(last=D(102))
    results = await asyncio.gather(*(ev.on_tick(SYM, q) for ev in evs))
    assert sum(results) == 1
    assert len(await _price_alerts(maker)) == 1


async def test_ticks_are_coalesced_for_active_symbols_only(maker, redis, qs):
    aid = await _agent(maker)
    await _commit(maker, redis, lambda s: watch.create_alert(
        s, qs, aid, SYM, AlertCondition.price_above, D(101)))
    ev = AlertEvaluator(maker, redis, qs)
    await ev.sweep()
    ev.submit_tick("MSFT", _quote(last=D(500), symbol="MSFT"))  # no alert: ignored
    ev.submit_tick(SYM, _quote(last=D(102)))
    ev.submit_tick(SYM, _quote(last=D(100)))  # latest wins
    await ev.drain()
    assert await _price_alerts(maker) == []
    ev.submit_tick(SYM, _quote(last=D(103)))
    ev._last_eval.clear()  # skip the per-symbol throttle
    await ev.drain()
    assert len(await _price_alerts(maker)) == 1


async def test_get_triggered_alerts_counts_rows(maker, redis, qs):
    """Sanity: the trigger writes exactly one notification row per alert."""
    aid = await _agent(maker)
    for v in (D(101), D(102)):
        await _commit(maker, redis, lambda s, v=v: watch.create_alert(
            s, qs, aid, SYM, AlertCondition.price_above, v))
    ev = AlertEvaluator(maker, redis, qs)
    await ev.sweep()
    assert await ev.on_tick(SYM, _quote(last=D(103))) == 2
    async with maker() as s:
        assert await s.scalar(select(func.count()).select_from(Notification)) == 2
