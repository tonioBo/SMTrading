"""Mode B schema and rule management (`autorules.tables`, `autorules.rules`): one active
rule per (portfolio, symbol), versions on every change, the activation checks, the
`auto_rules` price-stream source, the switch that pauses every rule, the single-query
read of the active rules and the hook that hides their symbols from mode A. Names are
made up."""
import asyncio
import uuid
from datetime import UTC, datetime
from decimal import Decimal as D

import pytest
import redis.asyncio as aioredis
from sqlalchemy import event, select, text, update
from sqlalchemy.exc import IntegrityError
from test_admin_intraday import _portfolio as _admin_portfolio
from test_admin_intraday import admin  # noqa: F401  (fixture)

from smtrading.activity.models import Decision
from smtrading.agents import service as agents
from smtrading.autorules import rules
from smtrading.autorules.models import PORTFOLIO_BRAKE_DEFAULTS, RULE_BRAKE_DEFAULTS
from smtrading.autorules.tables import (
    ACTIVE,
    BRAKED,
    DRAFT,
    PAUSED,
    STATUS_IT,
    AutoRule,
    AutoRuleVersion,
    AutoTrade,
)
from smtrading.common.errors import ErrorCode, SMTError
from smtrading.engine.models import Order, OrderSide, OrderType
from smtrading.engine.quotesource import FakeQuoteSource
from smtrading.engine.service import PlaceOrderRequest, place_order
from smtrading.events import discard_post_commit, dispatch_post_commit
from smtrading.instruments.models import AssetClass, Instrument
from smtrading.intraday.job import auto_rule_owned_pairs
from smtrading.marketdata.streaming import SOURCES
from smtrading.mcp.dto import order_dto
from smtrading.portfolios.models import Portfolio
from smtrading.portfolios.service import create_portfolio
from smtrading.positions.models import Position
from smtrading.seed import run_seed

OPEN = datetime(2026, 11, 25, 15, 0, tzinfo=UTC)  # a Wednesday, NYSE open
BY = "admin@example.com"
PARAMS = {"lookback_min": 20}


@pytest.fixture
async def seeded(db_session):
    await run_seed(db_session)
    await db_session.flush()


@pytest.fixture
def qs():
    q = FakeQuoteSource()
    q.set_fx("EUR", "USD", D("1.25"))
    q.set_quote("AAPL", D("190.00"), D("190.10"), D("190.05"))
    return q


async def _pf(session, *, auto=True, intraday=False) -> Portfolio:
    p = await create_portfolio(session, name=f"R-{uuid.uuid4().hex[:8]}",
                               initial_cash=D(100000), leverage=D(1))
    p.auto_rules_enabled = auto
    p.intraday_enabled = intraday
    await session.flush()
    return p


async def _rule(session, p, symbol="AAPL", **kw) -> AutoRule:
    kw.setdefault("qty", D(1))
    return await rules.create_rule(session, portfolio_id=p.id, symbol=symbol,
                                   model="breakout", params=PARAMS, by=BY, **kw)


def _refused(exc, text):
    assert exc.value.code == ErrorCode.INVALID_ORDER, exc.value
    assert text in exc.value.message, exc.value.message


async def test_constants():
    assert (DRAFT, ACTIVE, PAUSED, BRAKED) == ("draft", "active", "paused", "braked")
    assert STATUS_IT == {"draft": "bozza", "active": "attiva", "paused": "in pausa",
                         "braked": "fermata dai freni"}
    assert "auto_rules" in SOURCES


async def test_one_active_rule_per_symbol(db_session, seeded):
    p = await _pf(db_session)
    first, second = await _rule(db_session, p), await _rule(db_session, p)
    await rules.activate(db_session, first.id, by=BY, now=OPEN)
    with pytest.raises(SMTError) as exc:
        await rules.activate(db_session, second.id, by=BY, now=OPEN)
    _refused(exc, "c'è già una regola attiva")
    await db_session.flush()
    with pytest.raises(IntegrityError, match="uq_auto_rules_active_symbol"):
        async with db_session.begin_nested():
            await db_session.execute(
                update(AutoRule).where(AutoRule.id == second.id).values(status=ACTIVE))
    await db_session.refresh(second)
    await rules.pause(db_session, first.id, by=BY)
    await rules.activate(db_session, second.id, by=BY, now=OPEN)
    await db_session.refresh(first)
    await db_session.refresh(second)
    assert (first.status, second.status) == (PAUSED, ACTIVE)  # paused and active coexist


