# ruff: noqa: F811  (the fixtures imported from test_live_service are used as arguments)
"""Several real portfolios on one broker account (plan 9, Task 11): cash quotas, linking,
the cash cap, the per-link fuse, the gate, per-symbol reconciliation and the deposit
guard. Never reaches the network: the broker is the in-memory FakeLiveBroker behind the
live gate. Canary keys only. The helpers and fixtures come from test_live_service."""

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
import respx
from sqlalchemy import select, update
from test_live_service import (  # noqa: F401  (fixtures: sm, redis, fake, autouse env)
    ALPACA,
    OPEN,
    WHO,
    Fake,
    _admin,
    _arm,
    _audit,
    _env,
    _expect,
    _held_position,
    _link,
    _profile,
    _qs,
    _registered_secrets,
    _telegram,
    _tested,
    fake,
    redis,
    sm,
)

from smtrading.activity.models import Activity
from smtrading.admin.models import AdminUser
from smtrading.brokers.live.account import LIVE_AAD, account_fingerprint
from smtrading.brokers.live.deposit import DEPOSIT_SHORT_MESSAGE, BrokerCash, check_live_deposit
from smtrading.brokers.live.gate import (
    OTHER_PORTFOLIO_MESSAGE,
    LiveGateClosed,
    make_live_client,
)
from smtrading.brokers.live.quotas import link_quotas, portfolio_quota, unassigned
from smtrading.brokers.live.reconcile import reconcile_live
from smtrading.brokers.live.service import (
    disarm_live_link,
    link_live_portfolio,
    set_dry_run,
)
from smtrading.brokers.models import BrokerLink, Reconciliation
from smtrading.brokers.service import link_portfolio
from smtrading.common.crypto import encrypt_json
from smtrading.common.errors import ErrorCode, SMTError
from smtrading.engine.models import BrokerState, Order, OrderSide, OrderStatus, OrderType
from smtrading.engine.quotesource import FakeQuoteSource
from smtrading.portfolios.models import Portfolio, PortfolioStatus
from smtrading.portfolios.service import create_portfolio, get_cash_balances
from smtrading.risk.live_checks import check_live_cash, check_live_fuse, check_no_short
from smtrading.risk.settings import set_kill_switch

D = Decimal
ACCOUNT = "fake-alpaca-account"


def _snapshot(cash="1000", positions=0, orders=0, ccy="USD") -> dict:
    return {"currency": ccy, "cash_available": cash, "positions_count": positions,
            "open_orders_count": orders, "account_type": "cash"}


async def _new_portfolio(sm, cash, ccy="USD", name=None, profile=None) -> uuid.UUID:
    profile = profile or await _profile(sm)
    async with sm() as s:
        p = await create_portfolio(s, name=name or f"P-{uuid.uuid4().hex[:6]}",
                                   initial_cash=D(cash), base_currency=ccy, leverage=D(1),
                                   risk_profile_id=profile)
        await s.commit()
        return p.id


async def _multi(sm, cashes=("500", "400"), *, ccys=None, state="armed",
                 broker_cash="1000", names=None) -> tuple[uuid.UUID, list[uuid.UUID]]:
    """An Alpaca live link (1000 USD by default, fresh snapshot) with one linked USD
    portfolio per cash amount (names A, B, C... so the quotas are ordered)."""
    names = names or [chr(65 + i) + "-" + uuid.uuid4().hex[:4] for i in range(len(cashes))]
    pids = [await _new_portfolio(sm, c, (ccys or {}).get(i, "USD"), names[i])
            for i, c in enumerate(cashes)]
    now = datetime.now(UTC)
    async with sm() as s:
        link = BrokerLink(name=f"L-{uuid.uuid4().hex[:8]}", kind="alpaca_live",
                          live_state=state,
                          config_encrypted=encrypt_json(ALPACA, "test-master-key",
                                                        aad=LIVE_AAD),
                          account_fingerprint=account_fingerprint("alpaca_live", ACCOUNT),
                          last_account_snapshot=_snapshot(broker_cash),
                          last_snapshot_at=now, last_test_at=now)
        s.add(link)
        await s.flush()
        await s.execute(update(Portfolio).where(Portfolio.id.in_(pids))
                        .values(broker_link_id=link.id))
        await s.commit()
        return link.id, pids


