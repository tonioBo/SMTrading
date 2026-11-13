"""On-demand instruments for MCP tools (instruments.discovery at the MCP layer).

Tools that resolve symbols for an agent call `ensure_instrument(s)` before the engine or
the router looks the symbol up. A missing symbol is discovered in its own session and
committed at once, so the instrument stays even if the tool then fails (e.g. a rejected
order). An existing row (active or not) is left to the tool's normal path.
"""

from typing import Any

from sqlalchemy import select

from smtrading.agents.models import Agent
from smtrading.events import dispatch_post_commit
from smtrading.instruments.discovery import discover
from smtrading.instruments.models import Instrument
from smtrading.instruments.service import canonical_symbol
from smtrading.mcp.runtime import ToolCtx, get_runtime


async def _missing(ctx: ToolCtx, symbols: list[str]) -> list[str]:
    wanted = list(dict.fromkeys(canonical_symbol(s) for s in symbols))
    known = set(await ctx.session.scalars(
        select(Instrument.symbol).where(Instrument.symbol.in_(wanted))))
    return [s for s in wanted if s not in known]


async def ensure_instruments(ctx: ToolCtx, symbols: Any) -> None:
    """Discover the missing symbols in order; the first refusal is raised (SMTError)."""
    if not isinstance(symbols, list):
        return
    syms = [s for s in symbols if isinstance(s, str) and s.strip()]
    if not syms or ctx.router is None:
        return
    missing = await _missing(ctx, syms)
    if not missing:
        return
    name = await ctx.session.scalar(select(Agent.name).where(Agent.id == ctx.agent_id))
    runtime = get_runtime()
    for sym in missing:
        async with runtime.sessionmaker() as session:
            await discover(
                session, ctx.router, sym, actor=f"auto:{name or ctx.agent_id}",
                agent_id=ctx.agent_id, redis=ctx.redis,
            )
            await session.commit()
            await dispatch_post_commit(session, runtime.redis)


async def ensure_instrument(ctx: ToolCtx, symbol: Any) -> None:
    await ensure_instruments(ctx, [symbol])
