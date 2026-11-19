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
    assert len(profiles) == 8
    p = next(x for x in profiles if x.name == "Default")
    assert p.description.startswith("Libero")
    etf = next(x for x in profiles if x.name == "5 · Passivo ETF")
    assert len(etf.allowed_symbols) == 20 and etf.max_position_pct == Decimal(30)
    assert etf.short_allowed is False and etf.max_daily_loss_eur is None
    assert all(x.description for x in profiles)
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


async def test_seed_catalog_insert_only(db_session):
    from smtrading.instruments.catalog import load_catalog, seed_catalog
    from smtrading.instruments.models import Instrument, InstrumentSource

    await run_seed(db_session)
    symbols = {r.symbol for r in load_catalog()}
    stored = set(await db_session.scalars(
        select(Instrument.symbol).where(Instrument.symbol.in_(symbols))))
    assert stored == symbols
    mc = await db_session.get(Instrument, "MC.PA")
    assert (mc.market, mc.source, mc.added_by) == ("EURONEXT_PARIS", InstrumentSource.catalog, None)
    # an admin edit and a deactivation survive a new run; nothing new is inserted
    mc.name, mc.tick_size, mc.active = "Edited", Decimal("0.5"), False
    await db_session.flush()
    assert await seed_catalog(db_session) == 0
    await run_seed(db_session)
    await db_session.refresh(mc)
    assert (mc.name, mc.tick_size, mc.active) == ("Edited", Decimal("0.5"), False)
    aapl = await db_session.get(Instrument, "AAPL")
    assert aapl.source == InstrumentSource.catalog
    sui = await db_session.get(Instrument, "SUI-USD")
    assert sui.provider_symbols["yahoo"] == "SUI20947-USD"


async def test_seed_catalog_adds_only_the_missing_rows(db_session):
    """An install seeded from an older catalog gets the new rows at the next deploy."""
    from sqlalchemy import delete

    from smtrading.instruments.catalog import seed_catalog
    from smtrading.instruments.models import Instrument

    await run_seed(db_session)
    new = ["HYPE-USD", "IVV", "LHA.DE", "SWDA.MI"]  # rows added to the catalog later
    await db_session.execute(delete(Instrument).where(Instrument.symbol.in_(new)))
    aapl = await db_session.get(Instrument, "AAPL")
    aapl.name = "Edited"
    await db_session.flush()
    assert await seed_catalog(db_session) == len(new)
    assert await seed_catalog(db_session) == 0  # idempotent
    await db_session.refresh(aapl)
    assert aapl.name == "Edited"
    ivv = await db_session.get(Instrument, "IVV")
    assert (ivv.market, ivv.currency, ivv.lot_size) == ("NYSE", "USD", Decimal("0.000001"))


async def test_seed_risk_never_overwrites_existing(db_session):
    from smtrading.risk.models import RiskProfile

    await run_seed(db_session)
    prof = (
        await db_session.execute(select(RiskProfile).where(RiskProfile.name == "1 · Conservativo"))
    ).scalar_one()
    prof.max_position_pct = Decimal(12)
    prof.description = "mia"
    default = (
        await db_session.execute(select(RiskProfile).where(RiskProfile.name == "Default"))
    ).scalar_one()
    default.description = None
    await db_session.flush()
    await run_seed(db_session)
    await db_session.refresh(prof)
    await db_session.refresh(default)
    assert prof.max_position_pct == Decimal(12) and prof.description == "mia"
    assert default.description is None  # cleared by the admin: never refilled (I5)


async def test_fresh_seed_uses_catalog_rows(db_session):
    """M15: on a fresh install the 12 pre-catalog symbols get the catalog values."""
    from smtrading.instruments.models import Instrument

    await run_seed(db_session)
    aapl = await db_session.get(Instrument, "AAPL")
    assert aapl.tick_size == Decimal("0.0001") and aapl.name == "Apple Inc."
    assert (await db_session.get(Instrument, "ENI.MI")).tick_size == Decimal("0.0001")
    btc = await db_session.get(Instrument, "BTC-USD")
    assert btc.provider_symbols.get("coingecko") == "bitcoin:usd"


async def test_existing_legacy_instrument_rows_are_not_modified(db_session):
    from smtrading.instruments.models import Instrument

    await run_seed(db_session)
    aapl = await db_session.get(Instrument, "AAPL")
    aapl.tick_size, aapl.name = Decimal("0.01"), "Apple (admin)"
    await db_session.flush()
    await run_seed(db_session)
    await db_session.refresh(aapl)
    assert (aapl.tick_size, aapl.name) == (Decimal("0.01"), "Apple (admin)")


async def test_presets_seeded_once(db_session):
    """I5: a deleted preset is not re-inserted, a cleared description stays cleared."""
    from smtrading.risk.models import AppSetting, RiskProfile

    await run_seed(db_session)
    assert (await db_session.get(AppSetting, "risk_presets_seeded")) is not None
    by_name = {p.name: p for p in await db_session.scalars(select(RiskProfile))}
    await db_session.delete(by_name["0 · Difensivo"])
    by_name["1 · Conservativo"].description = None
    by_name["Default"].description = None
    await db_session.flush()
    await run_seed(db_session)
    names = set(await db_session.scalars(select(RiskProfile.name)))
    assert "0 · Difensivo" not in names
    await db_session.refresh(by_name["1 · Conservativo"])
    await db_session.refresh(by_name["Default"])
    assert by_name["1 · Conservativo"].description is None
    assert by_name["Default"].description is None


async def test_presets_flag_set_on_existing_install_without_inserting(db_session):
    """An install seeded before the flag: presets present (one deleted) -> flag only."""
    from smtrading.risk.models import AppSetting, RiskProfile

    await run_seed(db_session)
    await db_session.delete(await db_session.get(AppSetting, "risk_presets_seeded"))
    renamed = (await db_session.execute(
        select(RiskProfile).where(RiskProfile.name == "2 · Prudente"))).scalar_one()
    renamed.name = "Prudente (mio)"
    await db_session.flush()
    await run_seed(db_session)
    names = set(await db_session.scalars(select(RiskProfile.name)))
    assert "2 · Prudente" not in names and "Prudente (mio)" in names
    assert (await db_session.get(AppSetting, "risk_presets_seeded")) is not None


async def test_preset_symbols_in_catalog():
    from smtrading.instruments.catalog import load_catalog
    from smtrading.seed import _RISK_PRESETS

    catalog = {r.symbol for r in load_catalog()}
    for name, *_rest in _RISK_PRESETS:
        symbols = _rest[4]
        assert set(symbols) <= catalog, name


async def test_seed_does_not_recreate_demo_on_existing_install(db_session):
    """An install that already has portfolios/agents (the admin removed or renamed the
    Demo ones) never gets the demo portfolio or agent back on a later seed/migrate."""
    from smtrading.agents.models import Agent
    from smtrading.agents.service import create_agent
    from smtrading.portfolios.service import create_portfolio

    await create_portfolio(db_session, name="Portafoglio Prudente", initial_cash=Decimal(500))
    await create_agent(db_session, "Risk Manager")
    assert await run_seed(db_session) is None
    assert await db_session.scalar(select(Portfolio.id).where(Portfolio.name == "Demo")) is None
    names = (await db_session.execute(select(Agent.name))).scalars().all()
    assert names == ["Risk Manager"]
