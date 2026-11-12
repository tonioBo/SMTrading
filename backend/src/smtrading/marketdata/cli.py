"""Manual market-data CLI: `python -m smtrading.marketdata.cli <command> ...` (prints JSON)."""

import argparse
import asyncio
import json
import sys
from datetime import UTC, datetime, timedelta

import httpx
from redis.asyncio import Redis

from smtrading.common.config import get_settings
from smtrading.common.db import get_sessionmaker
from smtrading.common.errors import SMTError
from smtrading.marketdata import providers  # noqa: F401  (registers the providers)
from smtrading.marketdata.router import MarketDataRouter
from smtrading.marketdata.serialization import dumps
from smtrading.marketdata.streaming import STATUS_KEY, track, untrack
from smtrading.marketdata.types import Timeframe


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="smtrading.marketdata.cli")
    sub = parser.add_subparsers(dest="command", required=True)
    quote = sub.add_parser("quote", help="latest quotes")
    quote.add_argument("symbols", nargs="+")
    bars = sub.add_parser("bars", help="historical bars")
    bars.add_argument("symbol")
    bars.add_argument("timeframe", choices=[t.value for t in Timeframe])
    bars.add_argument("--days", type=int, default=2)
    fx = sub.add_parser("fx", help="fx rate")
    fx.add_argument("base")
    fx.add_argument("quote")
    search = sub.add_parser("search", help="instrument search")
    search.add_argument("query")
    search.add_argument("--limit", type=int, default=20)
    for name in ("track", "untrack"):
        p = sub.add_parser(name, help=f"{name} symbols for streaming (source=admin)")
        p.add_argument("symbols", nargs="+")
    sub.add_parser("status", help="stream manager status")
    return parser


async def execute(args: argparse.Namespace, router: MarketDataRouter, redis) -> object:
    match args.command:
        case "quote":
            return await router.get_quotes(args.symbols)
        case "bars":
            end = datetime.now(UTC)
            return await router.get_bars(
                args.symbol, Timeframe(args.timeframe), end - timedelta(days=args.days), end
            )
        case "fx":
            return {"base": args.base.upper(), "quote": args.quote.upper(),
                    "rate": await router.get_fx_rate(args.base.upper(), args.quote.upper())}
        case "search":
            return await router.search(args.query, args.limit)
        case "track":
            await track(redis, args.symbols, source="admin")
            return {"tracked": sorted(s.upper() for s in args.symbols)}
        case "untrack":
            await untrack(redis, args.symbols, source="admin")
            return {"untracked": sorted(s.upper() for s in args.symbols)}
        case "status":
            raw = await redis.get(STATUS_KEY)
            return json.loads(raw) if raw else None
    raise AssertionError(args.command)  # pragma: no cover


async def run(args: argparse.Namespace) -> int:
    settings = get_settings()
    redis = Redis.from_url(settings.redis_url)
    try:
        async with httpx.AsyncClient() as http:
            router = MarketDataRouter(get_sessionmaker(), redis, http)
            try:
                result = await execute(args, router, redis)
            except SMTError as e:
                print(dumps({"error": e.to_dict()}))
                return 1
            print(dumps(result))
            return 0
    finally:
        await redis.aclose()


def main(argv: list[str] | None = None) -> int:
    return asyncio.run(run(build_parser().parse_args(argv)))


if __name__ == "__main__":
    sys.exit(main())
