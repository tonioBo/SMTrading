"""Activity feed (written in the transaction of the fact), decisions log, feed queries."""

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import ClassVar

import pytest
from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import async_sessionmaker

from smtrading.activity.mapping import ActivityDraft
from smtrading.activity.models import Activity, ActivityLevel, Decision
from smtrading.activity.service import (
    ACTIVITY_KEYS,
    list_decisions,
    list_feed,
    log_decision,
    record_activity,
)
from smtrading.agents.service import assign_portfolio, create_agent
from smtrading.brokers import registry
from smtrading.brokers.base import BrokerAdapter
from smtrading.brokers.service import create_link, link_portfolio
from smtrading.brokers.sync import reconcile
from smtrading.common.db import make_engine
from smtrading.common.errors import ErrorCode, SMTError
from smtrading.engine.execution import execute_fill
from smtrading.engine.models import OrderSide, OrderStatus, OrderType
from smtrading.engine.quotesource import FakeQuoteSource
from smtrading.engine.service import PlaceOrderRequest, place_order, set_kill_switch_and_cancel
from smtrading.events import discard_post_commit, notify, queue_event
from smtrading.portfolios.service import create_portfolio
from smtrading.seed import run_seed

D = Decimal
OPEN = datetime(2026, 11, 10, 15, 0, tzinfo=UTC)  # Tuesday: NASDAQ open


@pytest.fixture
def qs():
    q = FakeQuoteSource()
    q.set_fx("EUR", "USD", D("1.25"))
    q.set_quote("AAPL", D("199.90"), D("200.00"), D("199.95"))
    return q


@pytest.fixture
async def seeded(db_session):
    await run_seed(db_session)


async def _portfolio(session, cash=0):
    return await create_portfolio(
        session, name=f"A-{uuid.uuid4().hex[:8]}", initial_cash=D(cash),
    )


async def _rows(session, **where) -> list[Activity]:
    stmt = select(Activity).order_by(Activity.id)
    for k, v in where.items():
        stmt = stmt.where(getattr(Activity, k) == v)
    await session.flush()
    return list(await session.scalars(stmt))


async def _settle(session) -> None:
    """Move every activity row out of the feed's settle window."""
    await session.flush()
    await session.execute(text("update activity set ts = now() - interval '4 seconds'"))


# -- recording ---------------------------------------------------------------------


async def test_place_and_fill_records_accepted_and_filled(db_session, seeded, qs):
    p = await _portfolio(db_session, cash=100000)
    order = await place_order(db_session, qs, PlaceOrderRequest(
        portfolio_id=p.id, symbol="AAPL", side=OrderSide.buy, type=OrderType.limit,
        qty=D(10), limit_price=D("195.00"),
    ), now=OPEN)
    assert order.status != OrderStatus.filled
    qs.set_quote("AAPL", D("194.00"), D("194.50"), None)
    quote = (await qs.get_quotes(["AAPL"]))["AAPL"]
    await execute_fill(db_session, qs, order.id, quote=quote)

    rows = [r for r in await _rows(db_session, portfolio_id=p.id) if r.type.startswith("order.")]
    assert [(r.type, r.level, r.telegram) for r in rows] == [
        ("order.accepted", "info", None),
        ("order.filled", "executed", "pending"),
    ]
    assert rows[1].data["order_id"] == str(order.id)
    assert rows[1].summary.startswith("Ordine eseguito: buy 10 AAPL a ")
    cash = await _rows(db_session, portfolio_id=p.id, type="portfolio.cash")
    assert [r.summary for r in cash] == ["Deposito di 100000 EUR"]


async def test_rollback_drops_activity(db_session):
    pid = (await _portfolio(db_session, cash=100)).id
    await db_session.commit()  # the savepoint: the portfolio stays
    assert len(await _rows(db_session, portfolio_id=pid)) == 1  # the initial deposit
    queue_event(db_session, "portfolio.cash", {
        "portfolio_id": pid, "type": "deposit", "amount": D(5), "currency": "EUR",
    })
    await db_session.rollback()
    discard_post_commit(db_session)
    assert len(await _rows(db_session, portfolio_id=pid)) == 1


