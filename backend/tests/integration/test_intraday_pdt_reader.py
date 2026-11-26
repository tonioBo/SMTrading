import hashlib
import logging
import uuid
from datetime import UTC, datetime
from decimal import Decimal as D

import httpx
import pytest
import respx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from smtrading.brokers.alpaca_paper import PAPER_URL
from smtrading.brokers.models import BrokerLink
from smtrading.brokers.service import create_link
from smtrading.common.db import make_engine
from smtrading.common.errors import ErrorCode, SMTError
from smtrading.engine.models import Fill, Order, OrderSide, OrderStatus, OrderType
from smtrading.intraday.pdt_reader import PDT_READ_INTERVAL_S, read_pdt_state, refresh_pdt_states
from smtrading.ledger.models import Journal, JournalType
from smtrading.portfolios.service import create_portfolio
from smtrading.seed import run_seed
from smtrading.telegram.models import TelegramOutbox

WED = datetime(2026, 11, 25, 17, 0, tzinfo=UTC)
KEY = "KID-CANARY-7731"
ACCT_ID = "acct-id-canary-5520"
KEYS = {"equity", "last_equity", "multiplier", "daytrade_count", "pattern_day_trader"}


def _account(**kw):
    base = {"id": ACCT_ID, "cash": "10000", "buying_power": "20000", "currency": "USD",
            "equity": "10000", "last_equity": "9900", "multiplier": "2",
            "daytrade_count": 0, "pattern_day_trader": False}
    base.update(kw)
    return base


def _mock_account(router, **kw):
    router.get(f"{PAPER_URL}/v2/positions").mock(return_value=httpx.Response(200, json=[]))
    router.get(f"{PAPER_URL}/v2/orders").mock(return_value=httpx.Response(200, json=[]))
    return router.get(f"{PAPER_URL}/v2/account").mock(
        return_value=httpx.Response(200, json=_account(**kw)))


@pytest.fixture
def router():
    with respx.mock(assert_all_mocked=True) as r:
        yield r


@pytest.fixture
async def env(fresh_db, redis_url, monkeypatch):
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
    yield sm
    await engine.dispose()


async def _link(sm, name, enabled=True):
    async with sm() as s:
        link = await create_link(s, name, "alpaca_paper",
                                 {"key_id": KEY, "secret_key": "SEC-CANARY"})
        link.enabled = enabled
        await s.commit()
        return link.id


async def _pf(sm, link_id, **kw):
    async with sm() as s:
        p = await create_portfolio(s, name=f"P-{uuid.uuid4().hex[:8]}",
                                   initial_cash=D(100000), leverage=D(1))
        p.broker_link_id = link_id
        for k, v in kw.items():
            setattr(p, k, v)
        await s.commit()
        return p.id


async def _fills(sm, pid, link_id, n=1):
    """n same-session buy+sell pairs on AAPL = n day trades."""
    async with sm() as s:
        for _ in range(n):
            for side, hour in ((OrderSide.buy, 15), (OrderSide.sell, 16)):
                o = Order(portfolio_id=pid, symbol="AAPL", side=side, type=OrderType.market,
                          qty=D(1), status=OrderStatus.filled, broker_link_id=link_id)
                s.add(o)
                await s.flush()
                h = hashlib.sha256(uuid.uuid4().bytes).hexdigest()
                j = Journal(type=JournalType.TRADE, author_type="system", prev_hash=h,
                            hash=hashlib.sha256(h.encode()).hexdigest())
                s.add(j)
                await s.flush()
                s.add(Fill(order_id=o.id, portfolio_id=pid, symbol="AAPL", side=side,
                           qty=D(1), price=D(100), fee_currency="USD", journal_id=j.id,
                           created_at=WED.replace(hour=hour)))
        await s.commit()


async def _state(sm, link_id):
    async with sm() as s:
        link = await s.get(BrokerLink, link_id)
        return link.pdt_state, link.pdt_state_at


async def _outbox(sm):
    async with sm() as s:
        return list((await s.scalars(select(TelegramOutbox.text))).all())


def test_interval():
    assert PDT_READ_INTERVAL_S == 300


async def test_reads_only_links_with_day_scope_portfolios(env, router):
    sm = env
    a, b, c = [await _link(sm, n) for n in ("A", "B", "C")]
    await _pf(sm, a, intraday_enabled=True)
    await _pf(sm, b, auto_rules_enabled=True)
    await _pf(sm, c)  # campaign portfolio, both switches off
    route = _mock_account(router)
    async with httpx.AsyncClient() as http:
        assert await refresh_pdt_states(sm, http, WED) == 2
    assert route.call_count == 2
    for link_id in (a, b):
        state, at = await _state(sm, link_id)
        assert set(state) == KEYS and at is not None
        assert state["multiplier"] == "2" and state["daytrade_count"] == 0
        assert state["pattern_day_trader"] is False
    assert await _state(sm, c) == (None, None)


