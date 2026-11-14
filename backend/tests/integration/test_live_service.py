"""The real-money activation flow (keys, read-only test, dry run, TOTP arm, one-click
disarm), the real portfolio's TOTP actions (link, resume, flatten) and its mode.

Never reaches the network: the broker is the in-memory FakeLiveBroker (behind the live
gate) or respx with assert_all_mocked/assert_all_called. Only canary keys."""

import json
import time
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx
import pyotp
import pytest
import respx
from redis.asyncio import Redis
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import async_sessionmaker

from smtrading.activity.models import Activity
from smtrading.admin.models import AdminUser
from smtrading.brokers.live import LIVE_KINDS
from smtrading.brokers.live.account import LIVE_AAD, register_live_secrets
from smtrading.brokers.live.alpaca_live import AlpacaLiveAdapter
from smtrading.brokers.live.fake import FakeLiveBroker
from smtrading.brokers.live.gate import LiveClient, make_live_client
from smtrading.brokers.live.models import LiveAudit
from smtrading.brokers.live.service import (
    Actor,
    arm_live_link,
    check_live_portfolio,
    disarm_live_link,
    flatten_live_portfolio,
    link_live_portfolio,
    resume_live_portfolio,
    save_live_keys,
    set_dry_run,
    test_live_link,
)
from smtrading.brokers.models import BrokerLink
from smtrading.brokers.service import create_link, get_adapter, link_portfolio, update_link
from smtrading.common import logsetup
from smtrading.common.config import get_settings
from smtrading.common.crypto import decrypt_json, encrypt_json
from smtrading.common.db import make_engine
from smtrading.common.errors import ErrorCode, SMTError
from smtrading.common.logsetup import redact_log_text
from smtrading.engine.models import BrokerState, Order, OrderSide, OrderStatus, OrderType
from smtrading.engine.quotesource import FakeQuoteSource
from smtrading.engine.service import PlaceOrderRequest, place_order
from smtrading.portfolios.mode import (
    PortfolioMode,
    live_state_of,
    load_portfolio_mode,
    portfolio_mode,
)
from smtrading.portfolios.models import Portfolio, PortfolioStatus
from smtrading.portfolios.service import create_portfolio
from smtrading.risk.models import RiskProfile
from smtrading.seed import run_seed
from smtrading.telegram.config import TelegramConfig, save_config
from smtrading.telegram.models import TelegramOutbox

D = Decimal
KEY_ID = "CANARYKEYID0001"
SECRET = "CANARYSECRET-do-not-use"
ALPACA = {"key_id": KEY_ID, "secret_key": SECRET}
T212 = {"api_key": KEY_ID, "api_secret": SECRET}
CANARIES = (KEY_ID, SECRET)
OPEN = datetime(2026, 11, 10, 15, 0, tzinfo=UTC)  # Tuesday: NASDAQ open
BAD = "000000"  # an invalid code (checks before the TOTP step)
WHO = Actor("admin@example.com", "10.0.0.1", "pytest")


@pytest.fixture(autouse=True)
def _env(monkeypatch, pg_url, redis_url):
    monkeypatch.setenv("SMT_DATABASE_URL", pg_url)
    monkeypatch.setenv("SMT_REDIS_URL", redis_url)
    monkeypatch.setenv("SMT_MASTER_KEY", "test-master-key")
    monkeypatch.setenv("SMT_ADMIN_EMAIL", "admin@example.com")
    monkeypatch.setenv("SMT_ADMIN_PASSWORD", "test-password")
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _registered_secrets():
    saved = set(logsetup._registered)
    logsetup._registered.clear()
    yield
    logsetup._registered.clear()
    logsetup._registered.update(saved)


@pytest.fixture
async def sm(migrated_db):
    """Sessions inside one rolled-back transaction (the gate's short sessions and the
    service's own commits see the test's rows; nothing is committed for real)."""
    engine = make_engine(migrated_db)
    async with engine.connect() as conn:
        trans = await conn.begin()
        maker = async_sessionmaker(bind=conn, expire_on_commit=False,
                                   join_transaction_mode="create_savepoint")
        async with maker() as s:
            await run_seed(s)
            await s.commit()
        try:
            yield maker
        finally:
            await trans.rollback()
    await engine.dispose()


@pytest.fixture
async def redis(redis_url):
    r = Redis.from_url(redis_url)
    yield r
    await r.aclose()