async def test_trigger_key_unique(db_session, seeded):
    p = await _pf(db_session)
    r = await _rule(db_session, p)

    def trade():
        return AutoTrade(rule_id=r.id, rule_version=1, portfolio_id=p.id, symbol="AAPL",
                         trigger_key="2026-11-25T15:00:00Z", trigger_at=OPEN,
                         trigger_price=D(190), trigger_values={}, expected_price=D(190),
                         status="sent")

    db_session.add(trade())
    await db_session.flush()
    with pytest.raises(IntegrityError, match="uq_auto_trades_rule_trigger"):
        async with db_session.begin_nested():
            db_session.add(trade())
            await db_session.flush()


@pytest.mark.parametrize("sql", [
    "update auto_rules set model = 'bogus'",
    "update auto_rules set status = 'bogus'",
    "update auto_rule_versions set qty = null, notional = null",
    "update auto_rule_versions set notional = 5",
    "update auto_rule_versions set qty = 0",
])
async def test_rule_checks(db_session, seeded, sql):
    p = await _pf(db_session)
    await _rule(db_session, p)
    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            await db_session.execute(text(sql))


async def test_versions_on_update(db_session, seeded):
    p = await _pf(db_session)
    r = await _rule(db_session, p, notional=None, qty=D(2))
    assert r.version == 1 and r.status == DRAFT
    await rules.update_rule(db_session, r.id, params={"lookback_min": 30}, by=BY)
    await db_session.refresh(r)
    assert r.version == 2
    versions = {v.version: v for v in await db_session.scalars(
        select(AutoRuleVersion).where(AutoRuleVersion.rule_id == r.id))}
    assert versions[1].params["lookback_min"] == 20
    assert versions[2].params["lookback_min"] == 30
    assert versions[2].qty == D(2) and versions[2].created_by == BY
    assert versions[1].brakes["max_trades_hour"] == RULE_BRAKE_DEFAULTS.max_trades_hour
    await rules.activate(db_session, r.id, by=BY, now=OPEN)
    with pytest.raises(SMTError) as exc:
        await rules.update_rule(db_session, r.id, qty=D(3), by=BY)
    _refused(exc, "metti in pausa la regola prima di modificarla")


async def test_create_validates(db_session, seeded):
    p = await _pf(db_session)
    fx = await db_session.scalar(select(Instrument.symbol).where(
        Instrument.asset_class == AssetClass.forex, Instrument.active.is_(True)).limit(1))
    with pytest.raises(SMTError) as exc:
        await _rule(db_session, p, symbol=fx)
    _refused(exc, "solo azioni USA e cripto")
    with pytest.raises(SMTError):
        await _rule(db_session, p, qty=D(1), notional=D(100))
    with pytest.raises(SMTError):
        await _rule(db_session, p, qty=D(0))
    with pytest.raises(SMTError):
        await rules.create_rule(db_session, portfolio_id=p.id, symbol="AAPL",
                                model="breakout", params={"lookback_min": 1}, qty=D(1),
                                by=BY)
    with pytest.raises(SMTError):
        await _rule(db_session, p, brakes={"bogus": 1})
    crypto = await _rule(db_session, p, symbol="BTC-USD", qty=None, notional=D(100))
    assert crypto.status == DRAFT


async def _strategy(session, symbols, portfolio_ids, timeframe="15m"):
    ceo, _ = await agents.create_agent(session, f"Agente {uuid.uuid4().hex[:5]}")
    session.add(Decision(
        agent_id=ceo.id, kind="strategy", summary="Strategia di prova",
        details={"ref": f"S-{uuid.uuid4().hex[:5]}", "status": "approvata",
                 "strategy": "rsi_reversion", "timeframe": timeframe,
                 "params": {"period": 2, "buy_below": 10, "sell_above": 70},
                 "portfolios": [str(x) for x in portfolio_ids]},
        symbols=list(symbols), portfolio_id=None))
    await session.flush()


