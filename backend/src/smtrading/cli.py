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
    agent notify NAME on|off (the `notify` permission: send_telegram)
    agent halt-permission NAME on|off (the `halt` permission: set_trading_halt)
    live status              (the real-money links: state, portfolio, last test)
    live disarm NAME [--reason TEXT]
                             («Disattiva denaro reale» from the shell: no TOTP; nothing
                             saves live keys or arms from the CLI)
    telegram set --chat-id ID [--token-stdin] [--no-verify] [--enable]
                 (the bot token is read only from stdin, never from argv; verified with
                 getMe/getChat like the admin API; the output never contains it)
    users list               (portal users: role, TOTP, last login, state; no passwords)
    users create EMAIL [--role admin|viewer]
                             (prints the temporary password once; default role viewer;
                             `--role admin` is the way back in when access is lost)
    users set-role EMAIL admin|viewer
    users disable EMAIL
    users reset-password EMAIL   (prints the new temporary password once)
                             (the same protections as /api/users, «at least one active
                             admin» included; the user's sessions are closed)

A temporary password is printed only by `users create` / `users reset-password`, once.
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

import smtrading.models  # noqa: F401  (complete mapper registry, e.g. broker_links FK)
from smtrading.admin import users
from smtrading.admin.models import ROLE_VIEWER, ROLES
from smtrading.agents import service as agents
from smtrading.agents.models import Agent, AgentPortfolio
from smtrading.common.config import get_settings
from smtrading.common.db import get_sessionmaker
from smtrading.common.errors import ErrorCode, SMTError
from smtrading.common.logsetup import configure_logging
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
from smtrading.telegram.settings import apply_config
from smtrading.valuation.service import value_portfolio

SOURCE = "admin"
SOURCE_CLI = "cli"
_ORDER_TYPES = (OrderType.market.value, OrderType.limit.value)
LIVE_DISARM_REASON = "disattivato da riga di comando"
NO_LIVE_LINKS = "nessun collegamento live"


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
    notify = agent_sub.add_parser("notify", help="grant/revoke the notify permission")
    notify.add_argument("name")
    notify.add_argument("state", choices=["on", "off"])
    halt = agent_sub.add_parser("halt-permission", help="grant/revoke the halt permission")
    halt.add_argument("name")
    halt.add_argument("state", choices=["on", "off"])

    live = sub.add_parser("live", help="real-money links (emergency disarm, status)")
    live_sub = live.add_subparsers(dest="live_command", required=True)
    live_sub.add_parser("status", help="the real-money links and their state")
    disarm = live_sub.add_parser("disarm", help="disarm a real-money link (no TOTP)")
    disarm.add_argument("name")
    disarm.add_argument("--reason", default=LIVE_DISARM_REASON)

    telegram = sub.add_parser("telegram", help="Telegram notifications")
    telegram_sub = telegram.add_subparsers(dest="telegram_command", required=True)
    tg_set = telegram_sub.add_parser("set", help="set the group (and the bot token from stdin)")
    tg_set.add_argument("--chat-id", required=True)
    tg_set.add_argument("--token-stdin", action="store_true",
                        help="read the bot token from stdin (never pass it as an argument)")
    tg_set.add_argument("--no-verify", action="store_true",
                        help="skip the getMe/getChat check with Telegram")
    tg_set.add_argument("--enable", action="store_true", help="also enable the notifications")

    users_p = sub.add_parser("users", help="portal users (emergency recovery)")
    users_sub = users_p.add_subparsers(dest="users_command", required=True)
    users_sub.add_parser("list", help="portal users (no passwords)")
    u_create = users_sub.add_parser("create", help="create a user; prints its temporary password once")
    u_create.add_argument("email")
    u_create.add_argument("--role", choices=ROLES, default=ROLE_VIEWER)
    u_role = users_sub.add_parser("set-role", help="change a user's role")
    u_role.add_argument("email")
    u_role.add_argument("role", choices=ROLES)
    users_sub.add_parser("disable", help="disable a user (sessions closed)").add_argument("email")
    users_sub.add_parser(
        "reset-password", help="new temporary password, printed once (sessions closed)"
    ).add_argument("email")
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
        case "notify":
            on = args.state == "on"
            agent = await agents.set_agent_can_notify(
                session, (await _agent(session, args.name)).id, on
            )
            await agents.write_audit(
                session, agent_id=None, method="admin:agent.notify",
                params={"agent_id": str(agent.id), "agent": agent.name, "can_notify": on,
                        "by": SOURCE_CLI},
                outcome="ok", latency_ms=0,
            )
            return {"id": agent.id, "name": agent.name, "can_notify": agent.can_notify}
        case "halt-permission":
            on = args.state == "on"
            agent = await agents.set_agent_can_halt(
                session, (await _agent(session, args.name)).id, on
            )
            await agents.write_audit(
                session, agent_id=None, method="admin:agent.halt",
                params={"agent_id": str(agent.id), "agent": agent.name, "can_halt": on,
                        "by": SOURCE_CLI},
                outcome="ok", latency_ms=0,
            )
            return {"id": agent.id, "name": agent.name, "can_halt": agent.can_halt}
    raise AssertionError(args.agent_command)  # pragma: no cover


async def execute_live(args: argparse.Namespace, session: AsyncSession, redis=None) -> object:
    """`live status` / `live disarm` (the same service as the admin's one-click disarm)."""
    from smtrading.brokers.live import LIVE_KINDS
    from smtrading.brokers.live.service import Actor, disarm_live_link_now
    from smtrading.brokers.models import BrokerLink

    match args.live_command:
        case "status":
            rows = (await session.execute(
                select(BrokerLink, Portfolio.name)
                .outerjoin(Portfolio, Portfolio.broker_link_id == BrokerLink.id)
                .where(BrokerLink.kind.in_(LIVE_KINDS)).order_by(BrokerLink.name)
            )).all()
            if not rows:
                return {"links": [], "message": NO_LIVE_LINKS}
            return {"links": [
                {"name": link.name, "kind": link.kind, "live_state": link.live_state,
                 "portfolio": portfolio, "armed_at": link.armed_at, "armed_by": link.armed_by,
                 "disarmed_at": link.disarmed_at, "disarmed_reason": link.disarmed_reason,
                 "last_test_at": link.last_test_at, "health_failures": link.health_failures}
                for link, portfolio in rows
            ]}
        case "disarm":
            link_id = await session.scalar(select(BrokerLink.id).where(
                BrokerLink.name == args.name, BrokerLink.kind.in_(LIVE_KINDS)))
            if link_id is None:
                raise SMTError(ErrorCode.NOT_FOUND, "collegamento reale non trovato",
                               {"name": args.name})
            reason = (args.reason or "").strip() or LIVE_DISARM_REASON
            await session.commit()  # the two phases use their own sessions (R8.12)
            maker = async_sessionmaker(bind=session.bind, expire_on_commit=False)
            orders = await disarm_live_link_now(maker, link_id, actor=Actor(SOURCE_CLI),
                                                reason=reason, redis=redis)
            state = await session.scalar(select(BrokerLink.live_state)
                                         .where(BrokerLink.id == link_id)
                                         .execution_options(populate_existing=True))
            return {"name": args.name, "live_state": state, "orders": orders,
                    "settled": orders is not None}
    raise AssertionError(args.live_command)  # pragma: no cover


def _user_row(user) -> dict:
    dto = users.user_dto(user)
    return {k: dto[k] for k in ("email", "role", "totp_enabled", "must_change_password",
                                "active", "last_login_at", "created_at", "created_by")}


async def execute_users(args: argparse.Namespace, session: AsyncSession, redis=None) -> object:
    """The /api/users protections (users.py); the actor is `cli` (no TOTP step-up: shell
    access to the server already is the strongest credential). Sessions are closed before
    the commit: a disabled user's session would be refused anyway, a new password changes
    the credentials fingerprint."""
    actor = users.CLI_ACTOR
    match args.users_command:
        case "list":
            return [_user_row(u) for u in await users.list_users(session)]
        case "create":
            user, temp = await users.create_user(session, email=args.email, role=args.role,
                                                 actor=actor)
            return {**_user_row(user), "temp_password": temp}
    target = await users.find_by_email(session, args.email)
    match args.users_command:
        case "set-role":
            user = await users.set_role(session, target.id, args.role, actor=actor)
            result = _user_row(user)
        case "disable":
            user = await users.set_active(session, target.id, False, actor=actor)
            result = _user_row(user)
        case "reset-password":
            user, temp = await users.reset_password(session, target.id, actor=actor)
            result = {**_user_row(user), "temp_password": temp}
        case _:  # pragma: no cover
            raise AssertionError(args.users_command)
    if redis is not None:
        await users.revoke_sessions(redis, user.id)
    return result


def _read_token_stdin() -> str:
    return sys.stdin.read()


async def execute_telegram(args: argparse.Namespace, session: AsyncSession) -> object:
    """`telegram set`: the same validation and verification as PUT /api/telegram/config."""
    fields = {"chat_id"}
    token = None
    if args.token_stdin:
        token, fields = _read_token_stdin(), fields | {"bot_token"}
    if args.enable:
        fields.add("enabled")
    async with httpx.AsyncClient() as http:
        cfg = await apply_config(
            session, http, fields=fields, bot_token=token, chat_id=args.chat_id,
            enabled=True if args.enable else None, verify=not args.no_verify, by=SOURCE_CLI,
        )
    return {
        "bot_username": f"@{cfg.bot_username}" if cfg.bot_username else None,
        "chat_id": cfg.chat_id, "chat_title": cfg.chat_title, "enabled": cfg.enabled,
        "paused": cfg.paused,
    }


async def execute(
    args: argparse.Namespace, session: AsyncSession, qs: QuoteSource, redis=None
) -> object:
    match args.command:
        case "users":
            return await execute_users(args, session, redis)
        case "telegram":
            return await execute_telegram(args, session)
        case "live":
            return await execute_live(args, session, redis)
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
    configure_logging(add_handler=False)
    return asyncio.run(run(build_parser().parse_args(argv)))


if __name__ == "__main__":
    sys.exit(main())
