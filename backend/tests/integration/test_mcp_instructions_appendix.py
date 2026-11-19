"""MCP get_instructions_appendix: the sections of the instructions appendix (slug
`appendice`, current version), open to every agent, portfolios or not."""

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from smtrading.agentdocs import service
from smtrading.agents import service as agents
from smtrading.common.db import make_engine
from smtrading.common.fingerprints import has_fingerprint
from smtrading.seed import run_seed


@pytest.fixture
async def env(mcp_server, mcp_call):
    engine = make_engine(mcp_server.db_url)

    async def call(args=None, token=None):
        return await mcp_call(mcp_server.url, token or mcp_server.token,
                              "get_instructions_appendix", args or {})

    def db():
        return AsyncSession(engine, expire_on_commit=False)

    yield {"call": call, "db": db}
    await engine.dispose()


async def _seed(env):
    async with env["db"]() as s:
        await run_seed(s)
        await s.commit()


async def test_index_and_sections(env):
    await _seed(env)
    is_error, index = await env["call"]()
    assert not is_error, index
    ids = [s["id"] for s in index["sections"]]
    assert ids[0] == "C1" and {"C9", "PM5", "RM2", "NOT1"} <= set(ids)
    assert all(s["title"] for s in index["sections"])
    for raw in ("C9", "c9", "§C9"):
        is_error, c9 = await env["call"]({"section": raw})
        assert not is_error, c9
        assert c9["id"] == "C9" and c9["text"].startswith("Il trading halt")
    assert not has_fingerprint(c9["title"] + c9["text"])


async def test_unknown_section_lists_the_ids(env):
    await _seed(env)
    is_error, err = await env["call"]({"section": "ZZ9"})
    assert is_error and err["code"] == "INVALID_ORDER"
    assert err["details"]["field"] == "section" and "C1" in err["details"]["available"]


async def test_reads_the_current_version_and_needs_no_portfolio(env):
    await _seed(env)
    async with env["db"]() as s:
        await service.save_doc(s, "appendice", body="# A\n\n## §X1 · Nuova\ntesto nuovo",
                               note=None, base_version=1, author="admin@example.com")
        _, token = await agents.create_agent(s, "Lettore")
        await s.commit()
    is_error, x1 = await env["call"]({"section": "X1"}, token=token)
    assert not is_error, x1
    assert x1 == {"id": "X1", "title": "Nuova", "text": "testo nuovo"}


async def test_no_appendix_is_not_found(env):
    is_error, err = await env["call"]()
    assert is_error and err["code"] == "NOT_FOUND"
