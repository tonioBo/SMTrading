"""The deposit guard of a real-money portfolio (plan 9, Task 7, D1): a deposit is
registered only when the broker account already holds that cash. Never reaches the
network: the live broker is the in-memory FakeLiveBroker behind the live gate
(app.state.live_http_factory). Canary keys only."""

import asyncio
import uuid
from decimal import Decimal

import httpx
import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

from smtrading.brokers.live.account import LIVE_AAD, account_fingerprint
from smtrading.brokers.live.deposit import (
    DEPOSIT_SHORT_MESSAGE,
    UNREADABLE_MESSAGE,
    BrokerCash,
    check_live_deposit,
)
from smtrading.brokers.live.fake import FakeLiveBroker
from smtrading.brokers.live.gate import make_live_client
from smtrading.brokers.models import BrokerLink
from smtrading.common.config import get_settings
from smtrading.common.crypto import encrypt_json
from smtrading.common.db import make_engine
from smtrading.common.errors import ErrorCode, SMTError
from smtrading.engine.quotesource import FakeQuoteSource
from smtrading.portfolios.models import Portfolio
from smtrading.portfolios.service import get_cash_balances

D = Decimal
PASSWORD = "correct-horse-battery"
MASTER = "test-master-key"
CONFIG = {
    "alpaca_live": ({"key_id": "CANARYKEYID0001", "secret_key": "CANARYSECRET-do-not-use"},
                    "fake-alpaca-account"),
    "t212_live": ({"api_key": "CANARYKEYID0001", "api_secret": "CANARYSECRET-do-not-use"},
                  "1000001"),
}


@pytest.fixture
async def admin(fresh_db, redis_url, monkeypatch):
    from smtrading.admin.auth import hash_password
    from smtrading.admin.models import AdminUser
    from smtrading.api.main import create_app
    from smtrading.engine.quotesource import FakeMarketData
    from smtrading.mcp.runtime import McpRuntime
    from smtrading.seed import run_seed

    monkeypatch.setenv("SMT_DATABASE_URL", fresh_db)
    monkeypatch.setenv("SMT_REDIS_URL", redis_url)
    monkeypatch.setenv("SMT_MASTER_KEY", MASTER)
    monkeypatch.setenv("SMT_ADMIN_EMAIL", "admin@example.com")
    monkeypatch.setenv("SMT_ADMIN_PASSWORD", "test-password")
    get_settings.cache_clear()
    engine = make_engine(fresh_db)
    sm = async_sessionmaker(engine, expire_on_commit=False)
    email = f"admin-{uuid.uuid4().hex[:8]}@example.com"
    async with sm() as s:
        await run_seed(s)
        s.add(AdminUser(email=email, password_hash=await hash_password(PASSWORD)))
        await s.commit()
    qs = FakeMarketData()
    qs.set_fx("EUR", "USD", D("1.25"))
    app = create_app(runtime_factory=lambda sm_, redis: McpRuntime(sm_, redis, qs, qs))
    fake = FakeLiveBroker()
    app.state.live_http_factory = lambda link_id: make_live_client(
        sm, link_id, inner=fake.transport())
    started, stop = asyncio.Event(), asyncio.Event()

    async def run_lifespan():
        async with app.router.lifespan_context(app):
            started.set()
            await stop.wait()

    lifespan = asyncio.create_task(run_lifespan())
    await asyncio.wait_for(started.wait(), 15)
    try:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            r = await client.post("/api/auth/login", json={"email": email, "password": PASSWORD})
            assert r.status_code == 200, r.text
            client.headers["X-CSRF-Token"] = client.cookies["smt_csrf"]

            class NS:
                pass

            ns = NS()
            ns.client, ns.sm, ns.fake, ns.app = client, sm, fake, app
            yield ns
    finally:
        stop.set()
        await lifespan
        await engine.dispose()