async def _status(sm, pid) -> PortfolioStatus:
    async with sm() as s:
        return (await s.get(Portfolio, pid)).status


async def _order(s, pid, link_id, **kw) -> uuid.UUID:
    values = {"side": OrderSide.buy, "type": OrderType.limit, "qty": D(1),
              "limit_price": D(100), "status": OrderStatus.pending, "symbol": "AAPL", **kw}
    o = Order(portfolio_id=pid, broker_link_id=link_id, **values)
    s.add(o)
    await s.flush()
    return o.id


async def _cash_check(sm, pid, link_id, value, *, qs=None) -> None:
    async with sm() as s:
        await check_live_cash(s, qs or _qs(), portfolio=await s.get(Portfolio, pid),
                              link=await s.get(BrokerLink, link_id), est_value=D(value),
                              est_fees=D(0), now=datetime.now(UTC))


async def _link_via_service(sm, redis, pid, link_id, admin, totp, qs=None):
    async with sm() as s:
        pf = await link_live_portfolio(s, redis, pid, link_id,
                                       admin=await s.get(AdminUser, admin.id),
                                       totp=totp.code(), actor=WHO, qs=qs or _qs())
        await s.commit()
        return pf


@pytest.fixture
async def committed(fresh_db):
    """Sessions that really commit: each reconciliation row gets its own `ts` (inside one
    rolled-back transaction now() never moves, and the debounce reads the latest row)."""
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from smtrading.common.db import make_engine
    from smtrading.seed import run_seed

    engine = make_engine(fresh_db)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as s:
        await run_seed(s)
        await s.commit()
    yield maker
    await engine.dispose()


# -- linking ---------------------------------------------------------------------------

async def _tested_dry_run(sm, fake) -> uuid.UUID:
    link_id = await _tested(sm, fake)  # Alpaca fake: 1000.00 USD, empty
    async with sm() as s:
        await set_dry_run(s, link_id, True, actor=WHO)
        await s.commit()
    return link_id


async def test_link_second_portfolio_when_quotas_fit(sm, redis, fake):
    admin, totp = await _admin(sm)
    link_id = await _tested_dry_run(sm, fake)
    a = await _new_portfolio(sm, "500", name="A")
    b = await _new_portfolio(sm, "400", name="B")
    await _link_via_service(sm, redis, a, link_id, admin, totp)
    pf = await _link_via_service(sm, redis, b, link_id, admin, totp)
    assert pf.broker_link_id == link_id
    rows = [r for r in await _audit(sm, link_id) if r.action == "portfolio_linked"]
    assert [r.portfolio_id for r in rows] == [a, b]
    async with sm() as s:
        quotas = await link_quotas(s, _qs(), await s.get(BrokerLink, link_id))
    assert [q.portfolio_id for q in quotas] == [a, b]
    assert unassigned(D("1000"), quotas) == D(100)


async def test_link_third_portfolio_refused_when_quotas_exceed_cash(sm, redis, fake):
    admin, totp = await _admin(sm)
    link_id = await _tested_dry_run(sm, fake)
    for cash in ("500", "400"):
        await _link_via_service(sm, redis, await _new_portfolio(sm, cash), link_id, admin,
                                totp)
    before = len(await _audit(sm, link_id))
    c = await _new_portfolio(sm, "200")
    async with sm() as s:
        e = await _expect(link_live_portfolio(s, redis, c, link_id,
                                              admin=await s.get(AdminUser, admin.id),
                                              totp=totp.code(), actor=WHO, qs=_qs()),
                          ErrorCode.INVALID_ORDER, "cash")
    assert e.message == ("la somma delle quote dei portafogli supera la liquidità disponibile"
                         " al broker")
    assert D(e.details["quotas_total"]) == D(1100) and D(e.details["unassigned"]) == D(-100)
    async with sm() as s:
        assert (await s.get(Portfolio, c)).broker_link_id is None
    assert len(await _audit(sm, link_id)) == before  # Review Focus 6: nothing recorded


