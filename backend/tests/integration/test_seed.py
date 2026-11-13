import re
from decimal import Decimal

from sqlalchemy import select, text

from smtrading.fees.models import FeeScope
from smtrading.fees.resolver import load_rules
from smtrading.instruments.models import AssetClass
from smtrading.instruments.service import get_instrument
from smtrading.portfolios.models import Portfolio
from smtrading.portfolios.service import get_cash_balances
from smtrading.seed import run_seed


async def test_seed_idempotent(db_session):
    await run_seed(db_session)
    await run_seed(db_session)
    demo = (
        (await db_session.execute(select(Portfolio).where(Portfolio.name == "Demo")))
        .scalars()
        .all()
    )
    assert len(demo) == 1
    assert await get_cash_balances(db_session, demo[0].id) == {"EUR": Decimal(100000)}
    btc = await get_instrument(db_session, "btc-usd")
    assert btc.asset_class == AssetClass.crypto
    assert btc.shortable is False
    assert (await get_instrument(db_session, "AAPL")).shortable is True
    rules = await load_rules(db_session)
    assert len([r for r in rules if r.scope == FeeScope.global_]) == 1


async def test_seed_takes_advisory_lock(db_session):
    await run_seed(db_session)
    held = await db_session.scalar(
        text(
            "select count(*) from pg_locks where locktype = 'advisory'"
            " and objid = 7300002 and pid = pg_backend_pid() and granted"
        )
    )
    assert held == 1


async def test_seed_risk_and_settings(db_session):
    from smtrading.risk.models import (
        AppSetting,
        InterestKind,
        InterestRate,
        RiskProfile,
        SystemState,
    )

    await run_seed(db_session)
    await run_seed(db_session)
    profiles = (await db_session.execute(select(RiskProfile))).scalars().all()
    assert [p.name for p in profiles] == ["Default"]
    p = profiles[0]
    assert p.max_daily_loss_pct == Decimal(10) and p.short_allowed is True
    assert p.max_order_notional_eur is None and p.allowed_symbols == []
    demo = (
        await db_session.execute(select(Portfolio).where(Portfolio.name == "Demo"))
    ).scalar_one()
    assert demo.risk_profile_id == p.id
    rates = {
        (r.kind, r.key): r.annual_pct
        for r in (await db_session.execute(select(InterestRate))).scalars()
    }
    assert rates == {
        (InterestKind.loan, "EUR"): Decimal(5),
        (InterestKind.loan, "USD"): Decimal(6),
        (InterestKind.loan, "*"): Decimal(6),
        (InterestKind.borrow, "us_equity"): Decimal(3),
        (InterestKind.borrow, "eu_equity"): Decimal(3),
        (InterestKind.borrow, "crypto"): Decimal(10),
        (InterestKind.borrow, "forex"): Decimal(0),
    }
    stale = await db_session.get(AppSetting, "stale_quote_s")
    assert stale.value == {"us_equity": 120, "eu_equity": 1200, "crypto": 60, "forex": 600}
    spread = await db_session.get(AppSetting, "default_spread_bps")
    assert spread.value == {"us_equity": 2, "eu_equity": 5, "crypto": 5, "forex": 1}
    assert (await db_session.get(SystemState, "kill_switch")).value == {"on": False}


async def test_seed_keeps_admin_edits_and_existing_profile_assignment(db_session):
    from smtrading.risk.models import AppSetting, RiskProfile

    await run_seed(db_session)
    (await db_session.get(AppSetting, "stale_quote_s")).value = {"us_equity": 5}
    other = RiskProfile(name="Strict", allowed_symbols=[], allowed_asset_classes=[],
                        allowed_markets=[], short_allowed=False)
    db_session.add(other)
    await db_session.flush()
    demo = (
        await db_session.execute(select(Portfolio).where(Portfolio.name == "Demo"))
    ).scalar_one()
    demo.risk_profile_id = other.id
    await db_session.flush()
    await run_seed(db_session)
    await db_session.refresh(demo)
    assert demo.risk_profile_id == other.id
    assert (await db_session.get(AppSetting, "stale_quote_s")).value == {"us_equity": 5}


async def test_seed_creates_agent_once(db_session, capsys):
    from smtrading.agents.models import Agent, AgentPortfolio

    token = await run_seed(db_session)
    assert token is not None and re.fullmatch(r"smt_[a-z0-9]{8}_[A-Za-z0-9_-]{40}", token)
    assert capsys.readouterr().out == ""  # printing is the committing caller's job
    agent = (await db_session.execute(select(Agent).where(Agent.name == "paperclip-demo"))).scalar_one()
    assert token not in agent.token_hash
    demo = await db_session.scalar(select(Portfolio.id).where(Portfolio.name == "Demo"))
    link = await db_session.get(AgentPortfolio, (agent.id, demo))
    assert link is not None and link.permission == "trade"

    assert await run_seed(db_session) is None
    assert len((await db_session.execute(select(Agent))).scalars().all()) == 1


async def test_demo_agent_skipped_without_demo_portfolio(db_session, caplog):
    from smtrading import seed
    from smtrading.agents.models import Agent

    assert await db_session.scalar(select(Portfolio.id).where(Portfolio.name == "Demo")) is None
    with caplog.at_level("WARNING", logger="smtrading.seed"):
        assert await seed._seed_demo_agent(db_session) is None
    assert "demo agent not created" in caplog.text
    assert (await db_session.execute(select(Agent))).scalars().all() == []