async def test_reject_via_notify_and_event_deduped(db_session):
    p = await _portfolio(db_session)
    oid = uuid.uuid4()
    await notify(db_session, portfolio_id=p.id, kind="order.rejected", payload={
        "order_id": oid, "symbol": "AAPL", "code": "X", "message": "no",
    })
    queue_event(db_session, "order.rejected", {
        "order_id": oid, "portfolio_id": p.id, "agent_id": None, "symbol": "AAPL",
    })
    rows = await _rows(db_session, type="order.rejected", portfolio_id=p.id)
    assert len(rows) == 1
    assert (rows[0].level, rows[0].telegram) == ("warning", "pending")
    assert rows[0].summary == "Ordine rifiutato: AAPL: no"
    # another order is another fact
    queue_event(db_session, "order.rejected", {
        "order_id": uuid.uuid4(), "portfolio_id": p.id, "symbol": "AAPL",
    })
    assert len(await _rows(db_session, type="order.rejected", portfolio_id=p.id)) == 2


async def test_invalid_agent_id_is_dropped(db_session):
    p = await _portfolio(db_session)
    agent, _ = await create_agent(db_session, f"ag-{uuid.uuid4().hex[:6]}")
    await notify(db_session, portfolio_id=p.id, kind="price_alert", agent_id=str(agent.id),
                 payload={"symbol": "AAPL"})
    queue_event(db_session, "order.canceled", {
        "order_id": uuid.uuid4(), "portfolio_id": p.id, "agent_id": "not-a-uuid",
        "symbol": "AAPL",
    })
    alert = (await _rows(db_session, type="price_alert", portfolio_id=p.id))[0]
    canceled = (await _rows(db_session, type="order.canceled", portfolio_id=p.id))[0]
    assert alert.agent_id == agent.id and canceled.agent_id is None


async def test_kill_switch_records_critical_pending(db_session, seeded):
    await set_kill_switch_and_cancel(db_session, True, cancel_open=True)
    await set_kill_switch_and_cancel(db_session, False, cancel_open=False)
    rows = (await _rows(db_session, type="kill_switch"))[-2:]
    assert [(r.level, r.telegram, r.portfolio_id) for r in rows] == [
        ("critical", "pending", None), ("warning", "pending", None),
    ]
    assert rows[0].summary.startswith("Kill switch attivato (")


# -- reconciliation ------------------------------------------------------------------


@registry.register
class _ReconcileAdapter(BrokerAdapter):
    kind: ClassVar[str] = "fake_activity_test"
    positions: ClassVar[dict] = {}

    async def get_positions(self):
        return dict(self.positions)


@pytest.fixture
async def maker(fresh_db, redis_url, monkeypatch):
    monkeypatch.setenv("SMT_DATABASE_URL", fresh_db)
    monkeypatch.setenv("SMT_REDIS_URL", redis_url)
    monkeypatch.setenv("SMT_MASTER_KEY", "test-master-key")
    monkeypatch.setenv("SMT_ADMIN_EMAIL", "admin@example.com")
    monkeypatch.setenv("SMT_ADMIN_PASSWORD", "test-password")
    engine = make_engine(fresh_db)
    sm = async_sessionmaker(engine, expire_on_commit=False)
    async with sm() as s:
        await run_seed(s)
        await s.commit()
    _ReconcileAdapter.positions = {}
    yield sm
    _ReconcileAdapter.positions = {}
    await engine.dispose()


async def _linked(maker, n=1):
    async with maker() as s:
        pids = [(await _portfolio(s)).id for _ in range(n)]
        link = await create_link(s, f"L-{uuid.uuid4().hex[:6]}", "fake_activity_test", {})
        for pid in pids:
            await link_portfolio(s, pid, link.id)
        await s.commit()
    return pids, link.id


async def _diff_rows(maker) -> list[Activity]:
    async with maker() as s:
        return await _rows(s, type="broker.reconciliation_diff")