class Fake(FakeLiveBroker):
    """FakeLiveBroker whose Alpaca account ID can change, counting the non-GET requests."""

    def __init__(self) -> None:
        super().__init__()
        self.account_id: str | None = None
        self.writes: list[str] = []

    def _handle(self, request: httpx.Request) -> httpx.Response:
        if request.method.upper() not in ("GET", "HEAD"):
            self.writes.append(f"{request.method} {request.url.path}")
        return super()._handle(request)

    def _alpaca(self, method, path, params, body):
        resp = super()._alpaca(method, path, params, body)
        if self.account_id and (method, path) == ("GET", "/v2/account"):
            data = resp.json()
            data["id"] = self.account_id
            return httpx.Response(200, json=data)
        return resp


@pytest.fixture
def fake():
    return Fake()


def _client(sm, link_id, fake) -> LiveClient:
    return make_live_client(sm, link_id, inner=fake.transport())


class Totp:
    """Distinct valid codes (each is accepted once: replay guard)."""

    def __init__(self, secret: str) -> None:
        self.totp = pyotp.TOTP(secret)
        self.used: set[str] = set()

    def code(self) -> str:
        now = time.time()
        for t in (now + 30, now, now - 30):
            c = self.totp.at(t)
            if c not in self.used:
                self.used.add(c)
                return c
        raise AssertionError("no unused TOTP code left in the window")


async def _admin(sm, *, totp: bool = True) -> tuple[AdminUser, Totp]:
    secret = pyotp.random_base32()
    async with sm() as s:
        user = AdminUser(
            email=f"a-{uuid.uuid4().hex[:6]}@example.com", password_hash="x",
            totp_secret_encrypted=encrypt_json({"secret": secret}, get_settings().master_key),
            totp_enabled=totp,
        )
        s.add(user)
        await s.commit()
        return user, Totp(secret)


async def _telegram(sm, *, sent_ago: timedelta | None = timedelta(hours=1),
                    paused: bool = False) -> None:
    async with sm() as s:
        await save_config(s, TelegramConfig(bot_token="123456789:AAH" + "x" * 32,
                                            chat_id="-100123", enabled=True, paused=paused))
        if sent_ago is not None:
            s.add(TelegramOutbox(source="system", category="test", level="info", text="prova",
                                 status="sent", sent_at=datetime.now(UTC) - sent_ago))
        await s.commit()


async def _keys(sm, kind="alpaca_live", config=None, name=None) -> uuid.UUID:
    async with sm() as s:
        link = await save_live_keys(s, name=name or f"Reale-{uuid.uuid4().hex[:6]}",
                                    kind=kind, config=dict(config or ALPACA), actor=WHO)
        await s.commit()
        return link.id


async def _tested(sm, fake, kind="alpaca_live", config=None) -> uuid.UUID:
    link_id = await _keys(sm, kind, config)
    async with _client(sm, link_id, fake) as http:
        await test_live_link(sm, link_id, actor=WHO, http=http)
    return link_id


async def _profile(sm, **kw) -> uuid.UUID:
    values = {"max_order_notional_eur": D(500), "max_daily_loss_pct": D(5),
              "short_allowed": False}
    values.update(kw)
    async with sm() as s:
        p = RiskProfile(name=f"Live-{uuid.uuid4().hex[:6]}", **values)
        s.add(p)
        await s.commit()
        return p.id


async def _portfolio(sm, *, cash=500, ccy="EUR", leverage=1, profile=None) -> uuid.UUID:
    profile = profile or await _profile(sm)
    async with sm() as s:
        p = await create_portfolio(s, name=f"P-{uuid.uuid4().hex[:6]}", initial_cash=D(cash),
                                   base_currency=ccy, leverage=D(leverage),
                                   risk_profile_id=profile)
        await s.commit()
        return p.id


def _qs() -> FakeQuoteSource:
    q = FakeQuoteSource()
    q.set_fx("EUR", "USD", D("1.25"))
    q.set_quote("AAPL", D("99.90"), D("100.00"), D("99.95"))
    return q


async def _link(sm, link_id) -> BrokerLink:
    async with sm() as s:
        return await s.get(BrokerLink, link_id)


async def _audit(sm, link_id) -> list[LiveAudit]:
    async with sm() as s:
        return list(await s.scalars(
            select(LiveAudit).where(LiveAudit.link_id == link_id).order_by(LiveAudit.id)))


async def _arm(sm, redis, link_id, admin, codes, **kw):
    link = await _link(sm, link_id)
    args = {"confirm_name": link.name, "accept_risks": True,
            "dedicated_account": True, "limits_set": True, "actor": WHO, "qs": _qs()}
    args.update(kw)
    if "totp" not in args:
        args["totp"] = codes.code()
    async with sm() as s:
        user = await s.get(AdminUser, admin.id)
        out = await arm_live_link(s, redis, link_id, admin=user, **args)
        await s.commit()
        return out