async def test_link_second_portfolio_requires_reconciled_account(committed, redis, fake):
    sm = committed  # rows with their own timestamps (real commits)
    admin, totp = await _admin(sm)
    link_id = await _tested_dry_run(sm, fake)
    a = await _new_portfolio(sm, "500")
    await _link_via_service(sm, redis, a, link_id, admin, totp)
    await _held_position(sm, a, link_id, qty="2")
    fake.alpaca.positions["AAPL"] = D(2)
    async with sm() as s:  # the account the fake reports now holds A's position
        cash = sum((await get_cash_balances(s, a)).values(), D(0))
    fake.alpaca.cash = cash + D(100)  # room for B's quota
    async with _client(sm, link_id, fake) as http:
        from smtrading.brokers.live.service import test_live_link

        await test_live_link(sm, link_id, actor=WHO, http=http)
    async with sm() as s:
        s.add(Reconciliation(broker_link_id=link_id, portfolio_id=a, ok=False,
                             diffs=[{"symbol": "AAPL", "ours": "2", "broker": "3"}]))
        await s.commit()
    b = await _new_portfolio(sm, "100")
    async with sm() as s:
        await _expect(link_live_portfolio(s, redis, b, link_id,
                                          admin=await s.get(AdminUser, admin.id),
                                          totp=totp.code(), actor=WHO, qs=_qs()),
                      ErrorCode.INVALID_ORDER, "reconcile")
    row = await reconcile_live(sm, link_id, lambda lid: _client(sm, lid, fake), qs=_qs())
    assert row.ok, row.diffs
    pf = await _link_via_service(sm, redis, b, link_id, admin, totp)
    assert pf.broker_link_id == link_id


async def test_second_portfolio_foreign_currency_quota_worst_rate(sm):
    """B in EUR on the USD account: the higher of the current rate (1.25) and the
    reference (1.30), plus 1 %."""
    link_id, (_a, b) = await _multi(sm, ("500", "200"), ccys={1: "EUR"})
    async with sm() as s:
        link = await s.get(BrokerLink, link_id)
        link.fx_ref_rates = {"EUR": "1.30"}
        await s.commit()
    async with sm() as s:
        link = await s.get(BrokerLink, link_id)
        assert await portfolio_quota(s, _qs(), await s.get(Portfolio, b), link) == \
            D(200) * D("1.30") * D("1.01")
        link.fx_ref_rates = {"EUR": "1.20"}
        assert await portfolio_quota(s, _qs(), await s.get(Portfolio, b), link) == \
            D(200) * D("1.25") * D("1.01")
        with pytest.raises(SMTError) as e:  # no current rate
            await portfolio_quota(s, FakeQuoteSource(), await s.get(Portfolio, b), link)
        assert e.value.code == ErrorCode.PROVIDER_UNAVAILABLE


# -- buys and sells --------------------------------------------------------------------

async def test_buy_cannot_spend_other_portfolios_quota(sm):
    link_id, (_a, b) = await _multi(sm, ("500", "400"))
    async with sm() as s:
        link = await s.get(BrokerLink, link_id)
        link.last_account_snapshot = _snapshot("800")
        await s.commit()
    # live_cash_needed adds 2 %: 300 available for B -> at most 294.11 of value
    with pytest.raises(SMTError) as e:
        await _cash_check(sm, b, link_id, "295")
    assert e.value.code == ErrorCode.LIVE_CASH_LIMIT
    assert D(e.value.details["others_reserved"]) == D(500)
    await _cash_check(sm, b, link_id, "294")


async def test_buy_recheck_before_send_uses_other_quotas(fresh_db, redis_url, monkeypatch):
    """The same cap in BrokerSync.tick() with a fresh snapshot (Review Focus 6)."""
    from redis.asyncio import Redis
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from smtrading.brokers.sync import BrokerSync
    from smtrading.common.config import get_settings
    from smtrading.common.db import make_engine
    from smtrading.engine.service import PlaceOrderRequest, place_order
    from smtrading.seed import run_seed

    monkeypatch.setenv("SMT_DATABASE_URL", fresh_db)
    get_settings.cache_clear()
    engine = make_engine(fresh_db)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    redis = Redis.from_url(redis_url)
    try:
        async with maker() as s:
            await run_seed(s)
            await s.commit()
        link_id, (_a, b) = await _multi(maker, ("500", "400"))
        qs = _qs()
        async with maker() as s:  # 3 x 100 (+2 %) = 306: fits B's 400 and 1000 - 500
            o = await place_order(s, qs, PlaceOrderRequest(
                portfolio_id=b, symbol="AAPL", side=OrderSide.buy,
                type=OrderType.market, qty=D(3)), now=OPEN)
            await s.commit()
        async with maker() as s:
            link = await s.get(BrokerLink, link_id)
            link.last_account_snapshot = _snapshot("800")  # 800 - A's 500 = 300 for B
            link.last_snapshot_at = OPEN
            await s.commit()
        with respx.mock(assert_all_mocked=True, assert_all_called=False) as mock:
            await BrokerSync(maker, redis, qs, None, clock=lambda: OPEN).tick()
        assert mock.calls.call_count == 0
        async with maker() as s:
            o = await s.get(Order, o.id)
        assert o.status == OrderStatus.rejected and o.reject_code == ErrorCode.LIVE_CASH_LIMIT
    finally:
        await redis.aclose()
        await engine.dispose()


