"""set_trading_halt / get_trading_halt: the «blocco aperture» (risk.halts) through MCP.

set_trading_halt needs the agent's `halt` permission (`agents.can_halt`, read at call
time; granted to the CEO agent) and, for scope `portfolio`, `read` on that portfolio.
get_trading_halt is open to every agent: the active halts covering its portfolios (or the
one named). Admin authors are shown to agents as "admin", without the email. The audit
row keeps scope, portfolio_id, active and the reason (cut to 500 characters).
"""

from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.types import CallToolResult
from sqlalchemy import select

from smtrading.agents.models import Agent
from smtrading.agents.service import agent_portfolio_ids, require_permission
from smtrading.common.errors import ErrorCode, SMTError
from smtrading.mcp.dto import parse_uuid
from smtrading.mcp.runtime import ToolCtx, run_tool
from smtrading.risk import halts
from smtrading.risk.models import HaltScope, TradingHalt

_TRUE = ("true", "1", "yes", "on")
_FALSE = ("false", "0", "no", "off")


def audit_params(arguments: dict[str, Any]) -> dict[str, Any]:
    reason = arguments.get("reason")
    return {
        "scope": arguments.get("scope", HaltScope.global_.value),
        "portfolio_id": arguments.get("portfolio_id"),
        "active": arguments.get("active"),
        "reason": reason[: halts.MAX_REASON] if isinstance(reason, str) else reason,
    }


def _parse_active(v: Any) -> bool:
    if isinstance(v, bool):
        return v
    if isinstance(v, str) and v.strip().lower() in _TRUE + _FALSE:
        return v.strip().lower() in _TRUE
    raise SMTError(ErrorCode.INVALID_ORDER, "active must be true or false", {"field": "active"})


def _author(value: str | None, name: str | None) -> str | None:
    if value is None:
        return None
    return "admin" if value.startswith("admin:") else name


async def _agent_view(ctx: ToolCtx, rows: list[TradingHalt]) -> list[dict[str, Any]]:
    out = []
    for d in await halts.halt_dtos(ctx.session, rows):
        item = {
            "id": d["id"], "scope": d["scope"], "portfolio_id": d["portfolio_id"],
            "portfolio": d["portfolio"], "reason": d["reason"],
            "set_by": _author(d["set_by"], d["set_by_name"]), "since": d["set_at"],
        }
        if not d["active"]:
            item |= {"cleared_by": _author(d["cleared_by"], d["cleared_by_name"]),
                     "cleared_at": d["cleared_at"]}
        out.append(item)
    return out


async def _set_trading_halt(
    ctx: ToolCtx, scope: Any, portfolio_id: Any, active: Any, reason: Any
) -> dict:
    can_halt = await ctx.session.scalar(select(Agent.can_halt).where(Agent.id == ctx.agent_id))
    if not can_halt:
        raise SMTError(ErrorCode.FORBIDDEN, "permesso halt mancante", {"needed": "halt"})
    on = _parse_active(active)
    pid = parse_uuid(portfolio_id, "portfolio_id")
    if scope == HaltScope.portfolio and pid is not None:
        await require_permission(ctx.session, ctx.agent_id, pid, "read")
    by = f"agent:{ctx.agent_id}"
    canceled = None
    if on:
        halt, changed, canceled = await halts.set_halt(
            ctx.session, scope=scope, portfolio_id=pid, reason=reason, by=by,
            agent_id=ctx.agent_id,
        )
    else:
        halt = await halts.clear_halt(
            ctx.session, scope=scope, portfolio_id=pid, by=by, agent_id=ctx.agent_id,
            reason=reason,
        )
        changed = halt is not None
    view = (await _agent_view(ctx, [halt]))[0] if halt is not None else None
    out = {"active": on, "changed": changed, "halt": view}
    if canceled is not None:
        out["canceled"] = canceled
    return out


async def _get_trading_halt(ctx: ToolCtx, portfolio_id: Any) -> dict:
    pid = parse_uuid(portfolio_id, "portfolio_id")
    if pid is not None:
        await require_permission(ctx.session, ctx.agent_id, pid, "read")
        ids = [pid]
    else:
        ids = await agent_portfolio_ids(ctx.session, ctx.agent_id)
    rows = await halts.active_halts(ctx.session, ids)
    return {"halted": bool(rows), "halts": await _agent_view(ctx, rows)}


def register(mcp: MCPServer) -> None:
    @mcp.tool(structured_output=False)
    async def set_trading_halt(
        active: bool | str, reason: str | None = None, scope: str = "global",
        portfolio_id: str | None = None,
    ) -> CallToolResult:
        """Block (active=true) or allow again (active=false) new openings: while a halt is
        active, every order that would increase exposure (new positions, adds, short
        opens, bracket/OCO entries) is refused with TRADING_HALTED, and activating it
        cancels the resting orders that would open exposure (returned as `canceled`);
        closes, reductions, stop loss/take profit of existing positions and cancels still
        work. A halt set by an admin can be cleared only by an admin (FORBIDDEN). Needs the
        halt permission (FORBIDDEN otherwise). scope: global (every portfolio, default)
        or portfolio (portfolio_id required, needs read on it). reason: 1-500 characters,
        required to activate. Returns {active, changed, halt: {id, scope, portfolio_id,
        portfolio, reason, set_by, since, ...} or null, canceled (activation only)}; the same reason again changes
        nothing, another reason replaces the halt. SMTrading sends the change to Telegram
        itself (halt.set / halt.cleared)."""
        params = audit_params({"active": active, "reason": reason, "scope": scope,
                               "portfolio_id": portfolio_id})
        return await run_tool(
            "set_trading_halt", params,
            lambda ctx: _set_trading_halt(ctx, scope, portfolio_id, active, reason),
        )

    @mcp.tool(structured_output=False)
    async def get_trading_halt(portfolio_id: str | None = None) -> CallToolResult:
        """Active trading halts covering the agent's portfolios (or only portfolio_id,
        needs read): {halted, halts: [{id, scope, portfolio_id, portfolio, reason, set_by,
        since}]} newest first. halted=true: opening orders are refused (TRADING_HALTED)."""
        return await run_tool(
            "get_trading_halt", {"portfolio_id": portfolio_id},
            lambda ctx: _get_trading_halt(ctx, portfolio_id),
        )