async def _expect(coro, code: ErrorCode, field: str | None = None) -> SMTError:
    with pytest.raises(SMTError) as e:
        await coro
    assert e.value.code == code, e.value.message
    if field is not None:
        assert e.value.details.get("field") == field, (e.value.details, e.value.message)
    return e.value


async def _armed_with_portfolio(sm, redis, fake, *, totp_admin=None):
    admin, totp = totp_admin or await _admin(sm)
    await _telegram(sm)
    link_id = await _tested(sm, fake)
    async with sm() as s:
        await set_dry_run(s, link_id, True, actor=WHO)
        await s.commit()
    pid = await _portfolio(sm)
    async with sm() as s:
        await link_live_portfolio(s, redis, pid, link_id, admin=await s.get(AdminUser, admin.id),
                                  totp=totp.code(), actor=WHO, qs=_qs())
        await s.commit()
    await _arm(sm, redis, link_id, admin, totp)
    return link_id, pid, admin, totp


# -- keys -------------------------------------------------------------------------------

async def test_keys_saved_encrypted_with_aad_and_never_returned(sm):
    link_id = await _keys(sm)
    link = await _link(sm, link_id)
    assert link.kind == "alpaca_live" and link.live_state == "configured"
    master = get_settings().master_key
    with pytest.raises(SMTError) as e:
        decrypt_json(link.config_encrypted, master)  # no context: refused
    assert e.value.code == ErrorCode.FORBIDDEN
    assert decrypt_json(link.config_encrypted, master, aad=LIVE_AAD) == ALPACA
    for canary in CANARIES:
        assert canary.encode() not in link.config_encrypted
    (row,) = await _audit(sm, link_id)
    assert row.action == "keys_saved" and row.actor == WHO.email
    assert row.ip == WHO.ip and row.user_agent == WHO.user_agent
    assert row.details["key_masked"] == "••••0001"
    assert not any(c in json.dumps(row.details) for c in CANARIES)
    # registered for log masking on save ...
    assert KEY_ID not in redact_log_text(f"key {KEY_ID} secret {SECRET}")
    assert SECRET not in redact_log_text(f"key {KEY_ID} secret {SECRET}")
    # ... and on every load path: a process that only reads the link (start, adapter)
    for load in ("start", "adapter"):
        logsetup._registered.clear()
        async with sm() as s:
            if load == "start":
                assert await register_live_secrets(s) >= 1
            else:
                adapter = await get_adapter(s, link_id, None)
                assert isinstance(adapter, AlpacaLiveAdapter)
        assert SECRET not in redact_log_text(SECRET), load


async def test_t212_basic_credential_registered(sm):
    import base64

    await _keys(sm, "t212_live", T212)
    cred = base64.b64encode(f"{KEY_ID}:{SECRET}".encode()).decode()
    assert cred not in redact_log_text(f"Authorization: X {cred}")


async def test_keys_validation(sm):
    async with sm() as s:
        await _expect(save_live_keys(s, name="X", kind="alpaca_paper", config=ALPACA,
                                     actor=WHO), ErrorCode.INVALID_ORDER, "kind")
        await _expect(save_live_keys(s, name="X", kind="alpaca_live",
                                     config={"key_id": KEY_ID}, actor=WHO),
                      ErrorCode.INVALID_ORDER, "secret_key")
        await _expect(save_live_keys(s, name="X", kind="alpaca_live",
                                     config={**ALPACA, "base_url": "https://x.example"},
                                     actor=WHO), ErrorCode.FORBIDDEN, "base_url")
        await _expect(save_live_keys(s, name="X", kind="t212_live",
                                     config={**T212, "order_types": ["bogus"]}, actor=WHO),
                      ErrorCode.INVALID_ORDER)


async def test_paper_crud_refuses_live_kinds(sm):
    link_id = await _keys(sm)
    async with sm() as s:
        e = await _expect(create_link(s, "X", "alpaca_live", ALPACA), ErrorCode.INVALID_ORDER)
        assert e.message == "usa la procedura «denaro reale»"
        await _expect(update_link(s, link_id, config=ALPACA), ErrorCode.INVALID_ORDER)
    pid = await _portfolio(sm)
    async with sm() as s:  # linking goes through link_live_portfolio (TOTP)
        await _expect(link_portfolio(s, pid, link_id), ErrorCode.FORBIDDEN, "link_id")