async def test_reconcile_diff_records_activity(maker):
    (pid,), link_id = await _linked(maker)
    assert [r.ok for r in await reconcile(maker, link_id, None)] == [True]
    assert await _diff_rows(maker) == []
    _ReconcileAdapter.positions = {"AAPL": D(5)}
    assert [r.ok for r in await reconcile(maker, link_id, None)] == [False]
    rows = await _diff_rows(maker)
    assert len(rows) == 1
    r = rows[0]
    assert (r.level, r.telegram, r.portfolio_id, r.agent_id) == (
        "warning", "pending", pid, None)
    assert r.summary == "Riconciliazione broker con 1 differenza"
    assert r.data == {"broker_link_id": str(link_id),
                      "diffs": [{"symbol": "AAPL", "ours": "0", "broker": "5"}]}


async def test_reconcile_repeat_not_direct_change_direct(maker):
    (pid,), link_id = await _linked(maker)
    _ReconcileAdapter.positions = {"AAPL": D(5)}
    await reconcile(maker, link_id, None)
    await reconcile(maker, link_id, None)  # same differences: recorded, not sent
    _ReconcileAdapter.positions = {"AAPL": D(5), "MSFT": D(1)}
    await reconcile(maker, link_id, None)  # changed: sent
    _ReconcileAdapter.positions = {}
    await reconcile(maker, link_id, None)  # fixed: nothing to record
    _ReconcileAdapter.positions = {"AAPL": D(5), "MSFT": D(1)}
    await reconcile(maker, link_id, None)  # back after an ok one: sent again
    rows = await _diff_rows(maker)
    assert [(r.telegram, r.summary) for r in rows] == [
        ("pending", "Riconciliazione broker con 1 differenza"),
        (None, "Riconciliazione broker con 1 differenza"),
        ("pending", "Riconciliazione broker con 2 differenze"),
        ("pending", "Riconciliazione broker con 2 differenze"),
    ]
    assert {r.portfolio_id for r in rows} == {pid}


async def test_reconcile_compares_each_portfolio_with_its_previous(maker):
    pids, link_id = await _linked(maker, 2)
    _ReconcileAdapter.positions = {"AAPL": D(5)}
    await reconcile(maker, link_id, None)
    await reconcile(maker, link_id, None)
    rows = await _diff_rows(maker)
    assert sorted((str(r.portfolio_id), r.telegram or "") for r in rows) == sorted(
        [(str(p), "pending") for p in pids] + [(str(p), "") for p in pids])


async def test_activity_keys_cleared_on_commit_and_rollback(maker):
    async with maker() as s:
        pid = (await _portfolio(s)).id
        await notify(s, portfolio_id=pid, kind="order.rejected",
                     payload={"order_id": uuid.uuid4(), "symbol": "AAPL"})
        assert ACTIVITY_KEYS in s.info
        await s.commit()
        assert ACTIVITY_KEYS not in s.info
        await notify(s, portfolio_id=pid, kind="order.rejected",
                     payload={"order_id": uuid.uuid4(), "symbol": "AAPL"})
        await s.rollback()
        assert ACTIVITY_KEYS not in s.info


async def test_unknown_agent_id_never_aborts(maker, qs):
    ghost = uuid.uuid4()  # UUID-shaped, no such agent
    async with maker() as s:
        p = await _portfolio(s, cash=100000)
        order = await place_order(s, qs, PlaceOrderRequest(
            portfolio_id=p.id, symbol="AAPL", side=OrderSide.buy, type=OrderType.market,
            qty=D(1), agent_id=str(ghost),
        ), now=OPEN)
        assert order.status == OrderStatus.filled
        await notify(s, portfolio_id=None, kind="price_alert", agent_id=str(ghost),
                     payload={"symbol": "AAPL"})
        await s.commit()
    async with maker() as s:
        rows = await _rows(s, agent_id=ghost)
    assert {r.type for r in rows} == {"order.filled", "order.accepted", "price_alert"}


