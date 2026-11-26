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
from smtrading.intraday.account_reader import (
    ACCOUNT_READ_INTERVAL_S,
    read_account_state,
    refresh_account_states,
)
from smtrading.portfolios.service import create_portfolio
from smtrading.seed import run_seed
from smtrading.telegram.models import TelegramOutbox

WED = datetime(2026, 11, 25, 17, 0, tzinfo=UTC)
KEY = "KID-CANARY-7731"
ACCT_ID = "acct-id-canary-5520"
KEYS = {"buying_power", "non_marginable_buying_power", "multiplier", "equity",
        "trading_blocked", "account_blocked", "read_at"}


def _account(**kw):
    base = {"id": ACCT_ID, "cash": "10000", "buying_power": "20000", "currency": "USD",
            "non_marginable_buying_power": "10000", "equity": "10000", "multiplier": "2",
            "trading_blocked": False, "account_blocked": False}
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


async def _state(sm, link_id):
    async with sm() as s:
        link = await s.get(BrokerLink, link_id)
        return link.pdt_state, link.pdt_state_at


async def _outbox(sm):
    async with sm() as s:
        return list((await s.scalars(select(TelegramOutbox.text))).all())


def test_interval():
    assert ACCOUNT_READ_INTERVAL_S == 300


async def test_reads_only_links_with_day_scope_portfolios(env, router):
    sm = env
    a, b, c = [await _link(sm, n) for n in ("A", "B", "C")]
    await _pf(sm, a, intraday_enabled=True)
    await _pf(sm, b, auto_rules_enabled=True)
    await _pf(sm, c)  # campaign portfolio, both switches off
    route = _mock_account(router)
    async with httpx.AsyncClient() as http:
        assert await refresh_account_states(sm, http, WED) == 2
    assert route.call_count == 2
    for link_id in (a, b):
        state, at = await _state(sm, link_id)
        assert set(state) == KEYS and at is not None
        assert state["read_at"] == at.isoformat()
        assert (state["buying_power"], state["non_marginable_buying_power"]) == ("20000", "10000")
        assert state["multiplier"] == "2" and state["equity"] == "10000"
        assert state["trading_blocked"] is False and state["account_blocked"] is False
    assert await _state(sm, c) == (None, None)
    assert await _outbox(sm) == []


async def test_blocked_account_alert_once_per_session(env, router):
    sm = env
    link = await _link(sm, "Conto prova")
    await _pf(sm, link, intraday_enabled=True)
    _mock_account(router, trading_blocked=True)
    thu = datetime(2026, 11, 27, 15, 0, tzinfo=UTC)
    async with httpx.AsyncClient() as http:
        await refresh_account_states(sm, http, WED)
        rows = await _outbox(sm)
        assert rows == [("Conto Conto prova bloccato dal broker (operatività): nessun nuovo "
                         "ordine passerà finché il broker non lo sblocca")]
        await refresh_account_states(sm, http, WED.replace(hour=19))  # same session
        assert len(await _outbox(sm)) == 1
        await refresh_account_states(sm, http, thu)  # next session
    assert len(await _outbox(sm)) == 2
    async with sm() as s:
        row = await s.scalar(select(TelegramOutbox).limit(1))
    assert (row.source, row.category, row.level) == ("system", "system", "warning")


async def test_account_blocked_alert(env, router):
    sm = env
    link = await _link(sm, "L")
    await _pf(sm, link, auto_rules_enabled=True)
    _mock_account(router, account_blocked=True)
    async with httpx.AsyncClient() as http:
        await refresh_account_states(sm, http, WED)
    rows = await _outbox(sm)
    assert len(rows) == 1 and "bloccato dal broker (conto)" in rows[0]


async def test_unblocked_no_alert(env, router):
    sm = env
    link = await _link(sm, "L")
    await _pf(sm, link, intraday_enabled=True)
    _mock_account(router)
    async with httpx.AsyncClient() as http:
        assert await refresh_account_states(sm, http, WED) == 1
    assert await _outbox(sm) == []


async def test_read_failure_keeps_old_state(env, router):
    sm = env
    link = await _link(sm, "L")
    await _pf(sm, link, intraday_enabled=True)
    _mock_account(router)
    async with httpx.AsyncClient() as http:
        await refresh_account_states(sm, http, WED)
        before = await _state(sm, link)
        router.get(f"{PAPER_URL}/v2/account").mock(return_value=httpx.Response(500))
        assert await refresh_account_states(sm, http, WED) == 0
        assert await _state(sm, link) == before
        with pytest.raises(SMTError) as e:
            await read_account_state(sm, http, link)
    assert e.value.code == ErrorCode.PROVIDER_UNAVAILABLE


async def test_no_secret_in_state_or_logs(env, router, caplog):
    sm = env
    link = await _link(sm, "L")
    await _pf(sm, link, intraday_enabled=True)
    _mock_account(router, trading_blocked=True)  # an alert on the outbox
    caplog.set_level(logging.DEBUG)
    async with httpx.AsyncClient() as http:
        await refresh_account_states(sm, http, WED)
        router.get(f"{PAPER_URL}/v2/account").mock(
            side_effect=httpx.ConnectError(f"boom {KEY} {ACCT_ID}"))
        await refresh_account_states(sm, http, WED)
    state, _ = await _state(sm, link)
    blob = repr(state) + caplog.text + repr(await _outbox(sm))
    assert KEY not in blob and ACCT_ID not in blob and "SEC-CANARY" not in blob
    assert "not read" in caplog.text and len(await _outbox(sm)) == 1


REAL_ACCOUNT = {  # shape of Alpaca's GET /v2/account (paper) after the end of the PDT rule
    "id": ACCT_ID, "admin_configurations": {}, "user_configurations": None,
    "account_number": "PA0000000000", "status": "ACTIVE", "crypto_status": "ACTIVE",
    "options_approved_level": 3, "options_trading_level": 3, "currency": "USD",
    "buying_power": "40000.5", "regt_buying_power": "20000.5",
    "effective_buying_power": "40000.5", "non_marginable_buying_power": "9800.25",
    "options_buying_power": "10000.25", "bod_dtbp": "0", "cash": "10000",
    "accrued_fees": "0", "portfolio_value": "10000.25", "trading_blocked": False,
    "transfers_blocked": False, "account_blocked": False,
    "created_at": "2025-01-02T10:00:00.123456Z", "trade_suspended_by_user": False,
    "multiplier": "4", "shorting_enabled": True, "equity": "10000.25",
    "last_equity": "9950.75", "long_market_value": "0", "short_market_value": "0",
    "position_market_value": "0", "initial_margin": "0", "maintenance_margin": "0",
    "last_maintenance_margin": "0", "sma": "0", "crypto_tier": 1,
    "intraday_adjustments": "0", "pending_reg_taf_fees": "0",
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
        state = await read_account_state(sm, http, link)
    saved, at = await _state(sm, link)
    assert state == saved == {
        "buying_power": "40000.5", "non_marginable_buying_power": "9800.25",
        "multiplier": "4", "equity": "10000.25", "trading_blocked": False,
        "account_blocked": False, "read_at": at.isoformat()}
    for gone in ("daytrade_count", "pattern_day_trader", "intraday_adjustments",
                 "account_number", "id"):
        assert gone not in state