async def _portfolio(admin, *, cash="500", ccy="EUR", kind: str | None = "t212_live"
                     ) -> uuid.UUID:
    """A portfolio (`cash` in `ccy`), linked to an armed live link of `kind` (None: not
    linked)."""
    r = await admin.client.post("/api/portfolios", json={
        "name": f"P-{uuid.uuid4().hex[:8]}", "initial_cash": cash, "base_currency": ccy})
    assert r.status_code == 200, r.text
    pid = uuid.UUID(r.json()["id"])
    if kind is not None:
        config, account = CONFIG[kind]
        async with admin.sm() as s:
            link = BrokerLink(name=f"L-{uuid.uuid4().hex[:8]}", kind=kind, live_state="armed",
                              config_encrypted=encrypt_json(config, MASTER, aad=LIVE_AAD),
                              account_fingerprint=account_fingerprint(kind, account))
            s.add(link)
            await s.flush()
            (await s.get(Portfolio, pid)).broker_link_id = link.id
            await s.commit()
    return pid


async def _deposit(admin, pid, amount: str, path="deposit") -> httpx.Response:
    return await admin.client.post(f"/api/portfolios/{pid}/{path}", json={"amount": amount},
                                   headers={"Idempotency-Key": uuid.uuid4().hex})


async def _cash(admin, pid) -> dict[str, Decimal]:
    async with admin.sm() as s:
        return await get_cash_balances(s, pid)


def _refused(r: httpx.Response, message: str = DEPOSIT_SHORT_MESSAGE) -> None:
    assert r.status_code == 400, r.text
    error = r.json()
    assert error["code"] == "INVALID_ORDER"
    assert error["details"]["field"] == "amount"
    assert error["message"] == message


async def test_deposit_refused_when_broker_cash_short(admin):
    """The account holds 600 EUR, the portfolio 500: 200 more is refused, 100 is fine."""
    admin.fake.t212.cash = D("600")
    pid = await _portfolio(admin)
    r = await _deposit(admin, pid, "200")
    _refused(r)
    assert ("Il conto del broker non ha ancora questo contante: fai prima il bonifico al"
            " broker, poi registra il deposito") == r.json()["message"]
    assert "600" not in r.text
    assert await _cash(admin, pid) == {"EUR": D(500)}
    r = await _deposit(admin, pid, "100")
    assert r.status_code == 200, r.text
    assert await _cash(admin, pid) == {"EUR": D(600)}


async def test_withdraw_always_allowed(admin):
    admin.fake.t212.cash = D("0")
    pid = await _portfolio(admin)
    r = await _deposit(admin, pid, "50", path="withdraw")
    assert r.status_code == 200, r.text
    assert await _cash(admin, pid) == {"EUR": D(450)}


async def test_deposit_other_currency_at_current_rate_with_margin(admin):
    """A EUR portfolio on the USD account with 1000 USD: 1000 / 1.25 x 0.99 = 792 EUR."""
    admin.fake.alpaca.cash = D("1000")
    pid = await _portfolio(admin, kind="alpaca_live")
    _refused(await _deposit(admin, pid, "292.01"))
    r = await _deposit(admin, pid, "292")
    assert r.status_code == 200, r.text
    assert await _cash(admin, pid) == {"EUR": D(792)}


async def test_deposit_without_fx_rate_refused(admin):
    admin.fake.t212.cash = D("100000")
    pid = await _portfolio(admin, ccy="GBP")  # no GBP rate on a EUR account
    _refused(await _deposit(admin, pid, "1"),
             DEPOSIT_SHORT_MESSAGE + " (cambio non disponibile)")
    assert await _cash(admin, pid) == {"GBP": D(500)}


async def test_deposit_on_paper_portfolio_unchanged(admin):
    admin.fake.t212.cash = D("0")
    pid = await _portfolio(admin, kind=None)
    r = await _deposit(admin, pid, "100000")
    assert r.status_code == 200, r.text
    assert await _cash(admin, pid) == {"EUR": D(100500)}


