"""Manual engine CLI: `python -m smtrading.cli <command> ...` (prints JSON).

    order place --portfolio NAME --symbol S --side buy|sell --qty Q
                [--type market|limit --limit-price P] [--reduce-only]
    close --portfolio NAME --symbol S [--qty Q]   (reduce-only market order)
    orders --portfolio NAME
    positions --portfolio NAME
    valuation --portfolio NAME
    cancel ORDER_ID
    agent list
    agent create NAME [--portfolio Demo --permission read|trade]
    agent rotate NAME        (prints the new token once; the old one stops working)
    agent deactivate NAME

A token is printed only by `agent create` / `agent rotate`, once: it is never stored in
clear. Quotes come from the real MarketDataRouter. Each command runs in one transaction
that is committed, then its Redis effects are dispatched (Ruling R10). On an SMTError the
error is printed as JSON with exit code 1; a rejected order (an error flagged `persisted`)
is still committed so it stays stored, anything else is rolled back. Redis is also read
for cached quotes when a provider quote is missing (Ruling R23).
"""

import argparse
import asyncio
import dataclasses
import json
import sys
from decimal import Decimal, InvalidOperation
from uuid import UUID

import httpx
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from smtrading.agents import service as agents
from smtrading.agents.models import Agent, AgentPortfolio
from smtrading.common.config import get_settings
from smtrading.common.db import get_sessionmaker
from smtrading.common.errors import ErrorCode, SMTError
from smtrading.common.money import decimal_to_str
from smtrading.engine.models import OrderSide, OrderType
from smtrading.engine.quotesource import QuoteSource
from smtrading.engine.service import (
    PlaceOrderRequest,
    cancel_order,
    close_position,
    list_orders,
    place_order,
)
from smtrading.events import discard_post_commit, dispatch_post_commit, json_safe
from smtrading.marketdata.router import MarketDataRouter
from smtrading.portfolios.models import Portfolio
from smtrading.positions.service import get_positions
from smtrading.valuation.service import value_portfolio

SOURCE = "admin"
_ORDER_TYPES = (OrderType.market.value, OrderType.limit.value)


def _decimal(text: str) -> Decimal:
    try:
        return Decimal(text)
    except InvalidOperation:
        raise argparse.ArgumentTypeError(f"invalid decimal: {text!r}") from None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="smtrading.cli")
    sub = parser.add_subparsers(dest="command", required=True)

    order = sub.add_parser("order", help="order commands")
    order_sub = order.add_subparsers(dest="order_command", required=True)
    place = order_sub.add_parser("place", help="place an order")
    place.add_argument("--portfolio", required=True)
    place.add_argument("--symbol", required=True)
    place.add_argument("--side", required=True, choices=[s.value for s in OrderSide])
    place.add_argument("--qty", required=True, type=_decimal)
    place.add_argument("--type", default=OrderType.market.value, choices=_ORDER_TYPES)
    place.add_argument("--limit-price", type=_decimal)
    place.add_argument("--reduce-only", action="store_true",
                       help="clamp to the position at fill; canceled if nothing to reduce")

    close = sub.add_parser("close", help="close a position (reduce-only market order)")
    close.add_argument("--portfolio", required=True)
    close.add_argument("--symbol", required=True)
    close.add_argument("--qty", type=_decimal, help="part of the position (default: all)")

    for name, help in (
        ("orders", "orders, newest first"), ("positions", "open positions"),
        ("valuation", "portfolio valuation (EUR)"),
    ):
        sub.add_parser(name, help=help).add_argument("--portfolio", required=True)
    cancel = sub.add_parser("cancel", help="cancel an open order")
    cancel.add_argument("order_id", type=UUID)

    agent = sub.add_parser("agent", help="MCP agents and their tokens")
    agent_sub = agent.add_subparsers(dest="agent_command", required=True)
    agent_sub.add_parser("list", help="agents with their portfolios (no tokens)")
    create = agent_sub.add_parser("create", help="create an agent; prints its token once")
    create.add_argument("name")
    create.add_argument("--portfolio", help="portfolio to assign (by name)")
    create.add_argument("--permission", choices=["read", "trade"], default="read")
    for name, help in (
        ("rotate", "issue a new token (printed once); the old one stops working"),
        ("deactivate", "disable an agent: its token stops working"),
    ):
        agent_sub.add_parser(name, help=help).add_argument("name")
    return parser


def _row(obj) -> dict:
    return {c.key: getattr(obj, c.key) for c in obj.__table__.columns}


async def _fresh_row(session: AsyncSession, obj) -> dict:
    """Row of an object just written: server-side values (updated_at, ...) reloaded."""
    await session.refresh(obj)
    return _row(obj)


async def _portfolio_id(session: AsyncSession, name: str) -> UUID:
    pid = await session.scalar(select(Portfolio.id).where(Portfolio.name == name))
    if pid is None:
        raise SMTError(ErrorCode.NOT_FOUND, "portfolio not found", {"portfolio": name})
    return pid


async def _agent(session: AsyncSession, name: str) -> Agent:
    agent = await session.scalar(select(Agent).where(Agent.name == name))
    if agent is None:
        raise SMTError(ErrorCode.NOT_FOUND, "agent not found", {"agent": name})
    return agent


