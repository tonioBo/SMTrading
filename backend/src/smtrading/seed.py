import asyncio
import logging
from decimal import Decimal

from sqlalchemy import select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from smtrading.admin.auth import MIN_PASSWORD_LEN, hash_password, normalize_email
from smtrading.admin.models import AdminUser
from smtrading.agents.models import Agent
from smtrading.agents.service import assign_portfolio, create_agent
from smtrading.common.config import get_settings
from smtrading.common.db import get_sessionmaker
from smtrading.connectors.service import seed_connectors
from smtrading.fees.models import FeeRule, FeeScope, FeeSide
from smtrading.instruments.catalog import seed_catalog
from smtrading.instruments.models import AssetClass, Instrument, InstrumentSource
from smtrading.portfolios.models import Portfolio
from smtrading.portfolios.service import create_portfolio
from smtrading.risk.models import AppSetting, InterestKind, InterestRate, RiskProfile, SystemState
from smtrading.risk.profiles import DEFAULT_RISK_PROFILE_NAME
from smtrading.risk.settings import DEFAULT_SPREAD_BPS, DEFAULT_STALE_QUOTE_S

log = logging.getLogger("smtrading.seed")

# Serializes concurrent seed runs (e.g. two migrate containers), transaction-scoped.
SEED_LOCK_KEY = 7300002

DEFAULT_FEE_RULE_NAME = "Default globale"
DEMO_PORTFOLIO_NAME = "Demo"
DEMO_AGENT_NAME = "paperclip-demo"

_INTEREST_RATES = [
    (InterestKind.loan, "EUR", 5),
    (InterestKind.loan, "USD", 6),
    (InterestKind.loan, "*", 6),
    (InterestKind.borrow, "us_equity", 3),
    (InterestKind.borrow, "eu_equity", 3),
    (InterestKind.borrow, "crypto", 10),
    (InterestKind.borrow, "forex", 0),
]
_APP_SETTINGS = {
    "stale_quote_s": dict(DEFAULT_STALE_QUOTE_S),
    "default_spread_bps": dict(DEFAULT_SPREAD_BPS),
}
_SYSTEM_STATE = {"kill_switch": {"on": False}}

_US = (AssetClass.us_equity, "USD", "0.000001", "0.01", True)
_EU_MTA = (AssetClass.eu_equity, "EUR", "1", "0.001", True)
_EU_XETRA = (AssetClass.eu_equity, "EUR", "1", "0.01", True)
_CRYPTO = (AssetClass.crypto, "USD", "0.00000001", "0.01", False)
_FX = (AssetClass.forex, "USD", "1", "0.00001", False)

# symbol, name, market, (asset_class, currency, lot, tick, shortable)
_INSTRUMENTS = [
    ("AAPL", "Apple Inc.", "NASDAQ", _US),
    ("MSFT", "Microsoft Corp.", "NASDAQ", _US),
    ("NVDA", "NVIDIA Corp.", "NASDAQ", _US),
    ("SPY", "SPDR S&P 500 ETF", "NYSE", _US),
    ("ENI.MI", "Eni S.p.A.", "MTA", _EU_MTA),
    ("ISP.MI", "Intesa Sanpaolo", "MTA", _EU_MTA),
    ("SAP.DE", "SAP SE", "XETRA", _EU_XETRA),
    ("VWCE.DE", "Vanguard FTSE All-World UCITS ETF", "XETRA", _EU_XETRA),
    ("BTC-USD", "Bitcoin", "CRYPTO", _CRYPTO),
    ("ETH-USD", "Ethereum", "CRYPTO", _CRYPTO),
    ("EURUSD", "Euro / US Dollar", "FX", _FX),
    ("GBPUSD", "British Pound / US Dollar", "FX", _FX),
]


