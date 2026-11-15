"""`python -m smtrading.cli names audit|fix`: agent-visible names with simulation words,
with neutral proposed names for archived portfolios and agents without portfolios."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from smtrading import cli
from smtrading.agents import service as agents
from smtrading.agents.models import Agent, McpAuditLog
from smtrading.common.db import make_engine
from smtrading.engine.quotesource import FakeQuoteSource
from smtrading.portfolios.models import Portfolio, PortfolioStatus
from smtrading.portfolios.service import create_portfolio
from smtrading.seed import run_seed

T0 = datetime(2026, 11, 1, 9, 0, tzinfo=UTC)


@pytest.fixture
async def maker(fresh_db):
    engine = make_engine(fresh_db)
    sm = async_sessionmaker(engine, expire_on_commit=False)
    async with sm() as s:
        await run_seed(s)
        await s.commit()
    yield sm
    await engine.dispose()


@pytest.fixture
async def redis(redis_url):
    r = Redis.from_url(redis_url)
    yield r
    await r.aclose()


async def _cli(maker, redis, *argv) -> tuple[int, str]:
    return await cli.run_command(cli.build_parser().parse_args(argv), maker,
                                 FakeQuoteSource(), redis)


async def _names_fixture(maker) -> dict[str, str]:
    """Two archived portfolios, an agent without portfolios, an active agent with one."""
    ids = {}
    async with maker() as s:
        for i, name in enumerate(["Test 250 · A", "Paperclip"]):
            p = await create_portfolio(s, name=name, initial_cash=Decimal(250))
            p.status = PortfolioStatus.archived
            p.created_at = T0 + timedelta(hours=i)
            ids[name] = str(p.id)
        live = await create_portfolio(s, name="Alpha", initial_cash=Decimal(1000))
        smoke, _ = await agents.create_agent(s, "smoke-agent")
        demo_x, _ = await agents.create_agent(s, "demo-x")
        await s.flush()
        await agents.assign_portfolio(s, demo_x.id, live.id, "read")
        ids["smoke-agent"], ids["demo-x"] = str(smoke.id), str(demo_x.id)
        await s.commit()
    return ids


def _rows(out: str) -> dict[str, list[str]]:
    rows = [line.split(";") for line in out.strip().splitlines()]
    assert all(len(r) == 4 for r in rows), out
    return {r[1]: r for r in rows}


async def test_names_audit_lists_proposals(maker, redis):
    ids = await _names_fixture(maker)
    code, out = await _cli(maker, redis, "names", "audit")
    assert code == 0, out
    rows = _rows(out)
    assert rows[ids["Test 250 · A"]] == [
        "portafoglio", ids["Test 250 · A"], "Test 250 · A", "Portafoglio archiviato 1"]
    assert rows[ids["Paperclip"]] == [
        "portafoglio", ids["Paperclip"], "Paperclip", "Portafoglio archiviato 2"]
    assert rows[ids["smoke-agent"]] == [
        "agente", ids["smoke-agent"], "smoke-agent", "Agente archiviato 1"]
    assert rows[ids["demo-x"]] == ["agente", ids["demo-x"], "demo-x", "—"]
    # the seeded active "Demo" portfolio is listed, to be renamed by hand
    assert ["portafoglio", "Demo", "—"] in [[r[0], r[2], r[3]] for r in rows.values()]
    assert all(r[2] != "Alpha" for r in rows.values())


async def test_names_audit_skips_existing_names(maker, redis):
    ids = await _names_fixture(maker)
    async with maker() as s:
        await create_portfolio(s, name="Portafoglio archiviato 1", initial_cash=Decimal(1))
        await agents.create_agent(s, "Agente archiviato 1")
        await s.commit()
    rows = _rows((await _cli(maker, redis, "names", "audit"))[1])
    assert rows[ids["Test 250 · A"]][3] == "Portafoglio archiviato 2"
    assert rows[ids["Paperclip"]][3] == "Portafoglio archiviato 3"
    assert rows[ids["smoke-agent"]][3] == "Agente archiviato 2"


async def _names(maker) -> set[str]:
    async with maker() as s:
        return set(await s.scalars(select(Portfolio.name))) | set(
            await s.scalars(select(Agent.name)))


async def _fix_audits(maker) -> list[McpAuditLog]:
    async with maker() as s:
        return list(await s.scalars(
            select(McpAuditLog).where(McpAuditLog.method == "admin:names.fix")))


async def test_names_fix_requires_yes_and_audits(maker, redis):
    ids = await _names_fixture(maker)
    before = await _names(maker)
    code, dry = await _cli(maker, redis, "names", "fix")
    assert code == 0 and dry == (await _cli(maker, redis, "names", "audit"))[1]
    assert await _names(maker) == before and await _fix_audits(maker) == []

    code, out = await _cli(maker, redis, "names", "fix", "--yes")
    assert code == 0 and out == "rinominati: 3"
    after = await _names(maker)
    assert {"Portafoglio archiviato 1", "Portafoglio archiviato 2",
            "Agente archiviato 1", "demo-x", "Demo"} <= after
    assert not {"Test 250 · A", "Paperclip", "smoke-agent"} & after
    [audit] = await _fix_audits(maker)
    assert audit.agent_id is None and audit.outcome == "ok"
    renamed = {r["id"]: (r["type"], r["from"], r["to"]) for r in audit.params["renamed"]}
    assert renamed == {
        ids["Test 250 · A"]: ("portafoglio", "Test 250 · A", "Portafoglio archiviato 1"),
        ids["Paperclip"]: ("portafoglio", "Paperclip", "Portafoglio archiviato 2"),
        ids["smoke-agent"]: ("agente", "smoke-agent", "Agente archiviato 1"),
    }
