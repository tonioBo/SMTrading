"""The fingerprint of the MCP tool catalog: sha256 of every registered tool's name,
description and input schema, sorted by name and serialised with sorted keys (stable across
processes and registration order). A change means the agents' sessions hold a stale
catalog (see `smtrading.paperclip.sessions`)."""

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


async def tool_catalog_fingerprint() -> str:
    """The fingerprint of the SMTrading MCP server (the one the API serves)."""
    from smtrading.mcp.server import build_mcp  # lazy: the worker imports this module

    return await catalog_fingerprint(build_mcp())
