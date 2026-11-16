"""The fingerprint of the MCP tool catalog: sha256 of every registered tool's name,
description and input schema, sorted by name and serialised with sorted keys (stable across
processes and registration order). A change means the agents' sessions hold a stale
catalog (see `smtrading.paperclip.sessions`). Tools only, by design: the server's
resources and prompts are not part of the fingerprint (the agents use the tools)."""

import hashlib
import json

from mcp.server.mcpserver import MCPServer


async def catalog_fingerprint(mcp: MCPServer) -> str:
    tools = sorted(
        ({"name": t.name, "description": t.description, "input_schema": t.input_schema}
         for t in await mcp.list_tools()),
        key=lambda t: t["name"])
    blob = json.dumps(tools, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                      default=str)
    return hashlib.sha256(blob.encode()).hexdigest()


_cached: str | None = None


async def tool_catalog_fingerprint() -> str:
    """The fingerprint of the SMTrading MCP server (the one the API serves), computed once
    per process (the catalog is code: it changes only with a new release)."""
    global _cached
    if _cached is None:
        from smtrading.mcp.server import build_mcp  # lazy: the worker imports this module

        _cached = await catalog_fingerprint(build_mcp())
    return _cached