async def test_other_portfolios_open_buy_not_counted_twice(sm):
    """A (500) has an open buy of 400 at the broker; its quota is still its cash, 500."""
    link_id, (a, b) = await _multi(sm, ("500", "400"))
    async with sm() as s:
        await _order(s, a, link_id, qty=D(4), limit_price=D(100), broker_status="new",
                     broker_order_id="bo-a", broker_sent_at=datetime.now(UTC),
                     status=OrderStatus.pending)
        await s.commit()
    await _cash_check(sm, b, link_id, "392")  # 392 x 1.02 = 399.84 <= min(400, 500)
    with pytest.raises(SMTError):
        await _cash_check(sm, b, link_id, "393")


async def test_unknown_order_trips_only_its_portfolio_and_stays_committed(sm):
    from smtrading.brokers.live.protect import trip_reduce_only

    link_id, (a, b) = await _multi(sm, ("500", "400"))
    async with sm() as s:  # an unknown outcome (M8): BrokerSync trips the order's portfolio
        await _order(s, a, link_id, qty=D(2), limit_price=D(100),
                     broker_status=BrokerState.UNKNOWN, broker_sent_at=datetime.now(UTC))
        await trip_reduce_only(s, a, cause="order_unknown", details={"link_id": link_id})
        await s.commit()
    assert await _status(sm, a) == PortfolioStatus.reduce_only
    assert await _status(sm, b) == PortfolioStatus.active
    async with sm() as s:  # A's quota is its cash, the open order still committed
        link = await s.get(BrokerLink, link_id)
        assert await portfolio_quota(s, _qs(), await s.get(Portfolio, a), link) == D(500)
    with pytest.raises(SMTError) as e:  # A: 500 less the open 204
        await _cash_check(sm, a, link_id, "300")
    assert D(e.value.details["committed"]) > D(204)  # 2 x 100 + fees + 2 %


async def test_sell_limited_to_own_holdings(sm):
    link_id, (a, b) = await _multi(sm, ("1000", "1000"), broker_cash="5000")
    await _held_position(sm, a, link_id, qty="3")
    await _held_position(sm, b, link_id, qty="2")
    async with sm() as s:
        with pytest.raises(SMTError) as e:
            await check_no_short(s, portfolio=await s.get(Portfolio, b), symbol="AAPL",
                                 qty=D(3))
        assert e.value.code == ErrorCode.SHORT_NOT_ALLOWED
        await check_no_short(s, portfolio=await s.get(Portfolio, b), symbol="AAPL", qty=D(2))


# -- the fuse --------------------------------------------------------------------------

async def test_fuse_counts_link_orders_and_trips_all(sm):
    link_id, (a, b) = await _multi(sm, ("500", "400"))
    now = datetime.now(UTC)
    async with sm() as s:
        for pid, limit in ((a, 5), (b, 3)):
            p = await s.get(Portfolio, pid)
            p.risk_profile_id = await _profile_id(s, limit)
        for pid in (a, a, b):
            await _order(s, pid, link_id, broker_status="new", broker_order_id=uuid.uuid4().hex,
                         broker_sent_at=now - timedelta(minutes=5))
        await s.commit()
    async with sm() as s:
        with pytest.raises(SMTError) as e:
            await check_live_fuse(s, portfolio=await s.get(Portfolio, b),
                                  link=await s.get(BrokerLink, link_id), now=now,
                                  reduce_only=False, live_flatten=False)
        assert e.value.code == ErrorCode.RISK_LIMIT_EXCEEDED
        assert e.value.details == {"sent_last_hour": 3, "limit": 3}
        await s.commit()
    assert await _status(sm, a) == PortfolioStatus.reduce_only
    assert await _status(sm, b) == PortfolioStatus.reduce_only