def _unreadable(r: httpx.Response) -> None:
    assert r.status_code == 400, r.text
    assert (r.json()["code"], r.json()["message"]) == ("PROVIDER_UNAVAILABLE",
                                                       UNREADABLE_MESSAGE)


async def test_deposit_refused_when_account_unreadable(admin):
    pid = await _portfolio(admin)

    def failing(request):
        return httpx.Response(500, json={"message": "down"})

    admin.app.state.live_http_factory = lambda link_id: make_live_client(
        admin.sm, link_id, inner=httpx.MockTransport(failing))
    _unreadable(await _deposit(admin, pid, "1"))
    assert await _cash(admin, pid) == {"EUR": D(500)}


async def test_deposit_refused_on_another_account(admin):
    admin.fake.t212.cash = D("100000")
    pid = await _portfolio(admin)
    async with admin.sm() as s:
        link = await s.get(BrokerLink, (await s.get(Portfolio, pid)).broker_link_id)
        link.account_fingerprint = account_fingerprint("t212_live", "another-account")
        await s.commit()
    _unreadable(await _deposit(admin, pid, "1"))


async def test_deposit_refused_when_keys_cannot_be_decrypted(admin):
    admin.fake.t212.cash = D("100000")
    pid = await _portfolio(admin)
    async with admin.sm() as s:
        link = await s.get(BrokerLink, (await s.get(Portfolio, pid)).broker_link_id)
        link.config_encrypted = b"not a ciphertext"
        await s.commit()
    r = await _deposit(admin, pid, "1")
    _unreadable(r)
    assert "CANARY" not in r.text


async def test_link_changed_between_read_and_lock_refused(admin):
    pid = await _portfolio(admin)
    async with admin.sm() as s:
        portfolio = await s.get(Portfolio, pid)
        other = BrokerCash(uuid.uuid4(), D(100000), "EUR", FakeQuoteSource())
        with pytest.raises(SMTError) as e:
            await check_live_deposit(s, portfolio, other, D(1), "EUR")
        assert (e.value.code, e.value.message) == (ErrorCode.PROVIDER_UNAVAILABLE,
                                                   UNREADABLE_MESSAGE)
        with pytest.raises(SMTError) as e:
            await check_live_deposit(s, portfolio, None, D(1), "EUR")
        assert e.value.code == ErrorCode.PROVIDER_UNAVAILABLE


async def test_deposit_in_another_currency_with_margin(admin):
    """600 EUR at the broker, 500 in the portfolio: 125 USD = 100 EUR + 1 % = 101 is too
    much, 120 USD = 96 EUR + 1 % = 96.96 fits."""
    admin.fake.t212.cash = D("600")
    pid = await _portfolio(admin)

    async def usd(amount):
        return await admin.client.post(f"/api/portfolios/{pid}/deposit",
                                       json={"amount": amount, "currency": "USD"},
                                       headers={"Idempotency-Key": uuid.uuid4().hex})

    _refused(await usd("125"))
    r = await usd("120")
    assert r.status_code == 200, r.text
    assert await _cash(admin, pid) == {"EUR": D(500), "USD": D(120)}


async def test_reset_with_new_cash_needs_the_broker_cash(admin):
    admin.fake.t212.cash = D("600")
    pid = await _portfolio(admin)

    async def reset(new_cash):
        return await admin.client.post(
            f"/api/portfolios/{pid}/reset",
            json={"new_cash": new_cash, "currency": "EUR", "reason": "prova"},
            headers={"Idempotency-Key": uuid.uuid4().hex})

    r = await reset("700")
    assert r.status_code == 400, r.text
    assert (r.json()["code"], r.json()["details"]["field"], r.json()["message"]) == (
        "INVALID_ORDER", "new_cash", DEPOSIT_SHORT_MESSAGE)
    assert await _cash(admin, pid) == {"EUR": D(500)}
    r = await reset("600")  # the cash is replaced, not added to
    assert r.status_code == 200, r.text
    assert (await _cash(admin, pid))["EUR"] == D(600)
