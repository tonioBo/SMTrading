"""The MCP tool catalog fingerprint: sha256 of the registered tools (name, description,
input schema), independent of the registration order and stable across processes."""

import asyncio
import os
import subprocess
import sys

from mcp.server.mcpserver import MCPServer

from smtrading.mcp.catalog import catalog_fingerprint, tool_catalog_fingerprint


def _server(*tools):
    mcp = MCPServer("x")
    for fn in tools:
        mcp.add_tool(fn)
    return mcp


def alpha(symbol: str) -> str:
    """First tool."""
    return symbol


def beta(qty: int) -> int:
    """Second tool."""
    return qty


async def test_fingerprint_is_sha256_hex_and_order_independent():
    one = await catalog_fingerprint(_server(alpha, beta))
    two = await catalog_fingerprint(_server(beta, alpha))
    assert one == two and len(one) == 64 and int(one, 16) >= 0


async def test_fingerprint_changes_with_description_schema_or_name():
    base = await catalog_fingerprint(_server(alpha, beta))

    def beta2(qty: int) -> int:
        """Second tool, reworded."""
        return qty

    beta2.__name__ = "beta"

    def beta3(qty: int, note: str = "") -> int:
        """Second tool."""
        return qty

    beta3.__name__ = "beta"

    def gamma(qty: int) -> int:
        """Second tool."""
        return qty

    for other in (beta2, beta3, gamma):
        assert await catalog_fingerprint(_server(alpha, other)) != base


def _child_env() -> dict[str, str]:
    """A different hash seed, nothing else from the parent but how to find the package
    (PYTHONPATH when the tests run from a source checkout / a shared virtualenv)."""
    env = {"PYTHONHASHSEED": "123", "PATH": ""}
    if os.environ.get("PYTHONPATH"):
        env["PYTHONPATH"] = os.environ["PYTHONPATH"]
    return env


def test_smtrading_catalog_stable_across_processes():
    here = asyncio.run(tool_catalog_fingerprint())
    code = ("import asyncio; from smtrading.mcp.catalog import tool_catalog_fingerprint; "
            "print(asyncio.run(tool_catalog_fingerprint()))")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                         check=True, env=_child_env())
    assert out.stdout.strip() == here