async def _profile_id(s, limit: int) -> uuid.UUID:
    from smtrading.risk.models import RiskProfile

    p = RiskProfile(name=f"F-{uuid.uuid4().hex[:6]}", max_order_notional_eur=D(500),
                    max_daily_loss_pct=D(5), short_allowed=False,
                    max_live_orders_per_hour=limit)
    s.add(p)
    await s.flush()
    return p.id


# -- the gate, disarm, dry run, kill switch -----------------------------------------------

def _client(sm, link_id, fake):
    return make_live_client(sm, link_id, inner=fake.transport())


async def _post(sm, link_id, order_id, fake):
    async with _client(sm, link_id, fake) as http:
        return await http.post("https://paper-api.invalid/v2/orders",
                               json={"symbol": "AAPL", "side": "buy", "qty": "1"},
                               extensions={"smt_order_id": str(order_id)})


async def test_gate_accepts_any_linked_portfolio(sm):
    link_id, (a, b) = await _multi(sm, ("500", "400"))
    other = await _new_portfolio(sm, "100")
    async with sm() as s:
        (await s.get(Portfolio, a)).status = PortfolioStatus.locked
        ob = await _order(s, b, link_id)
        oo = await _order(s, other, link_id)
        await s.commit()
    fake = Fake()
    r = await _post(sm, link_id, ob, fake)
    assert r.status_code != 403 and len(fake.writes) == 1
    async with sm() as s:
        (await s.get(Portfolio, b)).status = PortfolioStatus.locked
        await s.commit()
    fake = Fake()
    with pytest.raises(LiveGateClosed) as e:
        await _post(sm, link_id, ob, fake)
    assert e.value.code == ErrorCode.PORTFOLIO_LOCKED
    with pytest.raises(LiveGateClosed) as e:
        await _post(sm, link_id, oo, fake)
    assert (e.value.code, e.value.message) == (ErrorCode.LIVE_NOT_ARMED,
                                               OTHER_PORTFOLIO_MESSAGE)
    assert fake.writes == []


async def test_disarm_stops_all_linked_portfolios(sm):
    link_id, (a, b) = await _multi(sm, ("500", "400"))
    async with sm() as s:
        sent = [await _order(s, pid, link_id, broker_status="new",
                             broker_order_id=uuid.uuid4().hex,
                             broker_sent_at=datetime.now(UTC)) for pid in (a, b)]
        unsent = [await _order(s, pid, link_id, broker_status=BrokerState.TO_SEND)
                  for pid in (a, b)]
        await s.commit()
    async with sm() as s:
        assert await disarm_live_link(s, link_id, actor=WHO, reason="stop") == 4
        await s.commit()
    async with sm() as s:
        for oid in sent:
            assert (await s.get(Order, oid)).broker_status == BrokerState.CANCEL_REQUESTED
        for oid in unsent:
            assert (await s.get(Order, oid)).status == OrderStatus.rejected
        new = [await _order(s, pid, link_id) for pid in (a, b)]
        await s.commit()
    fake = Fake()
    for oid in new:  # Review Focus 6: the gate refuses a POST for both
        with pytest.raises(LiveGateClosed) as e:
            await _post(sm, link_id, oid, fake)
        assert e.value.code == ErrorCode.LIVE_NOT_ARMED
    assert fake.writes == []


async def test_dry_run_and_kill_switch_apply_to_all_linked(sm):
    from smtrading.brokers.live.gate import LiveDryRun

    link_id, (a, b) = await _multi(sm, ("500", "400"), state="dry_run")
    async with sm() as s:
        orders = [await _order(s, pid, link_id) for pid in (a, b)]
        await s.commit()
    fake = Fake()
    for oid in orders:
        with pytest.raises(LiveDryRun):
            await _post(sm, link_id, oid, fake)
    async with sm() as s:
        (await s.get(BrokerLink, link_id)).live_state = "armed"
        await set_kill_switch(s, True)
        await s.commit()
    for oid in orders:
        with pytest.raises(LiveGateClosed) as e:
            await _post(sm, link_id, oid, fake)
        assert e.value.code == ErrorCode.KILL_SWITCH_ACTIVE
    assert fake.writes == []


