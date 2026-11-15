"""devtools.live_e2e_seed: e2e only, refused unless the `live_fake_broker` setting is on."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pyotp
import pytest
from sqlalchemy import func, select

from smtrading.admin.auth import _totp_ok, _totp_secret, hash_password
from smtrading.admin.models import AdminUser
from smtrading.brokers.live.service import _telegram_ok, check_live_portfolio
from smtrading.common.config import get_settings
from smtrading.common.errors import ErrorCode, SMTError
from smtrading.devtools import live_e2e_seed
from smtrading.devtools.live_e2e_seed import (
    E2E_ETORO_PORTFOLIOS,
    E2E_PORTFOLIO_NAME,
    run_live_e2e_seed,
)
from smtrading.portfolios.models import Portfolio
from smtrading.portfolios.service import get_cash_balances
from smtrading.risk.models import RiskProfile
from smtrading.telegram.config import is_ready, load_config
from smtrading.telegram.models import TelegramOutbox

SECRET = pyotp.random_base32()
EMAIL = "e2e@example.com"


@pytest.fixture
def env(monkeypatch, migrated_db):
    monkeypatch.setenv("SMT_DATABASE_URL", migrated_db)
    monkeypatch.setenv("SMT_REDIS_URL", "redis://unused:6379/0")
    monkeypatch.setenv("SMT_MASTER_KEY", "test-master-key")
    monkeypatch.setenv("SMT_ADMIN_EMAIL", EMAIL)
    monkeypatch.setenv("SMT_ADMIN_PASSWORD", "test-password-e2e")
    get_settings.cache_clear()
    return monkeypatch


async def _admin(session) -> AdminUser:
    user = AdminUser(email=EMAIL, password_hash=await hash_password("test-password-e2e"))
    session.add(user)
    await session.flush()
    return user


async def test_refused_when_flag_off(env, db_session):
    env.delenv("SMT_LIVE_FAKE_BROKER", raising=False)
    get_settings.cache_clear()
    user = await _admin(db_session)
    with pytest.raises(SMTError) as e:
        await run_live_e2e_seed(db_session, totp_secret=SECRET)
    assert e.value.code == ErrorCode.FORBIDDEN
    assert "live_fake_broker" in e.value.message
    assert user.totp_enabled is False
    n = await db_session.scalar(select(func.count()).select_from(Portfolio).where(
        Portfolio.name == E2E_PORTFOLIO_NAME))
    assert n == 0


def test_main_exits_with_error_when_flag_off(env, capsys):
    env.delenv("SMT_LIVE_FAKE_BROKER", raising=False)
    env.setenv("SMT_E2E_TOTP_SECRET", SECRET)
    get_settings.cache_clear()
    with pytest.raises(SystemExit) as e:
        live_e2e_seed.main()
    assert e.value.code not in (0, None)
    err = capsys.readouterr().err
    assert "live_fake_broker" in err and SECRET not in err


def test_main_exits_with_error_without_secret(env, capsys):
    env.setenv("SMT_LIVE_FAKE_BROKER", "1")
    env.delenv("SMT_E2E_TOTP_SECRET", raising=False)
    get_settings.cache_clear()
    with pytest.raises(SystemExit) as e:
        live_e2e_seed.main()
    assert e.value.code not in (0, None)
    assert "SMT_E2E_TOTP_SECRET" in capsys.readouterr().err


async def test_seed_prepares_the_wizard(env, db_session):
    env.setenv("SMT_LIVE_FAKE_BROKER", "1")
    get_settings.cache_clear()
    user = await _admin(db_session)
    pf = await run_live_e2e_seed(db_session, totp_secret=SECRET)
    pf2 = await run_live_e2e_seed(db_session, totp_secret=SECRET)  # idempotent
    assert pf2.id == pf.id

    # TOTP on with the known secret
    await db_session.refresh(user)
    assert user.totp_enabled is True and _totp_secret(user) == SECRET
    assert _totp_ok(_totp_secret(user), pyotp.TOTP(SECRET).now())

    # Telegram «ready» with a recent sent test message (what arming checks)
    cfg = await load_config(db_session)
    assert is_ready(cfg) and not cfg.paused
    assert await _telegram_ok(db_session, datetime.now(UTC))
    tests = (await db_session.scalars(select(TelegramOutbox).where(
        TelegramOutbox.category == "test"))).all()
    assert len(tests) == 1 and tests[0].status == "sent"
    assert tests[0].sent_at > datetime.now(UTC) - timedelta(minutes=5)

    # an empty EUR portfolio, leverage 1, with a compliant risk profile
    assert pf.name == E2E_PORTFOLIO_NAME and pf.base_currency == "EUR"
    assert pf.leverage == 1 and pf.broker_link_id is None
    cash = await get_cash_balances(db_session, pf.id)
    assert cash == {"EUR": Decimal(500)}
    profile = await db_session.get(RiskProfile, pf.risk_profile_id)
    assert profile.short_allowed is False and profile.max_order_notional_eur
    assert profile.max_daily_loss_pct or profile.max_daily_loss_eur


async def test_seed_adds_the_etoro_portfolios(env, db_session):
    """Two more empty EUR portfolios (300 each, same compliant profile), created once."""
    env.setenv("SMT_LIVE_FAKE_BROKER", "1")
    get_settings.cache_clear()
    await _admin(db_session)
    pf = await run_live_e2e_seed(db_session, totp_secret=SECRET)
    await run_live_e2e_seed(db_session, totp_secret=SECRET)  # idempotent
    rows = (await db_session.scalars(select(Portfolio).where(
        Portfolio.name.in_(E2E_ETORO_PORTFOLIOS)))).all()
    assert sorted(p.name for p in rows) == sorted(E2E_ETORO_PORTFOLIOS)
    for p in rows:
        assert p.base_currency == "EUR" and p.leverage == 1 and p.broker_link_id is None
        assert p.risk_profile_id == pf.risk_profile_id
        assert await get_cash_balances(db_session, p.id) == {"EUR": Decimal(300)}


async def test_seeded_portfolio_passes_the_live_checks(env, db_session):
    """The portfolio the e2e links meets check_live_portfolio against the fake's account."""
    import uuid

    from smtrading.brokers.models import BrokerLink

    env.setenv("SMT_LIVE_FAKE_BROKER", "1")
    get_settings.cache_clear()
    await _admin(db_session)
    pf = await run_live_e2e_seed(db_session, totp_secret=SECRET)
    link = BrokerLink(id=uuid.uuid4(), name="e2e-check", kind="t212_live", config_encrypted=b"x",
                      live_state="dry_run",
                      last_account_snapshot={"currency": "EUR", "cash_available": "1000",
                                             "positions_count": 0, "open_orders_count": 0})
    await check_live_portfolio(db_session, pf, link)
