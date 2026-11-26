"""Regression guard: the MCP tool catalog must not change with the intraday work. The agents'
sessions hold the catalog they saw, so during a live campaign no tool may appear, vanish or
change description/schema. Update FROZEN_FINGERPRINT only on purpose, with a release that
accepts the stale-catalog handling (see `smtrading.paperclip.sessions`)."""

from smtrading.mcp.catalog import catalog_fingerprint
from smtrading.mcp.server import build_mcp

FROZEN_FINGERPRINT = "fa77dcbd98f5c075a88b8cc8e1f902fb2677674ce2bbfc931a19d393593b71b0"


async def test_tool_catalog_unchanged_by_intraday():
    assert await catalog_fingerprint(build_mcp()) == FROZEN_FINGERPRINT, (
        "il catalogo MCP è cambiato: durante la campagna gli strumenti non devono cambiare")