# -- unlink ----------------------------------------------------------------------------

async def test_unlink_one_portfolio_keeps_the_others(sm):
    link_id, (a, b) = await _multi(sm, ("1000", "400"), broker_cash="5000")
    await _held_position(sm, a, link_id, qty="2")
    async with sm() as s:
        await link_portfolio(s, b, None, actor=WHO)
        await s.commit()
    async with sm() as s:
        assert (await s.get(Portfolio, b)).broker_link_id is None
        assert (await s.get(Portfolio, a)).broker_link_id == link_id
        assert (await s.get(BrokerLink, link_id)).live_state == "armed"
    async with sm() as s:
        e = await _expect(link_portfolio(s, a, None, actor=WHO), ErrorCode.INVALID_ORDER,
                          "positions")
    assert e.message == "Chiudi prima le posizioni reali"


# -- the deposit guard -----------------------------------------------------------------

async def test_deposit_guard_uses_sum_of_quotas(sm):
    link_id, (a, _b) = await _multi(sm, ("400", "300"))
    broker = BrokerCash(link_id, D("1000"), "USD")
    async with sm() as s:
        pf = await s.get(Portfolio, a)
        await check_live_deposit(s, _qs(), pf, broker, D("300"), "USD")
        with pytest.raises(SMTError) as e:
            await check_live_deposit(s, _qs(), pf, broker, D("301"), "USD")
    assert e.value.message == DEPOSIT_SHORT_MESSAGE and e.value.details["field"] == "amount"


# -- reconciliation --------------------------------------------------------------------

async def _broker_matching(sm, fake, pids, positions: dict[str, str]) -> None:
    """The fake account holds the portfolios' cash and `positions`."""
    total = D(0)
    async with sm() as s:
        for pid in pids:
            total += sum((await get_cash_balances(s, pid)).values(), D(0))
    fake.alpaca.cash = total
    fake.alpaca.positions = {k: D(v) for k, v in positions.items()}


async def _pass(sm, link_id, fake, qs=None):
    return await reconcile_live(sm, link_id, lambda lid: _client(sm, lid, fake),
                                qs=qs or _qs())


async def _three_with_aapl(sm, fake):
    link_id, pids = await _multi(sm, ("1000", "1000", "500"), broker_cash="5000")
    a, b, _c = pids
    await _held_position(sm, a, link_id, qty="3")
    await _held_position(sm, b, link_id, qty="2")
    await _broker_matching(sm, fake, pids, {"AAPL": "5"})
    return link_id, pids


async def test_reconcile_same_symbol_sum_ok(committed, fake):
    sm = committed  # rows with their own timestamps (real commits)
    link_id, pids = await _three_with_aapl(sm, fake)
    assert (await _pass(sm, link_id, fake)).ok
    async with sm() as s:
        rows = list(await s.scalars(select(Reconciliation).where(
            Reconciliation.broker_link_id == link_id)))
    assert {r.portfolio_id for r in rows} == set(pids) and all(r.ok for r in rows)


async def test_reconcile_symbol_diff_trips_only_holders(committed, fake):
    sm = committed  # rows with their own timestamps (real commits)
    link_id, (a, b, c) = await _three_with_aapl(sm, fake)
    fake.alpaca.positions["AAPL"] = D(4)
    await _pass(sm, link_id, fake)
    assert [await _status(sm, p) for p in (a, b, c)] == [PortfolioStatus.active] * 3
    await _pass(sm, link_id, fake)
    assert [await _status(sm, p) for p in (a, b, c)] == [
        PortfolioStatus.reduce_only, PortfolioStatus.reduce_only, PortfolioStatus.active]
    async with sm() as s:
        assert (await s.get(Portfolio, a)).reduce_only_reason == "live_reconcile_diff"


async def test_reconcile_unknown_symbol_trips_all(committed, fake):
    sm = committed  # rows with their own timestamps (real commits)
    link_id, pids = await _three_with_aapl(sm, fake)
    fake.alpaca.positions["MSFT"] = D(1)
    await _pass(sm, link_id, fake)
    assert {await _status(sm, p) for p in pids} == {PortfolioStatus.active}
    await _pass(sm, link_id, fake)
    assert {await _status(sm, p) for p in pids} == {PortfolioStatus.reduce_only}


