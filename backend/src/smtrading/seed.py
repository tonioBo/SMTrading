import asyncio
import logging
from decimal import Decimal
from pathlib import Path

from sqlalchemy import select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

import smtrading.models  # noqa: F401  (complete mapper registry: portfolios -> broker_links FK)
from smtrading.admin.auth import MIN_PASSWORD_LEN, hash_password, normalize_email
from smtrading.admin.models import AdminUser
from smtrading.agentdocs.service import DEFAULT_DOCS_DIR, seed_agent_docs
from smtrading.agents.models import Agent
from smtrading.agents.service import assign_portfolio, create_agent
from smtrading.common.config import get_settings
from smtrading.common.db import get_sessionmaker
from smtrading.common.logsetup import configure_logging
from smtrading.connectors.service import seed_connectors
from smtrading.fees.models import FeeRule, FeeScope, FeeSide
from smtrading.instruments.catalog import seed_catalog
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

async def run_seed(
    session: AsyncSession, *, admin: tuple[str, str] | None = None,
    agent_docs_dir: Path | None = None,
) -> str | None:
    """Idempotent seed. Does not commit. Existing rows (and admin edits) are left untouched.

    `admin` = (email, password) of the web-app admin, created only if missing (the CLI
    entry point passes SMT_ADMIN_EMAIL / SMT_ADMIN_PASSWORD). `agent_docs_dir` = the
    «Istruzioni agenti» texts (default: the checkout's docs/paperclip-agents).

    Returns the plaintext demo-agent token when it was created by this run (print it only
    after the caller commits), else None."""
    await session.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": SEED_LOCK_KEY})
    # the curated catalog, insert-only: a fresh install gets the catalog values for every
    # symbol; rows already in a database (incl. the 12 pre-catalog ones) are left as they are
    await seed_catalog(session)

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
    await seed_agent_docs(session, agent_docs_dir or DEFAULT_DOCS_DIR)
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


_US_EU = ["us_equity", "eu_equity"]
_PASSIVE_ETFS = [
    "SPY", "QQQ", "VTI", "VOO", "IWM", "DIA", "EFA", "EEM", "TLT", "IEF", "SHY", "LQD", "GLD",
    "EUNL.DE", "SXR8.DE", "IS3N.DE", "VWCE.DE", "EXS1.DE", "SXRV.DE", "4GLD.DE",
]
# (name, max order EUR, max position %, max daily loss %, classes, symbols, short, "when to use")
_RISK_PRESETS = [
    ("0 · Difensivo", 500, 5, 1, _US_EU, [], False,
     "Mercati turbolenti o notizie forti: priorità a proteggere il capitale."),
    ("1 · Conservativo", 1000, 10, 2, _US_EU, [], False,
     "Prudenza generale: solo azioni ed ETF, posizioni piccole."),
    ("2 · Prudente", 1500, 15, 3, [*_US_EU, "crypto"], [], False,
     "Uso normale con cautela: azioni, ETF e crypto, niente scoperto."),
    ("3 · Moderato", 2500, 25, 5, [], [], True,
     "Fiducia nella strategia: tutti i mercati, scoperto consentito."),
    ("4 · Aggressivo", 5000, 40, 8, [], [], True,
     "Fasi di forte convinzione: posizioni grandi, rischio elevato."),
    ("5 · Passivo ETF", 2500, 30, 3, [], _PASSIVE_ETFS, False,
     ("Investimento di lungo periodo: solo ETF ampi (azionari, obbligazionari, oro), "
      "niente titoli singoli.")),
    ("6 · Solo crypto", 1000, 20, 6, ["crypto"], [], False,
     "Strategie dedicate alle criptovalute."),
]
_DEFAULT_RISK_DESCRIPTION = (
    "Libero: nessun limite per ordine o posizione, stop al 10% di perdita giornaliera. "
    "Le decisioni restano agli agenti."
)


PRESETS_SEEDED_KEY = "risk_presets_seeded"


async def _seed_presets_once(session: AsyncSession) -> None:
    """The preset profiles and the descriptions, once per database (I5): afterwards a
    deleted or renamed preset, or a cleared description, is never brought back. A database
    that already holds any preset (seeded before the flag existed) only gets the flag."""
    existing = set(await session.scalars(select(RiskProfile.name)))
    if not existing & {name for name, *_ in _RISK_PRESETS}:
        for name, order, pos, loss, classes, symbols, short, description in _RISK_PRESETS:
            session.add(
                RiskProfile(
                    name=name,
                    description=description,
                    max_order_notional_eur=Decimal(order),
                    max_position_pct=Decimal(pos),
                    max_daily_loss_pct=Decimal(loss),
                    allowed_symbols=list(symbols),
                    allowed_asset_classes=list(classes),
                    allowed_markets=[],
                    short_allowed=short,
                )
            )
        await session.execute(
            update(RiskProfile)
            .where(RiskProfile.name == DEFAULT_RISK_PROFILE_NAME,
                   RiskProfile.description.is_(None))
            .values(description=_DEFAULT_RISK_DESCRIPTION)
        )
    session.add(AppSetting(key=PRESETS_SEEDED_KEY, value={"on": True}))
    await session.flush()


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
    if await session.get(AppSetting, PRESETS_SEEDED_KEY) is None:
        await _seed_presets_once(session)
    await session.execute(
        update(Portfolio)
        .where(Portfolio.name == DEMO_PORTFOLIO_NAME, Portfolio.risk_profile_id.is_(None))
        .values(risk_profile_id=profile_id)
    )


async def _main() -> None:
    async with get_sessionmaker()() as session:
        settings = get_settings()
        docs_dir = Path(settings.agent_docs_dir) if settings.agent_docs_dir else None
        token = await run_seed(session, admin=(settings.admin_email, settings.admin_password),
                               agent_docs_dir=docs_dir)
        await session.commit()
    if token is not None:
        print(
            f"SMTrading: token agente {DEMO_AGENT_NAME} = {token} "
            "(salvalo: non verrà più mostrato)"
        )
    log.info("seed complete: instrument catalog, default fee rule, Demo portfolio, agent docs")


def main() -> None:
    configure_logging(logging.INFO)
    asyncio.run(_main())


if __name__ == "__main__":
    main()