# -- read-only test --------------------------------------------------------------------

async def test_test_link_is_get_only(sm):
    link_id = await _keys(sm)
    base = AlpacaLiveAdapter.BASE_URL
    with respx.mock(assert_all_mocked=True, assert_all_called=True) as mock:
        mock.get(f"{base}/v2/account").respond(200, json={
            "id": "acct-123456789", "currency": "USD", "cash": "1500.00",
            "buying_power": "1500.00", "multiplier": "1", "crypto_status": "INACTIVE"})
        mock.get(f"{base}/v2/positions").respond(200, json=[])
        mock.get(f"{base}/v2/orders").respond(200, json=[])
        result = await test_live_link(sm, link_id, actor=WHO)  # its own gated client
    assert all(c.request.method == "GET" for c in mock.calls)
    assert result["account_masked"] == "••••6789" and result["empty"] is True
    assert result["currency"] == "USD" and result["cash_available"] == "1500.00"
    assert result["positions_count"] == 0 and result["open_orders_count"] == 0
    assert result["crypto_enabled"] is False and result["account_type"] == "cash"
    assert "acct-123456789" not in json.dumps(result)
    link = await _link(sm, link_id)
    assert link.last_test_at is not None and link.account_fingerprint
    assert "raw" not in link.last_account_snapshot
    assert "acct-123456789" not in json.dumps(link.last_account_snapshot)
    assert [a.action for a in await _audit(sm, link_id)] == ["keys_saved", "test_ok"]


async def test_failed_test_is_recorded_with_keys_masked(sm):
    link_id = await _keys(sm)
    with respx.mock(assert_all_mocked=True, assert_all_called=True) as mock:
        mock.get(f"{AlpacaLiveAdapter.BASE_URL}/v2/account").respond(
            401, json={"message": f"bad key {KEY_ID}"})
        e = await _expect(test_live_link(sm, link_id, actor=WHO),
                          ErrorCode.PROVIDER_UNAVAILABLE)
    assert KEY_ID not in e.message
    row = (await _audit(sm, link_id))[-1]
    assert row.action == "test_failed" and KEY_ID not in json.dumps(row.details)
    assert (await _link(sm, link_id)).last_test_at is None


async def test_account_not_empty_refused(sm, redis, fake):
    admin, totp = await _admin(sm)
    await _telegram(sm)
    fake.alpaca.positions["AAPL"] = D(1)
    link_id = await _tested(sm, fake)
    assert (await _link(sm, link_id)).last_account_snapshot["positions_count"] == 1
    async with sm() as s:
        e = await _expect(set_dry_run(s, link_id, True, actor=WHO), ErrorCode.INVALID_ORDER,
                          "test")
    assert e.message == "Il conto non è dedicato: chiudi posizioni e ordini dall'app del broker"
    await _expect(_arm(sm, redis, link_id, admin, totp, totp=BAD), ErrorCode.INVALID_ORDER, "test")


# -- arming ----------------------------------------------------------------------------

async def test_arm_requires_totp_enabled(sm, redis, fake):
    admin, totp = await _admin(sm, totp=False)
    await _telegram(sm)
    link_id = await _tested(sm, fake)
    e = await _expect(_arm(sm, redis, link_id, admin, totp, totp=BAD), ErrorCode.FORBIDDEN, "totp")
    assert e.message == "Attiva prima l'autenticazione a due fattori"
    assert (await _link(sm, link_id)).live_state == "configured"


async def test_arm_requires_checkboxes_and_name(sm, redis, fake):
    admin, totp = await _admin(sm)
    await _telegram(sm)
    link_id = await _tested(sm, fake)
    for flag in ("accept_risks", "dedicated_account", "limits_set"):
        await _expect(_arm(sm, redis, link_id, admin, totp, totp=BAD, **{flag: False}),
                      ErrorCode.INVALID_ORDER, "confirm")
    await _expect(_arm(sm, redis, link_id, admin, totp, totp=BAD, confirm_name="altro"),
                  ErrorCode.INVALID_ORDER, "confirm_name")
    assert (await _link(sm, link_id)).live_state == "configured"