async def test_reconcile_debounce_per_symbol(committed, fake):
    sm = committed  # rows with their own timestamps (real commits)
    link_id, pids = await _multi(sm, ("1000", "1000"), broker_cash="5000")
    a, b = pids
    await _held_position(sm, a, link_id, qty="3")
    async with sm() as s:  # B holds MSFT
        from smtrading.instruments.models import AssetClass, Instrument

        if await s.get(Instrument, "MSFT") is None:
            s.add(Instrument(symbol="MSFT", name="Microsoft", asset_class=AssetClass.us_equity,
                             market="NASDAQ", currency="USD"))
        await s.commit()
    qs = _qs()
    qs.set_quote("MSFT", D("99.90"), D("100.00"), D("99.95"))
    async with sm() as s:
        await s.execute(update(Portfolio).where(Portfolio.id == b).values(broker_link_id=None))
        from smtrading.engine.service import PlaceOrderRequest, place_order

        await place_order(s, qs, PlaceOrderRequest(
            portfolio_id=b, symbol="MSFT", side=OrderSide.buy, type=OrderType.market,
            qty=D(2)), now=OPEN)
        await s.execute(update(Portfolio).where(Portfolio.id == b)
                        .values(broker_link_id=link_id))
        await s.commit()
    await _broker_matching(sm, fake, pids, {"AAPL": "4", "MSFT": "2"})  # AAPL differs
    await _pass(sm, link_id, fake, qs)
    fake.alpaca.positions["MSFT"] = D(1)  # pass 2: AAPL again, MSFT new
    await _pass(sm, link_id, fake, qs)
    assert (await _status(sm, a), await _status(sm, b)) == (PortfolioStatus.reduce_only,
                                                            PortfolioStatus.active)
    await _pass(sm, link_id, fake, qs)  # pass 3: MSFT confirmed
    assert await _status(sm, b) == PortfolioStatus.reduce_only


async def test_cash_shortfall_trips_all_linked(committed, fake):
    sm = committed  # rows with their own timestamps (real commits)
    link_id, pids = await _multi(sm, ("500", "300", "200"))
    fake.alpaca.cash = D("990")  # 10 short: tolerance max(0.625, 1.0) = 1
    for _ in range(2):
        await _pass(sm, link_id, fake)
    async with sm() as s:
        for pid in pids:
            p = await s.get(Portfolio, pid)
            assert (p.status, p.reduce_only_reason) == (PortfolioStatus.reduce_only,
                                                        "live_cash_shortfall")


async def test_fx_drift_alert_once_per_link_per_day(committed, fake):
    sm = committed  # rows with their own timestamps (real commits)
    link_id, pids = await _multi(sm, ("500", "500"), ccys={0: "EUR", 1: "EUR"})
    fake.alpaca.cash = D("1250")  # 1000 EUR at 1.25
    q125 = FakeQuoteSource()
    q125.set_fx("EUR", "USD", D("1.25"))
    assert (await _pass(sm, link_id, fake, q125)).ok
    q130 = FakeQuoteSource()
    q130.set_fx("EUR", "USD", D("1.30"))
    for _ in range(2):
        assert (await _pass(sm, link_id, fake, q130)).ok
    async with sm() as s:
        alerts = list(await s.scalars(select(Activity).where(
            Activity.type == "live.fx_drift",
            Activity.data["link_id"].astext == str(link_id))))
    assert len(alerts) == 1
    assert {await _status(sm, p) for p in pids} == {PortfolioStatus.active}


# -- the portal --------------------------------------------------------------------------

async def test_arm_records_every_linked_portfolio(sm, redis, fake):
    admin, totp = await _admin(sm)
    await _telegram(sm)
    link_id = await _tested_dry_run(sm, fake)
    a = await _new_portfolio(sm, "500")
    b = await _new_portfolio(sm, "400")
    for pid in (a, b):
        await _link_via_service(sm, redis, pid, link_id, admin, totp)
    await _arm(sm, redis, link_id, admin, totp)
    row = [r for r in await _audit(sm, link_id) if r.action == "armed"][-1]
    assert set(row.details["portfolios"]) == {str(a), str(b)}
    assert row.portfolio_id in (a, b)
    assert (await _link(sm, link_id)).live_state == "armed"

