"""Smoke client: python -m smtrading.mcp.smoke --url http://localhost:8000/mcp --token <t>

Connects, prints the number of tools and calls list_portfolios, get_quote(["BTC-USD"])
and get_market_status(). Exit code 0 when all succeed, 1 otherwise.
"""

import argparse
import asyncio
import json
import sys

from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.shared._httpx_utils import create_mcp_http_client

CALLS: list[tuple[str, dict]] = [
    ("list_portfolios", {}),
    ("get_quote", {"symbols": ["BTC-USD"]}),
    ("get_market_status", {}),
]


async def run(url: str, token: str) -> int:
    ok = True
    async with (
        create_mcp_http_client(headers={"Authorization": f"Bearer {token}"}) as http,
        streamable_http_client(url, http_client=http) as (read, write),
        ClientSession(read, write) as cs,
    ):
        await cs.initialize()
        print(f"tools: {len((await cs.list_tools()).tools)}")
        for name, args in CALLS:
            result = await cs.call_tool(name, args)
            text = "".join(c.text for c in result.content if c.type == "text")
            status = "ERROR" if result.is_error else "ok"
            ok = ok and not result.is_error
            print(f"{name}: {status} {json.dumps(json.loads(text))[:300]}")
    return 0 if ok else 1


def main() -> None:
    parser = argparse.ArgumentParser(description="SMTrading MCP smoke test")
    parser.add_argument("--url", required=True)
    parser.add_argument("--token", required=True)
    args = parser.parse_args()
    try:
        code = asyncio.run(run(args.url, args.token))
    except Exception as e:  # noqa: BLE001  connection refused, 401, protocol errors
        print(f"smoke failed: {type(e).__name__}: {e}", file=sys.stderr)
        code = 1
    sys.exit(code)


if __name__ == "__main__":
    main()