async def test_record_activity_normalises_everything(db_session):
    p = await _portfolio(db_session)
    record_activity(db_session, ActivityDraft(
        "t" * 100, "warning", True, "s" * 900,
        {"amount": D("1.50"), "at": datetime(2026, 1, 1, tzinfo=UTC), "ids": {1},
         "order_id": uuid.uuid4()},
    ), portfolio_id=str(p.id), agent_id=12345)
    record_activity(db_session, ActivityDraft(
        "x", ActivityLevel.info, False, None, "not a dict",  # type: ignore[arg-type]
    ), portfolio_id=p.id, agent_id=None)
    rows = await _rows(db_session, portfolio_id=p.id)
    a, b = rows
    assert (len(a.type), a.level, len(a.summary), a.agent_id) == (64, "warning", 500, None)
    assert a.data["amount"] == "1.50" and a.data["ids"] == [1]
    assert (b.summary, b.data, b.telegram) == ("", {"value": "not a dict"}, None)


# -- decisions -----------------------------------------------------------------------


async def _agent(session, name="dec"):
    agent, _ = await create_agent(session, f"{name}-{uuid.uuid4().hex[:6]}")
    return agent


async def test_log_decision_requires_read(db_session):
    p = await _portfolio(db_session)
    agent = await _agent(db_session)
    with pytest.raises(SMTError) as e:
        await log_decision(db_session, agent.id, kind="proposal", summary="x",
                           portfolio_id=p.id)
    assert e.value.code == ErrorCode.FORBIDDEN
    await assign_portfolio(db_session, agent.id, p.id, "read")
    d = await log_decision(db_session, agent.id, kind="proposal", summary="Compra AAPL",
                           portfolio_id=p.id)
    assert d.portfolio_id == p.id


@pytest.mark.parametrize(("kw", "field"), [
    ({"kind": "foo"}, "kind"),
    ({"summary": "   "}, "summary"),
    ({"summary": "x" * 501}, "summary"),
    ({"symbols": [f"S{i}" for i in range(51)]}, "symbols"),
    ({"symbols": ["X" * 33]}, "symbols"),
    ({"symbols": "AAPL"}, "symbols"),
    ({"details": {"blob": "x" * 9000}}, "details"),
    ({"details": ["not", "a", "dict"]}, "details"),
])
async def test_log_decision_validation(db_session, kw, field):
    agent = await _agent(db_session)
    args = {"kind": "proposal", "summary": "ok"} | kw
    with pytest.raises(SMTError) as e:
        await log_decision(db_session, agent.id, **args)
    assert e.value.code == ErrorCode.INVALID_ORDER
    assert e.value.details["field"] == field


async def test_log_decision_writes_activity(db_session):
    agent = await _agent(db_session)
    d = await log_decision(
        db_session, agent.id, kind="allocation", summary="  Più peso su BTC  ",
        symbols=["btc-usd", "BTC-USD", " eth-usd "], details={"pct": D("12.5")},
    )
    assert d.summary == "Più peso su BTC"
    assert d.symbols == ["BTC-USD", "ETH-USD"]
    assert d.details == {"pct": "12.5"}
    rows = await _rows(db_session, agent_id=agent.id)
    assert len(rows) == 1
    a = rows[0]
    assert (a.type, a.level, a.telegram, a.portfolio_id) == (
        "decision.allocation", "info", None, None)
    assert a.data == {"decision_id": d.id, "symbols": ["BTC-USD", "ETH-USD"],
                      "details": {"pct": "12.5"}}