async def test_arm_requires_fresh_empty_test(sm, redis, fake):
    admin, totp = await _admin(sm)
    await _telegram(sm)
    link_id = await _keys(sm)
    await _expect(_arm(sm, redis, link_id, admin, totp, totp=BAD), ErrorCode.INVALID_ORDER, "test")
    async with _client(sm, link_id, fake) as http:
        await test_live_link(sm, link_id, actor=WHO, http=http)
    async with sm() as s:  # 16 minutes old: stale
        await s.execute(update(BrokerLink).where(BrokerLink.id == link_id).values(
            last_test_at=datetime.now(UTC) - timedelta(seconds=960)))
        await s.commit()
    await _expect(_arm(sm, redis, link_id, admin, totp, totp=BAD), ErrorCode.INVALID_ORDER, "test")


async def test_arm_requires_telegram_test_last_24h(sm, redis, fake):
    admin, totp = await _admin(sm)
    link_id = await _tested(sm, fake)
    await _expect(_arm(sm, redis, link_id, admin, totp, totp=BAD), ErrorCode.INVALID_ORDER, "telegram")
    await _telegram(sm, sent_ago=timedelta(hours=25))
    await _expect(_arm(sm, redis, link_id, admin, totp, totp=BAD), ErrorCode.INVALID_ORDER, "telegram")
    await _telegram(sm, paused=True)
    await _expect(_arm(sm, redis, link_id, admin, totp, totp=BAD), ErrorCode.INVALID_ORDER, "telegram")
    await _telegram(sm)
    assert (await _arm(sm, redis, link_id, admin, totp)).live_state == "armed"


async def test_arm_totp_replay_rejected(sm, redis, fake):
    admin, totp = await _admin(sm)
    await _telegram(sm)
    link_id = await _tested(sm, fake)
    await _expect(_arm(sm, redis, link_id, admin, totp, totp=BAD), ErrorCode.FORBIDDEN,
                  "totp")
    code = totp.code()
    await _arm(sm, redis, link_id, admin, totp, totp=code)
    async with sm() as s:
        await disarm_live_link(s, link_id, actor=WHO, reason="prova")
        await s.commit()
    e = await _expect(_arm(sm, redis, link_id, admin, totp, totp=code), ErrorCode.FORBIDDEN,
                      "totp")
    assert e.message == "codice TOTP già usato"


async def test_arm_ok_audited_and_alerted(sm, redis, fake):
    link_id, pid, admin, _ = await _armed_with_portfolio(sm, redis, fake)
    link = await _link(sm, link_id)
    assert link.live_state == "armed" and link.armed_by == admin.email
    assert link.armed_at is not None
    actions = [a.action for a in await _audit(sm, link_id)]
    assert actions == ["keys_saved", "test_ok", "dry_run_on", "portfolio_linked", "armed"]
    armed = (await _audit(sm, link_id))[-1]
    assert armed.portfolio_id == pid and armed.actor == WHO.email
    async with sm() as s:
        alert = await s.scalar(select(Activity).where(Activity.type == "live.armed",
                                                      Activity.portfolio_id == pid))
    assert alert is not None and alert.level == "critical"
    async with sm() as s:
        assert await load_portfolio_mode(s, await s.get(Portfolio, pid)) == PortfolioMode.reale


async def test_arm_checks_the_linked_portfolio(sm, redis, fake):
    admin, totp = await _admin(sm)
    await _telegram(sm)
    link_id = await _tested(sm, fake)
    async with sm() as s:
        await set_dry_run(s, link_id, True, actor=WHO)
        await s.commit()
    pid = await _portfolio(sm)
    async with sm() as s:
        await link_live_portfolio(s, redis, pid, link_id,
                                  admin=await s.get(AdminUser, admin.id), totp=totp.code(),
                                  actor=WHO, qs=_qs())
        await s.commit()
    async with sm() as s:  # the profile loses its order limit after linking
        p = await s.get(Portfolio, pid)
        await s.execute(update(RiskProfile).where(RiskProfile.id == p.risk_profile_id)
                        .values(max_order_notional_eur=None))
        await s.commit()
    await _expect(_arm(sm, redis, link_id, admin, totp, totp=BAD), ErrorCode.INVALID_ORDER,
                  "risk_profile")


async def test_replacing_keys_returns_configured_and_needs_new_totp(sm, redis, fake):
    link_id, _, admin, totp = await _armed_with_portfolio(sm, redis, fake)
    link = await _link(sm, link_id)
    async with sm() as s:
        await save_live_keys(s, name=link.name, kind="alpaca_live",
                             config={"key_id": "CANARYKEYID0002", "secret_key": SECRET},
                             actor=WHO, link_id=link_id)
        await s.commit()
    link = await _link(sm, link_id)
    assert link.live_state == "configured" and link.armed_at is None
    assert link.last_test_at is None and link.disarmed_reason == "chiavi sostituite"
    actions = [a.action for a in await _audit(sm, link_id)]
    assert actions[-2:] == ["disarmed", "keys_saved"]
    await _expect(_arm(sm, redis, link_id, admin, totp, totp=BAD), ErrorCode.INVALID_ORDER, "test")
    async with _client(sm, link_id, fake) as http:
        await test_live_link(sm, link_id, actor=WHO, http=http)
    await _expect(_arm(sm, redis, link_id, admin, totp, totp=BAD), ErrorCode.FORBIDDEN,
                  "totp")
    assert (await _arm(sm, redis, link_id, admin, totp)).live_state == "armed"