async def test_mismatch_alert_once(env, router):
    sm = env
    link = await _link(sm, "Conto prova")
    pid = await _pf(sm, link, intraday_enabled=True)
    await _fills(sm, pid, link, 1)
    _mock_account(router, daytrade_count=2)
    async with httpx.AsyncClient() as http:
        await refresh_pdt_states(sm, http, WED)
        rows = await _outbox(sm)
        assert len(rows) == 1
        assert rows[0] == ("Conteggio operazioni di giornata diverso dal broker su "
                           "Conto prova: SMTrading 1, Alpaca 2")
        await refresh_pdt_states(sm, http, WED)
    assert len(await _outbox(sm)) == 1


async def test_day_trader_flag_alert_once_per_session(env, router):
    sm = env
    link = await _link(sm, "L")
    await _pf(sm, link, auto_rules_enabled=True)
    _mock_account(router, pattern_day_trader=True)
    async with httpx.AsyncClient() as http:
        await refresh_pdt_states(sm, http, WED)
        await refresh_pdt_states(sm, http, WED)
    rows = await _outbox(sm)
    assert len(rows) == 1 and "day trader" in rows[0]


async def test_read_failure_keeps_old_state(env, router):
    sm = env
    link = await _link(sm, "L")
    await _pf(sm, link, intraday_enabled=True)
    _mock_account(router)
    async with httpx.AsyncClient() as http:
        await refresh_pdt_states(sm, http, WED)
        before = await _state(sm, link)
        router.get(f"{PAPER_URL}/v2/account").mock(return_value=httpx.Response(500))
        assert await refresh_pdt_states(sm, http, WED) == 0
        assert await _state(sm, link) == before
        with pytest.raises(SMTError) as e:
            await read_pdt_state(sm, http, link)
    assert e.value.code == ErrorCode.PROVIDER_UNAVAILABLE


async def test_no_secret_in_state_or_logs(env, router, caplog):
    sm = env
    link = await _link(sm, "L")
    await _pf(sm, link, intraday_enabled=True)
    pid = await _pf(sm, link, intraday_enabled=True)
    await _fills(sm, pid, link, 1)
    _mock_account(router, daytrade_count=3)  # mismatch: logs a warning
    caplog.set_level(logging.DEBUG)
    async with httpx.AsyncClient() as http:
        await refresh_pdt_states(sm, http, WED)
        router.get(f"{PAPER_URL}/v2/account").mock(
            side_effect=httpx.ConnectError(f"boom {KEY} {ACCT_ID}"))
        await refresh_pdt_states(sm, http, WED)
    state, _ = await _state(sm, link)
    blob = repr(state) + caplog.text + repr(await _outbox(sm))
    assert KEY not in blob and ACCT_ID not in blob and "SEC-CANARY" not in blob
    assert "differ" in caplog.text or "diverso" in caplog.text


REAL_ACCOUNT = {  # shape of Alpaca's GET /v2/account (paper)
    "id": ACCT_ID, "account_number": "PA0000000000", "status": "ACTIVE", "crypto_status": "ACTIVE",
    "currency": "USD", "buying_power": "40000.5", "regt_buying_power": "40000.5",
    "daytrading_buying_power": "0", "non_marginable_buying_power": "10000", "cash": "10000",
    "accrued_fees": "0", "pending_transfer_in": "0", "portfolio_value": "10000.25",
    "pattern_day_trader": True, "trading_blocked": False, "transfers_blocked": False,
    "account_blocked": False, "created_at": "2025-01-02T10:00:00.123456Z",
    "trade_suspended_by_user": False, "multiplier": "4", "shorting_enabled": True,
    "equity": "10000.25", "last_equity": "9950.75", "long_market_value": "0",
    "short_market_value": "0", "initial_margin": "0", "maintenance_margin": "0",
    "last_maintenance_margin": "0", "sma": "0", "daytrade_count": 2,
}


async def test_realistic_account_payload(env, router):
    sm = env
    link = await _link(sm, "L")
    await _pf(sm, link, intraday_enabled=True)
    router.get(f"{PAPER_URL}/v2/positions").mock(return_value=httpx.Response(200, json=[]))
    router.get(f"{PAPER_URL}/v2/orders").mock(return_value=httpx.Response(200, json=[]))
    router.get(f"{PAPER_URL}/v2/account").mock(
        return_value=httpx.Response(200, json=REAL_ACCOUNT))
    async with httpx.AsyncClient() as http:
        state = await read_pdt_state(sm, http, link)
    assert state == {"equity": "10000.25", "last_equity": "9950.75", "multiplier": "4",
                     "daytrade_count": 2, "pattern_day_trader": True}
    assert (await _state(sm, link))[0] == state