async def test_list_decisions_filters_and_visibility(db_session):
    mine, hidden = await _portfolio(db_session), await _portfolio(db_session)
    me, other = await _agent(db_session, "me"), await _agent(db_session, "other")
    await assign_portfolio(db_session, me.id, mine.id, "read")
    await assign_portfolio(db_session, other.id, hidden.id, "trade")
    d1 = await log_decision(db_session, me.id, kind="proposal", summary="a",
                            portfolio_id=mine.id)
    d2 = await log_decision(db_session, other.id, kind="veto", summary="b")  # global
    d3 = await log_decision(db_session, other.id, kind="veto", summary="c",
                            portfolio_id=hidden.id)
    seen = {d.id for d in await list_decisions(db_session, viewer_agent_id=me.id, limit=500)}
    assert {d1.id, d2.id} <= seen and d3.id not in seen
    admin = await list_decisions(db_session, viewer_agent_id=None, agent_id=other.id)
    assert [d.id for d in admin] == [d3.id, d2.id]
    assert [d.id for d in await list_decisions(
        db_session, viewer_agent_id=None, agent_id=other.id, before_id=d3.id)] == [d2.id]
    assert [d.id for d in await list_decisions(
        db_session, viewer_agent_id=None, portfolio_id=mine.id, kind="proposal")] == [d1.id]
    assert await list_decisions(db_session, viewer_agent_id=None, agent_id=me.id,
                                kind="veto") == []
    assert (await db_session.get(Decision, d1.id)).kind == "proposal"


# -- feed --------------------------------------------------------------------------


async def _feed_setup(session):
    p = await _portfolio(session)
    agent = await _agent(session, "feed")
    await assign_portfolio(session, agent.id, p.id, "read")
    return p, agent


def _deposit(session, p, amount):
    queue_event(session, "portfolio.cash", {
        "portfolio_id": p.id, "type": "deposit", "amount": D(amount), "currency": "EUR",
    })


async def test_feed_cursor_contract(db_session):
    p, agent = await _feed_setup(db_session)
    for amount in (1, 2, 3):
        _deposit(db_session, p, amount)
    await _settle(db_session)
    mine = [r.id for r in await _rows(db_session, portfolio_id=p.id)]

    items, cursor = await list_feed(db_session, agent.id, after_id=None, since=None, limit=2)
    assert [i["id"] for i in items] == mine[::-1][:2]  # newest first
    assert cursor == mine[-1]
    assert items[0]["summary"] == "Deposito di 3 EUR"
    assert items[0]["portfolio"] == p.name and items[0]["portfolio_id"] == p.id

    items, cursor = await list_feed(db_session, agent.id, after_id=mine[0], since=None,
                                    limit=10)
    assert [i["id"] for i in items] == mine[1:]  # ascending
    assert cursor == mine[-1]
    items, again = await list_feed(db_session, agent.id, after_id=cursor, since=None, limit=10)
    assert items == [] and again == cursor  # unchanged when nothing is new

    with pytest.raises(SMTError) as e:
        await list_feed(db_session, agent.id, after_id=mine[-1] + 10_000, since=None, limit=10)
    assert e.value.code == ErrorCode.INVALID_ORDER and e.value.details["field"] == "after_id"


async def test_feed_invisible_after_id_is_unknown(db_session):
    _p, agent = await _feed_setup(db_session)
    hidden = await _portfolio(db_session)
    _deposit(db_session, hidden, 1)
    await _settle(db_session)
    hidden_id = (await _rows(db_session, portfolio_id=hidden.id))[-1].id
    with pytest.raises(SMTError) as e:
        await list_feed(db_session, agent.id, after_id=hidden_id, since=None, limit=10)
    assert e.value.code == ErrorCode.INVALID_ORDER and e.value.details["field"] == "after_id"
    assert e.value.message == "unknown after_id"


async def test_feed_first_cursor_is_settled(db_session):
    p, agent = await _feed_setup(db_session)
    _deposit(db_session, p, 1)
    await _settle(db_session)
    settled = (await _rows(db_session, portfolio_id=p.id))[-1].id
    _deposit(db_session, p, 2)
    fresh = (await _rows(db_session, portfolio_id=p.id))[-1]
    await db_session.execute(
        update(Activity).where(Activity.id == fresh.id).values(ts=datetime.now(UTC))
    )
    items, cursor = await list_feed(db_session, agent.id, after_id=None, since=None, limit=10)
    assert [i["id"] for i in items][:2] == [fresh.id, settled]  # fresh rows still shown
    assert cursor == settled  # but the cursor stays behind them
    items, cursor = await list_feed(db_session, agent.id, after_id=cursor, since=None, limit=10)
    assert items == [] and cursor == settled
    await _settle(db_session)
    items, cursor = await list_feed(db_session, agent.id, after_id=cursor, since=None, limit=10)
    assert [i["id"] for i in items] == [fresh.id] and cursor == fresh.id