async def test_fingerprint_change_disarms_and_reduce_only(sm, redis, fake):
    link_id, pid, admin, totp = await _armed_with_portfolio(sm, redis, fake)
    before = (await _link(sm, link_id)).account_fingerprint
    fake.account_id = "another-account-9999"
    async with _client(sm, link_id, fake) as http:
        result = await test_live_link(sm, link_id, actor=WHO, http=http)
    assert result["account_changed"] is True and result["account_masked"] == "••••9999"
    link = await _link(sm, link_id)
    assert link.live_state == "configured" and link.disarmed_reason == "conto cambiato"
    assert link.account_fingerprint != before and link.last_test_at is None
    async with sm() as s:
        p = await s.get(Portfolio, pid)
        assert p.status == PortfolioStatus.reduce_only
        assert p.reduce_only_reason == "live_fingerprint_changed"
        kinds = set(await s.scalars(select(Activity.type).where(Activity.portfolio_id == pid)))
    assert {"live.account_changed", "live.reduce_only", "live.disarmed"} <= kinds
    actions = [a.action for a in await _audit(sm, link_id)]
    assert {"disarmed", "reduce_only_auto"} <= set(actions[-4:])
    # arming again: a new test, then a new TOTP
    await _expect(_arm(sm, redis, link_id, admin, totp, totp=BAD), ErrorCode.INVALID_ORDER, "test")
    async with _client(sm, link_id, fake) as http:
        assert (await test_live_link(sm, link_id, actor=WHO, http=http))["account_changed"] \
            is False
    assert (await _arm(sm, redis, link_id, admin, totp)).live_state == "armed"


# -- the real portfolio ----------------------------------------------------------------

async def test_link_checks_profile_short_leverage_currency_cash_one_to_one(sm, redis, fake):
    admin, totp = await _admin(sm)
    link_id = await _tested(sm, fake)  # Alpaca fake: 1000.00 USD
    async with sm() as s:
        await set_dry_run(s, link_id, True, actor=WHO)
        await s.commit()
    qs = _qs()  # 1 EUR = 1.25 USD

    async def check(pid, field):
        async with sm() as s:
            await _expect(check_live_portfolio(s, await s.get(Portfolio, pid),
                                               await s.get(BrokerLink, link_id), qs=qs),
                          ErrorCode.INVALID_ORDER, field)

    await check(await _portfolio(sm, profile=await _profile(sm, max_order_notional_eur=None)),
                "risk_profile")
    await check(await _portfolio(sm, profile=await _profile(
        sm, max_daily_loss_pct=None, max_daily_loss_eur=None)), "risk_profile")
    await check(await _portfolio(sm, profile=await _profile(sm, short_allowed=True)),
                "short_allowed")
    await check(await _portfolio(sm, leverage=2), "leverage")
    await check(await _portfolio(sm, cash=801), "cash")  # 801 EUR = 1001.25 USD
    ok = await _portfolio(sm, cash=800)  # 1000.00 USD: allowed
    async with sm() as s:
        await check_live_portfolio(s, await s.get(Portfolio, ok),
                                   await s.get(BrokerLink, link_id), qs=qs)
        await link_live_portfolio(s, redis, ok, link_id, admin=await s.get(AdminUser, admin.id),
                                  totp=totp.code(), actor=WHO, qs=qs)
        await s.commit()
    await check(await _portfolio(sm), "link_id")  # 1 live link <-> 1 portfolio

    t212 = await _tested(sm, fake, "t212_live", T212)  # Trading 212 fake: EUR
    async with sm() as s:
        await _expect(check_live_portfolio(s, await s.get(Portfolio, await _portfolio(
            sm, ccy="USD")), await s.get(BrokerLink, t212), qs=qs),
            ErrorCode.INVALID_ORDER, "base_currency")


