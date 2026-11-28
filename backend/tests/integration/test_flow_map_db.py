"""The «Fiume dei flussi» map read from the database: agent nodes from `agents`, broker
nodes from the `broker_links` kinds that exist (a fresh database: nothing else in it)."""

from sqlalchemy.ext.asyncio import AsyncSession

from smtrading.agents.models import Agent
from smtrading.brokers.models import BrokerLink
from smtrading.common.db import make_engine
from smtrading.flow.map import flow_map


async def test_flow_map_from_db(fresh_db):
    engine = make_engine(fresh_db)
    try:
        async with AsyncSession(engine) as session:
            a1 = Agent(name="Portfolio Manager", token_prefix="aaaa0001", token_hash="x")
            a2 = Agent(name="Zeta Bot", token_prefix="aaaa0002", token_hash="x")
            session.add_all([
                a1, a2,
                BrokerLink(name="L1", kind="alpaca_paper"),
                BrokerLink(name="L2", kind="alpaca_paper"),
                BrokerLink(name="L3", kind="etoro_demo"),
            ])
            await session.flush()
            out = await flow_map(session)
    finally:
        await engine.dispose()

    nodes = {n["key"]: n for n in out["nodes"]}
    assert set(nodes[f"agent:{a1.id}"]) == {"key", "label", "kind", "column", "description"}
    assert nodes[f"agent:{a1.id}"]["label"] == "Portfolio Manager"
    assert nodes[f"agent:{a1.id}"]["description"] == "Decide entrate e uscite, invia gli ordini"
    assert nodes[f"agent:{a2.id}"]["label"] == "Zeta Bot"
    assert nodes[f"agent:{a2.id}"]["description"] == "Agente"
    assert {k for k in nodes if k.startswith("broker:")} == {"broker:alpaca_paper", "broker:etoro_demo"}
    assert nodes["broker:alpaca_paper"]["label"] == "Alpaca (prova)"
    assert nodes["broker:etoro_demo"]["kind"] == "broker"

    columns = {k: n["column"] for k, n in nodes.items()}
    assert columns[f"agent:{a1.id}"] == 0 and columns[f"agent:{a2.id}"] == 0
    assert columns["pc"] == 1 and columns["mcp"] == 2 and columns["rules"] == 2
    assert all(columns[k] == 3 for k in ("ng", "eng", "db", "px"))
    assert all(columns[k] == 4 for k in ("sync", "sim", "tg"))
    assert columns["broker:alpaca_paper"] == 5 and columns["broker:etoro_demo"] == 5

    links = out["links"]
    for pair in (["sync", "broker:alpaca_paper"], ["sync", "broker:etoro_demo"],
                 [f"agent:{a1.id}", "pc"], [f"agent:{a2.id}", "pc"], ["pc", "mcp"],
                 ["mcp", "eng"], ["mcp", "px"], ["mcp", "ng"], ["mcp", "db"], ["mcp", "tg"],
                 ["eng", "sync"], ["eng", "sim"], ["eng", "tg"], ["px", "rules"],
                 ["rules", "eng"]):
        assert pair in links, pair
    assert len(links) == len({tuple(p) for p in links})
    keys = set(nodes)
    assert all(a in keys and b in keys for a, b in links)