async def test_feed_settle_window(db_session):
    p, agent = await _feed_setup(db_session)
    _deposit(db_session, p, 1)
    await _settle(db_session)
    cursor = (await _rows(db_session, portfolio_id=p.id))[-1].id
    _deposit(db_session, p, 2)
    fresh = (await _rows(db_session, portfolio_id=p.id))[-1]
    await db_session.execute(
        update(Activity).where(Activity.id == fresh.id).values(ts=datetime.now(UTC))
    )
    items, nxt = await list_feed(db_session, agent.id, after_id=cursor, since=None, limit=10)
    assert items == [] and nxt == cursor
    await _settle(db_session)
    items, nxt = await list_feed(db_session, agent.id, after_id=cursor, since=None, limit=10)
    assert [i["id"] for i in items] == [fresh.id] and nxt == fresh.id


async def test_feed_since(db_session):
    p, agent = await _feed_setup(db_session)
    _deposit(db_session, p, 1)
    await _settle(db_session)
    row = (await _rows(db_session, portfolio_id=p.id))[-1]
    items, _ = await list_feed(db_session, agent.id, after_id=None, since=row.ts, limit=500)
    assert row.id not in [i["id"] for i in items]
    items, nxt = await list_feed(db_session, agent.id, after_id=None,
                                 since=row.ts - timedelta(microseconds=1), limit=500)
    assert [i["id"] for i in items] == [row.id] and nxt == row.id  # ascending, cursor


async def test_feed_visibility(db_session, seeded):
    p, agent = await _feed_setup(db_session)
    hidden = await _portfolio(db_session)
    other = await _agent(db_session, "other")
    _deposit(db_session, p, 1)
    _deposit(db_session, hidden, 1)
    await notify(db_session, portfolio_id=None, kind="price_alert", agent_id=str(other.id),
                 payload={"symbol": "AAPL"})
    await notify(db_session, portfolio_id=None, kind="price_alert", agent_id=str(agent.id),
                 payload={"symbol": "MSFT"})
    other_global = await log_decision(db_session, other.id, kind="veto", summary="global")
    await set_kill_switch_and_cancel(db_session, True, cancel_open=False)
    await _settle(db_session)

    rows = await _rows(db_session)
    by = {(r.type, r.portfolio_id, r.agent_id): r.id for r in rows}
    items, _ = await list_feed(db_session, agent.id, after_id=None, since=None, limit=500)
    seen = {i["id"] for i in items}
    assert by[("portfolio.cash", p.id, None)] in seen
    assert by[("portfolio.cash", hidden.id, None)] not in seen
    assert by[("price_alert", None, other.id)] not in seen
    assert by[("price_alert", None, agent.id)] in seen
    assert by[("decision.veto", None, other.id)] in seen
    assert by[("kill_switch", None, None)] in seen
    decision = next(i for i in items if i["type"] == "decision.veto")
    assert decision["agent"] == other.name and decision["agent_id"] == other.id
    assert decision["data"]["decision_id"] == other_global.id


async def test_feed_already_sent_flag(db_session):
    p, agent = await _feed_setup(db_session)
    for amount in (1, 2, 3, 4):
        _deposit(db_session, p, amount)
    await _settle(db_session)
    rows = await _rows(db_session, portfolio_id=p.id)
    for row, state in zip(rows, ("pending", "queued", "digest", None), strict=True):
        row.telegram = state
    await db_session.flush()
    items, _ = await list_feed(db_session, agent.id, after_id=None, since=None, limit=10)
    flags = {i["id"]: i["already_sent"] for i in items}
    assert [flags[r.id] for r in rows] == [False, True, True, False]