async def test_link_allowed_in_dry_run(sm, redis, fake):
    admin, totp = await _admin(sm)
    link_id = await _tested(sm, fake)
    pid = await _portfolio(sm)
    async with sm() as s:  # configured: not yet
        await _expect(link_live_portfolio(s, redis, pid, link_id,
                                          admin=await s.get(AdminUser, admin.id),
                                          totp=totp.code(), actor=WHO, qs=_qs()),
                      ErrorCode.INVALID_ORDER, "live_state")
    async with sm() as s:
        await set_dry_run(s, link_id, True, actor=WHO)
        await s.commit()
    async with sm() as s:
        await _expect(link_live_portfolio(s, redis, pid, link_id,
                                          admin=await s.get(AdminUser, admin.id),
                                          totp=BAD, actor=WHO, qs=_qs()),
                      ErrorCode.FORBIDDEN, "totp")
    async with sm() as s:
        pf = await link_live_portfolio(s, redis, pid, link_id,
                                       admin=await s.get(AdminUser, admin.id),
                                       totp=totp.code(), actor=WHO, qs=_qs())
        await s.commit()
    assert pf.broker_link_id == link_id
    row = (await _audit(sm, link_id))[-1]
    assert row.action == "portfolio_linked" and row.portfolio_id == pid
    async with sm() as s:  # dry run sends nothing: not "reale"
        p = await s.get(Portfolio, pid)
        assert await load_portfolio_mode(s, p) == PortfolioMode.paper
        assert live_state_of(await s.get(BrokerLink, link_id)) == "dry_run"


async def test_unlink_live_refused_with_positions_and_audited(sm, redis, fake):
    link_id, pid, _, _ = await _armed_with_portfolio(sm, redis, fake)
    async with sm() as s:  # a real position (as the broker reports fills)
        await s.execute(update(Portfolio).where(Portfolio.id == pid)
                        .values(broker_link_id=None))
        await place_order(s, _qs(), PlaceOrderRequest(
            portfolio_id=pid, symbol="AAPL", side=OrderSide.buy, type=OrderType.market,
            qty=D(1)), now=OPEN)
        await s.execute(update(Portfolio).where(Portfolio.id == pid)
                        .values(broker_link_id=link_id))
        await s.commit()
    async with sm() as s:
        e = await _expect(link_portfolio(s, pid, None, actor=WHO), ErrorCode.INVALID_ORDER,
                          "positions")
    assert e.message == "Chiudi prima le posizioni reali"
    link2 = await _armed_with_portfolio(sm, redis, Fake())
    async with sm() as s:  # a flat live portfolio can be unlinked
        await link_portfolio(s, link2[1], None, actor=WHO)
        await s.commit()
    row = (await _audit(sm, link2[0]))[-1]
    assert row.action == "portfolio_unlinked" and row.portfolio_id == link2[1]
    assert row.actor == WHO.email


async def _order(s, pid, link_id, **kw) -> uuid.UUID:
    o = Order(portfolio_id=pid, symbol="AAPL", side=OrderSide.buy, type=OrderType.limit,
              qty=D(1), limit_price=D(90), status=OrderStatus.pending,
              broker_link_id=link_id, **kw)
    s.add(o)
    await s.flush()
    return o.id


async def test_disarm_one_click_cancels_sent_rejects_unsent(sm, redis, fake):
    link_id, pid, _, _ = await _armed_with_portfolio(sm, redis, fake)
    async with sm() as s:
        sent = await _order(s, pid, link_id, broker_status="new", broker_order_id="b-1",
                            broker_sent_at=datetime.now(UTC))
        unsent = await _order(s, pid, link_id, broker_status=BrokerState.TO_SEND)
        await s.commit()
    async with sm() as s:  # no TOTP, no admin: one click
        assert await disarm_live_link(s, link_id, actor=WHO, reason="disattivato") == 2
        await s.commit()
    async with sm() as s:
        a, b = await s.get(Order, sent), await s.get(Order, unsent)
    assert a.status == OrderStatus.pending and a.broker_status == BrokerState.CANCEL_REQUESTED
    assert b.status == OrderStatus.rejected and b.reject_code == "LIVE_NOT_ARMED"
    assert b.reject_message == "denaro reale disattivato"
    link = await _link(sm, link_id)
    assert link.live_state == "configured" and link.disarmed_reason == "disattivato"
    assert link.disarmed_at is not None
    assert fake.writes == []  # nothing was sent by the disarm itself
    row = (await _audit(sm, link_id))[-1]
    assert row.action == "disarmed" and row.details["orders"] == 2
    async with sm() as s:
        assert await s.scalar(select(Activity.id).where(
            Activity.type == "live.disarmed", Activity.portfolio_id == pid)) is not None
        assert await load_portfolio_mode(s, await s.get(Portfolio, pid)) == PortfolioMode.paper