async def _agent_rows(session: AsyncSession) -> list[dict]:
    links = await session.execute(
        select(AgentPortfolio.agent_id, Portfolio.name, AgentPortfolio.permission)
        .join(Portfolio, Portfolio.id == AgentPortfolio.portfolio_id)
        .order_by(Portfolio.name)
    )
    portfolios: dict[UUID, list[dict]] = {}
    for agent_id, name, permission in links:
        portfolios.setdefault(agent_id, []).append({"portfolio": name, "permission": permission})
    return [
        {
            "id": a.id, "name": a.name, "active": a.active, "token_prefix": a.token_prefix,
            "created_at": a.created_at, "last_seen_at": a.last_seen_at,
            "portfolios": portfolios.get(a.id, []),
        }
        for a in await session.scalars(select(Agent).order_by(Agent.name))
    ]


async def execute_agent(args: argparse.Namespace, session: AsyncSession) -> object:
    """Token invalidation for rotate/deactivate happens on commit (agents.service)."""
    match args.agent_command:
        case "list":
            return await _agent_rows(session)
        case "create":
            if args.portfolio is None and args.permission != "read":
                raise SMTError(ErrorCode.INVALID_ORDER, "--permission needs --portfolio")
            pid = await _portfolio_id(session, args.portfolio) if args.portfolio else None
            agent, token = await agents.create_agent(session, args.name)
            if pid is not None:
                await agents.assign_portfolio(session, agent.id, pid, args.permission)
            return {"id": agent.id, "name": agent.name, "token": token}
        case "rotate":
            agent = await _agent(session, args.name)
            return {"id": agent.id, "name": agent.name,
                    "token": await agents.rotate_token(session, agent.id)}
        case "deactivate":
            agent = await agents.set_agent_active(
                session, (await _agent(session, args.name)).id, False
            )
            return {"id": agent.id, "name": agent.name, "active": agent.active}
    raise AssertionError(args.agent_command)  # pragma: no cover


async def execute(
    args: argparse.Namespace, session: AsyncSession, qs: QuoteSource, redis=None
) -> object:
    match args.command:
        case "agent":
            return await execute_agent(args, session)
        case "order":
            req = PlaceOrderRequest(
                portfolio_id=await _portfolio_id(session, args.portfolio),
                symbol=args.symbol, side=OrderSide(args.side), type=OrderType(args.type),
                qty=args.qty, limit_price=args.limit_price, source=SOURCE,
                reduce_only=args.reduce_only,
            )
            return await _fresh_row(session, await place_order(session, qs, req, redis=redis))
        case "close":
            pid = await _portfolio_id(session, args.portfolio)
            order = await close_position(
                session, qs, pid, args.symbol, args.qty, source=SOURCE, redis=redis
            )
            return await _fresh_row(session, order)
        case "orders":
            pid = await _portfolio_id(session, args.portfolio)
            return [_row(o) for o in await list_orders(session, pid)]
        case "positions":
            pid = await _portfolio_id(session, args.portfolio)
            return [_row(p) for p in await get_positions(session, pid)]
        case "valuation":
            pid = await _portfolio_id(session, args.portfolio)
            return dataclasses.asdict(await value_portfolio(session, qs, pid, redis=redis))
        case "cancel":
            return await _fresh_row(
                session, await cancel_order(session, args.order_id, by=SOURCE)
            )
    raise AssertionError(args.command)  # pragma: no cover


def _plain(obj: object) -> object:
    """Decimals as plain strings without trailing zeros (NUMERIC(38,18) pads them)."""
    if isinstance(obj, Decimal):
        return decimal_to_str(obj)
    if isinstance(obj, dict):
        return {k: _plain(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_plain(v) for v in obj]
    return obj


def _dumps(obj: object) -> str:
    return json.dumps(json_safe(_plain(obj)), sort_keys=True)


async def run_command(
    args: argparse.Namespace, sessionmaker: async_sessionmaker[AsyncSession],
    qs: QuoteSource, redis,
) -> tuple[int, str]:
    """(exit code, JSON output). Commits on success and on an SMTError flagged `persisted`
    (a rejected order is kept); any other error rolls back."""
    async with sessionmaker() as session:
        try:
            result = await execute(args, session, qs, redis)
            code, out = 0, _dumps(result)
        except SMTError as e:
            code, out = 1, _dumps({"error": e.to_dict()})
            if not e.persisted:
                await session.rollback()
                discard_post_commit(session)
                return code, out
        await session.commit()
        await dispatch_post_commit(session, redis)
    return code, out


async def run(args: argparse.Namespace) -> int:
    settings = get_settings()
    redis = Redis.from_url(settings.redis_url)
    try:
        async with httpx.AsyncClient() as http:
            router = MarketDataRouter(get_sessionmaker(), redis, http)
            code, out = await run_command(args, get_sessionmaker(), router, redis)
            print(out)
            return code
    finally:
        await redis.aclose()


def main(argv: list[str] | None = None) -> int:
    return asyncio.run(run(build_parser().parse_args(argv)))


if __name__ == "__main__":
    sys.exit(main())