async def test_activate_refuses(db_session, seeded, qs):
    off = await _pf(db_session, auto=False)
    with pytest.raises(SMTError) as exc:
        await rules.activate(db_session, (await _rule(db_session, off)).id, by=BY, now=OPEN)
    _refused(exc, "regole automatiche sono spente")

    held = await _pf(db_session)
    db_session.add(Position(portfolio_id=held.id, symbol="AAPL", qty=D(3),
                            avg_price=D(190)))
    await db_session.flush()
    with pytest.raises(SMTError) as exc:
        await rules.activate(db_session, (await _rule(db_session, held)).id, by=BY, now=OPEN)
    _refused(exc, "posizione aperta")

    resting = await _pf(db_session)
    await place_order(db_session, qs, PlaceOrderRequest(
        portfolio_id=resting.id, symbol="AAPL", side=OrderSide.buy, type=OrderType.limit,
        qty=D(1), limit_price=D(150)), now=OPEN)
    discard_post_commit(db_session)
    with pytest.raises(SMTError) as exc:
        await rules.activate(db_session, (await _rule(db_session, resting)).id, by=BY,
                             now=OPEN)
    _refused(exc, "ordine aperto")

    day = await _pf(db_session, intraday=True)
    await _strategy(db_session, ["AAPL"], [day.id])
    with pytest.raises(SMTError) as exc:
        await rules.activate(db_session, (await _rule(db_session, day)).id, by=BY, now=OPEN)
    _refused(exc, "il simbolo è già seguito dal lavoro in giornata con l'agente su questo "
                  "portafoglio")

    # a daily strategy or mode A off: not a conflict
    daily = await _pf(db_session, intraday=True)
    await _strategy(db_session, ["AAPL"], [daily.id], timeframe="1d")
    r = await rules.activate(db_session, (await _rule(db_session, daily)).id, by=BY, now=OPEN)
    assert r.status == ACTIVE


async def test_resume_and_brake(db_session, seeded):
    p = await _pf(db_session)
    r = await _rule(db_session, p)
    await rules.activate(db_session, r.id, by=BY, now=OPEN)
    await rules.brake(db_session, r.id, "perdite consecutive")
    await db_session.refresh(r)
    assert (r.status, r.status_reason) == (BRAKED, "perdite consecutive")
    p.auto_rules_enabled = False
    await db_session.flush()
    with pytest.raises(SMTError) as exc:
        await rules.resume(db_session, r.id, by=BY, now=OPEN)
    _refused(exc, "regole automatiche sono spente")
    p.auto_rules_enabled = True
    await db_session.flush()
    await rules.resume(db_session, r.id, by=BY, now=OPEN)
    await db_session.refresh(r)
    assert (r.status, r.status_reason) == (ACTIVE, None)


@pytest.fixture
async def redis(redis_url):
    client = aioredis.from_url(redis_url, decode_responses=True)
    await client.delete("md:tracked", "md:tracked:auto_rules")
    yield client
    await client.delete("md:tracked", "md:tracked:auto_rules")
    await client.aclose()


async def _commit(session, redis):
    await session.commit()
    await dispatch_post_commit(session, redis)


async def test_activate_tracks_symbol_and_pause_untracks(db_session, seeded, redis):
    db_session.sync_session.expire_on_commit = False  # the commits keep the objects
    p, other = await _pf(db_session), await _pf(db_session)
    r = await _rule(db_session, p)
    await _commit(db_session, redis)
    await rules.activate(db_session, r.id, by=BY, now=OPEN)
    await _commit(db_session, redis)
    assert await redis.smembers("md:tracked:auto_rules") == {"AAPL"}
    await rules.pause(db_session, r.id, by=BY)
    await _commit(db_session, redis)
    assert await redis.smembers("md:tracked:auto_rules") == set()

    second = await _rule(db_session, other)
    await rules.resume(db_session, r.id, by=BY, now=OPEN)
    await rules.activate(db_session, second.id, by=BY, now=OPEN)
    await _commit(db_session, redis)
    await rules.pause(db_session, r.id, by=BY)
    await _commit(db_session, redis)
    assert await redis.smembers("md:tracked:auto_rules") == {"AAPL"}  # the other one


async def test_pause_all(db_session, seeded):
    p = await _pf(db_session)
    a, b = await _rule(db_session, p), await _rule(db_session, p, symbol="MSFT")
    draft = await _rule(db_session, p, symbol="NVDA")
    await rules.activate(db_session, a.id, by=BY, now=OPEN)
    await rules.activate(db_session, b.id, by=BY, now=OPEN)
    await rules.brake(db_session, b.id, "troppe operazioni")
    paused = await rules.pause_all(db_session, p.id, by=BY, reason="interruttore spento")
    assert {x.id for x in paused} == {a.id, b.id}
    for x in (a, b, draft):
        await db_session.refresh(x)
    assert (a.status, b.status, draft.status) == (PAUSED, PAUSED, DRAFT)
    assert a.status_reason == b.status_reason == "interruttore spento"