async def test_resume_requires_totp_and_live_reason(sm, redis, fake):
    link_id, pid, admin, totp = await _armed_with_portfolio(sm, redis, fake)

    async def resume(code):
        async with sm() as s:
            out = await resume_live_portfolio(s, redis, pid,
                                              admin=await s.get(AdminUser, admin.id),
                                              totp=code, actor=WHO)
            await s.commit()
            return out

    await _expect(resume(BAD), ErrorCode.INVALID_ORDER, "status")  # active
    async with sm() as s:
        p = await s.get(Portfolio, pid)
        p.status, p.reduce_only_reason = PortfolioStatus.reduce_only, "daily_loss"
        await s.commit()
    await _expect(resume(BAD), ErrorCode.INVALID_ORDER, "status")  # not live
    async with sm() as s:
        await s.execute(update(Portfolio).where(Portfolio.id == pid)
                        .values(reduce_only_reason="live_fuse"))
        await s.commit()
    await _expect(resume("000000"), ErrorCode.FORBIDDEN, "totp")
    p = await resume(totp.code())
    assert p.status == PortfolioStatus.active and p.reduce_only_reason is None
    row = (await _audit(sm, link_id))[-1]
    assert row.action == "resumed" and row.details["reason"] == "live_fuse"
    no_totp, _ = await _admin(sm, totp=False)
    async with sm() as s:
        await _expect(resume_live_portfolio(s, redis, pid,
                                            admin=await s.get(AdminUser, no_totp.id),
                                            totp="1", actor=WHO), ErrorCode.FORBIDDEN, "totp")


async def test_flatten_creates_reduce_only_market_flagged_orders(sm, redis, fake):
    link_id, pid, admin, totp = await _armed_with_portfolio(sm, redis, fake)
    qs = _qs()
    async with sm() as s:  # a 2-share position (filled while unlinked)
        await s.execute(update(Portfolio).where(Portfolio.id == pid)
                        .values(broker_link_id=None))
        await place_order(s, qs, PlaceOrderRequest(
            portfolio_id=pid, symbol="AAPL", side=OrderSide.buy, type=OrderType.market,
            qty=D(2)), now=OPEN)
        await s.execute(update(Portfolio).where(Portfolio.id == pid)
                        .values(broker_link_id=link_id))
        await s.commit()
    async with sm() as s:
        await _expect(flatten_live_portfolio(s, redis, qs, pid,
                                             admin=await s.get(AdminUser, admin.id),
                                             totp=BAD, actor=WHO, now=OPEN),
                      ErrorCode.FORBIDDEN, "totp")
    async with sm() as s:
        ids = await flatten_live_portfolio(s, redis, qs, pid,
                                           admin=await s.get(AdminUser, admin.id),
                                           totp=totp.code(), actor=WHO, now=OPEN)
        await s.commit()
    assert len(ids) == 1
    async with sm() as s:
        o = await s.get(Order, ids[0])
    assert (o.side, o.type, o.qty) == (OrderSide.sell, OrderType.market, D(2))
    assert o.reduce_only and o.live_flatten and o.source == "admin"
    assert o.broker_link_id == link_id and o.broker_status == BrokerState.TO_SEND
    assert fake.writes == []  # BrokerSync sends it, not the flatten
    row = (await _audit(sm, link_id))[-1]
    assert row.action == "flatten" and row.details["orders"] == [str(ids[0])]
    async with sm() as s:
        assert await s.scalar(select(Activity.id).where(
            Activity.type == "live.flatten", Activity.portfolio_id == pid)) is not None


def test_mode_reale_for_live_kinds():
    linked = Portfolio(name="x", broker_link_id=uuid.uuid4())
    assert portfolio_mode(Portfolio(name="x")) == PortfolioMode.simulazione
    assert portfolio_mode(linked) == PortfolioMode.paper
    assert portfolio_mode(linked, "alpaca_paper", None) == PortfolioMode.paper
    for kind in LIVE_KINDS:
        assert portfolio_mode(linked, kind, "armed") == PortfolioMode.reale
        for state in ("configured", "dry_run", None):
            assert portfolio_mode(linked, kind, state) == PortfolioMode.paper
    assert live_state_of(None) is None
    assert live_state_of(BrokerLink(kind="t212_demo", live_state=None)) is None
    assert live_state_of(BrokerLink(kind="t212_live", live_state="dry_run")) == "dry_run"