async def run_seed(session: AsyncSession, *, admin: tuple[str, str] | None = None) -> str | None:
    """Idempotent seed. Does not commit. Existing rows (and admin edits) are left untouched.

    `admin` = (email, password) of the web-app admin, created only if missing (the CLI
    entry point passes SMT_ADMIN_EMAIL / SMT_ADMIN_PASSWORD).

    Returns the plaintext demo-agent token when it was created by this run (print it only
    after the caller commits), else None."""
    await session.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": SEED_LOCK_KEY})
    rows = [
        {
            "symbol": symbol,
            "name": name,
            "asset_class": asset_class,
            "market": market,
            "currency": currency,
            "lot_size": Decimal(lot),
            "tick_size": Decimal(tick),
            "shortable": shortable,
            "active": True,
            "provider_symbols": {},
            "source": InstrumentSource.catalog,
        }
        for symbol, name, market, (asset_class, currency, lot, tick, shortable) in _INSTRUMENTS
    ]
    await session.execute(
        insert(Instrument).values(rows).on_conflict_do_nothing(index_elements=["symbol"])
    )
    await seed_catalog(session)  # the wider curated catalog, insert-only as well

    if await session.scalar(
        select(FeeRule.id).where(FeeRule.name == DEFAULT_FEE_RULE_NAME).limit(1)
    ) is None:
        session.add(
            FeeRule(
                name=DEFAULT_FEE_RULE_NAME,
                scope=FeeScope.global_,
                side=FeeSide.both,
                currency="EUR",
                fixed=Decimal(1),
                pct=Decimal("0.1"),
                min_fee=Decimal(1),
                max_fee=Decimal(20),
                fx_pct=Decimal("0.25"),
            )
        )

    if await session.scalar(
        select(Portfolio.id).where(Portfolio.name == DEMO_PORTFOLIO_NAME)
    ) is None:
        await create_portfolio(
            session, name=DEMO_PORTFOLIO_NAME, initial_cash=Decimal(100000), base_currency="EUR"
        )
    await seed_connectors(session)
    await _seed_risk(session)
    token = await _seed_demo_agent(session)
    if admin is not None:
        await seed_admin(session, *admin)
    await session.flush()
    return token


async def seed_admin(session: AsyncSession, email: str, password: str) -> None:
    """Create the admin user if no admin has this email; an existing password is kept.

    Empty credentials or a password shorter than MIN_PASSWORD_LEN are refused (logged)."""
    email = normalize_email(email)
    if email and await session.scalar(
        select(AdminUser.id).where(AdminUser.email == email)
    ) is not None:
        return
    if not email or len(password) < MIN_PASSWORD_LEN:
        log.error(
            "admin not created: SMT_ADMIN_EMAIL must be set and SMT_ADMIN_PASSWORD must have"
            " at least %d characters", MIN_PASSWORD_LEN,
        )
        return
    session.add(AdminUser(email=email, password_hash=await hash_password(password)))
    await session.flush()


async def _seed_demo_agent(session: AsyncSession) -> str | None:
    """Create the demo agent (trade on Demo) if missing; its token is printed exactly once."""
    if await session.scalar(select(Agent.id).where(Agent.name == DEMO_AGENT_NAME)) is not None:
        return None
    demo_id = await session.scalar(select(Portfolio.id).where(Portfolio.name == DEMO_PORTFOLIO_NAME))
    if demo_id is None:
        log.warning("portfolio %r missing: demo agent not created", DEMO_PORTFOLIO_NAME)
        return None
    agent, token = await create_agent(session, DEMO_AGENT_NAME)
    await assign_portfolio(session, agent.id, demo_id, "trade")
    return token


async def _seed_risk(session: AsyncSession) -> None:
    """Insert-if-missing: admin edits are never overwritten."""
    await session.execute(
        insert(InterestRate)
        .values([{"kind": k, "key": key, "annual_pct": Decimal(r)} for k, key, r in _INTEREST_RATES])
        .on_conflict_do_nothing()
    )
    await session.execute(
        insert(AppSetting)
        .values([{"key": k, "value": v} for k, v in _APP_SETTINGS.items()])
        .on_conflict_do_nothing()
    )
    await session.execute(
        insert(SystemState)
        .values([{"key": k, "value": v} for k, v in _SYSTEM_STATE.items()])
        .on_conflict_do_nothing()
    )
    profile_id = await session.scalar(
        select(RiskProfile.id).where(RiskProfile.name == DEFAULT_RISK_PROFILE_NAME)
    )
    if profile_id is None:
        profile = RiskProfile(
            name=DEFAULT_RISK_PROFILE_NAME,
            max_daily_loss_pct=Decimal(10),
            allowed_symbols=[],
            allowed_asset_classes=[],
            allowed_markets=[],
            short_allowed=True,
        )
        session.add(profile)
        await session.flush()
        profile_id = profile.id
    await session.execute(
        update(Portfolio)
        .where(Portfolio.name == DEMO_PORTFOLIO_NAME, Portfolio.risk_profile_id.is_(None))
        .values(risk_profile_id=profile_id)
    )


async def _main() -> None:
    async with get_sessionmaker()() as session:
        settings = get_settings()
        token = await run_seed(session, admin=(settings.admin_email, settings.admin_password))
        await session.commit()
    if token is not None:
        print(
            f"SMTrading: token agente {DEMO_AGENT_NAME} = {token} "
            "(salvalo: non verrà più mostrato)"
        )
    log.info("seed complete: instrument catalog, default fee rule, Demo portfolio")


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    asyncio.run(_main())


if __name__ == "__main__":
    main()