async def test_disable_switch_pauses_rules(admin):  # noqa: F811
    pid = await _admin_portfolio(admin, auto_rules_enabled=True)
    async with admin.sm() as s:
        p = await s.get(Portfolio, uuid.UUID(pid))
        ids = []
        for sym in ("AAPL", "MSFT"):
            r = await _rule(s, p, symbol=sym)
            await rules.activate(s, r.id, by=BY, now=OPEN)
            ids.append(r.id)
        discard_post_commit(s)
        await s.commit()
    r = await admin.client.patch(f"/api/portfolios/{pid}", json={"auto_rules_enabled": False})
    assert r.status_code == 200, r.text
    async with admin.sm() as s:
        got = list(await s.scalars(select(AutoRule).where(AutoRule.id.in_(ids))))
    assert {(x.status, x.status_reason) for x in got} == {(PAUSED, "interruttore spento")}
    client = aioredis.from_url(admin.redis_url, decode_responses=True)
    try:
        assert not await client.sismember("md:tracked:auto_rules", "AAPL")
    finally:
        await client.aclose()


async def test_active_rules_single_query(db_session, seeded):
    on, off = await _pf(db_session), await _pf(db_session)
    r = await _rule(db_session, on, brakes={"max_trades_hour": 5})
    other = await _rule(db_session, off, symbol="MSFT")
    await rules.activate(db_session, r.id, by=BY, now=OPEN)
    await rules.activate(db_session, other.id, by=BY, now=OPEN)
    off.auto_rules_enabled = False
    on.auto_brakes = {"max_trades_day": 50}
    await db_session.flush()
    statements: list[str] = []

    def count(conn, cursor, statement, *args):
        statements.append(statement)

    engine = db_session.bind.sync_engine
    event.listen(engine, "before_cursor_execute", count)
    try:
        got = await rules.active_rules(db_session)
    finally:
        event.remove(engine, "before_cursor_execute", count)
    assert len(statements) == 1
    assert [a.rule_id for a in got] == [r.id]
    a = got[0]
    assert (a.version, a.portfolio_id, a.symbol, a.model) == (1, on.id, "AAPL", "breakout")
    assert a.params["lookback_min"] == 20 and a.params["stop_pct"] == D("0.3")
    assert (a.qty, a.notional) == (D(1), None)
    assert a.brakes.max_trades_hour == 5 and a.brakes.max_open_positions == 1
    assert a.portfolio_brakes.max_trades_day == 50
    assert a.portfolio_brakes.max_trades_hour == PORTFOLIO_BRAKE_DEFAULTS.max_trades_hour
    assert (a.market, a.asset_class, a.tick_size) == ("NASDAQ", "us_equity", D("0.0001"))
    assert (a.day_close_minutes, a.base_currency) == (10, "EUR")


async def test_auto_rule_owned_pairs(db_session, seeded):
    p = await _pf(db_session)
    active, draft = await _rule(db_session, p), await _rule(db_session, p, symbol="MSFT")
    assert await auto_rule_owned_pairs(db_session) == frozenset()
    await rules.activate(db_session, active.id, by=BY, now=OPEN)
    await db_session.flush()
    assert await auto_rule_owned_pairs(db_session) == frozenset({(p.id, "AAPL")})
    assert draft.status == DRAFT


async def test_order_auto_rule_id_default_null(db_session, seeded, qs):
    p = await _pf(db_session, auto=False)
    order = await place_order(db_session, qs, PlaceOrderRequest(
        portfolio_id=p.id, symbol="AAPL", side=OrderSide.buy, type=OrderType.market,
        qty=D(1)), now=OPEN)
    await db_session.refresh(order)
    assert order.auto_rule_id is None
    r = await _rule(db_session, await _pf(db_session))
    await db_session.execute(update(Order).where(Order.id == order.id)
                             .values(auto_rule_id=r.id))
    await db_session.refresh(order)
    assert order.auto_rule_id == r.id
    assert "auto_rule_id" not in order_dto(order)


async def test_portfolio_auto_brakes_default(db_session, seeded):
    p = await _pf(db_session)
    await db_session.refresh(p)
    assert p.auto_brakes == {}


async def test_downgrade_0047_and_back(fresh_db):
    from test_migrations import _alembic

    await asyncio.to_thread(_alembic, fresh_db, "downgrade", "0046")
    await asyncio.to_thread(_alembic, fresh_db, "upgrade", "head")
