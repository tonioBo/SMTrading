"""Seed for the «denaro reale» e2e (web/e2e/live.spec.ts), on the isolated e2e stack only.

    SMT_E2E_TOTP_SECRET=<base32> python -m smtrading.devtools.live_e2e_seed

Refused (exit code 2, nothing written) unless the `live_fake_broker` setting is on
(`SMT_LIVE_FAKE_BROKER=1`, set by scripts/e2e.sh in the e2e stack only). Otherwise it:
- enables the TOTP of the admin `SMT_ADMIN_EMAIL` with the given test secret (known to the
  Playwright spec, which generates the codes);
- stores a Telegram configuration that is «ready» (a fake token and group; the e2e stack
  sends nothing: api.telegram.org resolves to nowhere there) and a `test` outbox row
  marked `sent` now, as arming requires;
- creates an empty EUR portfolio (500 € of cash, leverage 1) with a compliant risk profile
  (limit per order, daily loss limit, no short), and two more (300 € each, same profile)
  for the eToro procedure with several portfolios on one account.

Idempotent. It never creates a live link, never stores live keys and never talks to a
broker: the spec does all of that through the UI against the in-memory FakeLiveBroker.
"""

import asyncio
import os
import sys
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pyotp
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

import smtrading.models  # noqa: F401  (complete mapper registry)
from smtrading.admin.auth import normalize_email
from smtrading.admin.models import AdminUser
from smtrading.common.config import get_settings
from smtrading.common.crypto import encrypt_json
from smtrading.common.db import get_sessionmaker
from smtrading.common.errors import ErrorCode, SMTError
from smtrading.portfolios.models import Portfolio
from smtrading.portfolios.service import create_portfolio
from smtrading.risk.models import RiskProfile
from smtrading.telegram.config import TelegramConfig, load_config, save_config
from smtrading.telegram.models import OutboxStatus, TelegramOutbox

TOTP_ENV = "SMT_E2E_TOTP_SECRET"
E2E_PORTFOLIO_NAME = "Reale E2E"
E2E_PROFILE_NAME = "Reale E2E · a norma"
E2E_CASH = Decimal(500)  # below the fake broker's 1000 EUR
# eToro: two portfolios on the fake's 1000 USD account (quotas 2 x 300 EUR, the rest unassigned)
E2E_ETORO_PORTFOLIOS = ("Reale E2E eToro A", "Reale E2E eToro B")
E2E_ETORO_CASH = Decimal(300)
# not a real bot: the e2e stack cannot reach Telegram
FAKE_BOT_TOKEN = "123456789:E2E-fake-bot-token-not-real"
FAKE_CHAT_ID = "-1000000000001"
REFUSED_MESSAGE = ("seed e2e rifiutato: richiede live_fake_broker attivo "
                   "(SMT_LIVE_FAKE_BROKER=1, solo nello stack e2e)")


def _flag_on() -> bool:
    try:
        return bool(get_settings().live_fake_broker)
    except ValidationError:
        return False


def _check_secret(secret: str) -> str:
    secret = (secret or "").strip().upper()
    try:
        pyotp.TOTP(secret).now()
    except Exception:  # noqa: BLE001 - binascii/ValueError: not base32
        raise SMTError(ErrorCode.INVALID_ORDER, f"{TOTP_ENV} non è un segreto base32 valido",
                       {"field": "totp"}) from None
    if len(secret) < 16:
        raise SMTError(ErrorCode.INVALID_ORDER, f"{TOTP_ENV} troppo corto (min 16)",
                       {"field": "totp"})
    return secret


async def _enable_totp(session: AsyncSession, secret: str) -> None:
    settings = get_settings()
    email = normalize_email(settings.admin_email)
    user = await session.scalar(select(AdminUser).where(AdminUser.email == email))
    if user is None:
        raise SMTError(ErrorCode.NOT_FOUND, "admin e2e non trovato: esegui prima il seed",
                       {"email": email})
    user.totp_secret_encrypted = encrypt_json({"secret": secret}, settings.master_key)
    user.totp_enabled = True


async def _telegram(session: AsyncSession, now: datetime) -> None:
    cfg = await load_config(session)
    await save_config(session, TelegramConfig(
        bot_token=FAKE_BOT_TOKEN, chat_id=FAKE_CHAT_ID, enabled=True, paused=False,
        bot_username="smtrading_e2e_bot", chat_title="E2E", hold_version=cfg.hold_version))
    recent = await session.scalar(select(TelegramOutbox.id).where(
        TelegramOutbox.category == "test", TelegramOutbox.status == OutboxStatus.sent,
        TelegramOutbox.sent_at >= now - timedelta(hours=1)).limit(1))
    if recent is None:
        session.add(TelegramOutbox(source="system", category="test", level="info",
                                   text="Messaggio di prova (e2e)", status=OutboxStatus.sent,
                                   sent_at=now))


async def _profile(session: AsyncSession) -> RiskProfile:
    profile = await session.scalar(select(RiskProfile).where(
        RiskProfile.name == E2E_PROFILE_NAME))
    if profile is None:
        profile = RiskProfile(
            name=E2E_PROFILE_NAME, max_order_notional_eur=Decimal(100),
            max_daily_loss_pct=Decimal(5), allowed_symbols=[], allowed_asset_classes=[],
            allowed_markets=[], short_allowed=False,
            description="e2e denaro reale: limite per ordine, perdita giornaliera, no short")
        session.add(profile)
        await session.flush()
    return profile


async def run_live_e2e_seed(session: AsyncSession, *, totp_secret: str,
                            now: datetime | None = None) -> Portfolio:
    """Prepares the e2e database (see the module docstring). Does not commit."""
    if not _flag_on():
        raise SMTError(ErrorCode.FORBIDDEN, REFUSED_MESSAGE)
    secret = _check_secret(totp_secret)
    now = now or datetime.now(UTC)
    await _enable_totp(session, secret)
    await _telegram(session, now)
    profile = await _profile(session)
    pf = await _portfolio(session, E2E_PORTFOLIO_NAME, E2E_CASH, profile)
    for name in E2E_ETORO_PORTFOLIOS:
        await _portfolio(session, name, E2E_ETORO_CASH, profile)
    await session.flush()
    return pf


async def _portfolio(session: AsyncSession, name: str, cash: Decimal,
                     profile: RiskProfile) -> Portfolio:
    pf = await session.scalar(select(Portfolio).where(Portfolio.name == name))
    if pf is None:
        pf = await create_portfolio(session, name=name, initial_cash=cash,
                                    base_currency="EUR", risk_profile_id=profile.id)
    return pf


async def _main(secret: str) -> None:
    async with get_sessionmaker()() as session:
        pf = await run_live_e2e_seed(session, totp_secret=secret)
        await session.commit()
        print(f"seed e2e denaro reale pronto: portafoglio «{pf.name}»")


def main() -> None:
    # checked before anything else: with the flag off nothing is read or written
    if not _flag_on():
        print(REFUSED_MESSAGE, file=sys.stderr)
        sys.exit(2)
    secret = os.environ.get(TOTP_ENV, "")
    if not secret.strip():
        print(f"{TOTP_ENV} mancante: lancia il seed da scripts/e2e.sh", file=sys.stderr)
        sys.exit(2)
    try:
        asyncio.run(_main(secret))
    except SMTError as e:
        print(f"seed e2e non riuscito: {e.message}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
