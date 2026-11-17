"""get_goals: the progress of the goals («Obiettivi», plan 14) visible to the agent.

Open to every agent with at least one portfolio (`read` or `trade`; FORBIDDEN without
any): the active total goals plus the active goals of the agent's own live portfolios,
never another agent's. Each item is the Progress of `goals.progress` without the internal
goal id (`portfolio_id` is kept: the agent knows its portfolios).
"""

from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

from mcp.server.mcpserver import MCPServer
from mcp.types import CallToolResult

from smtrading.agents.service import agent_portfolio_ids
from smtrading.common.config import get_settings
from smtrading.common.errors import ErrorCode, SMTError
from smtrading.goals.progress import active_progress
from smtrading.mcp.runtime import ToolCtx, run_tool


def _now() -> datetime:
    return datetime.now(UTC)


async def _get_goals(ctx: ToolCtx) -> dict[str, Any]:
    pids = await agent_portfolio_ids(ctx.session, ctx.agent_id)
    if not pids:
        raise SMTError(ErrorCode.FORBIDDEN, "agent has no read permission on any portfolio",
                       {"needed": "read"})
    items = await active_progress(ctx.session, _now(), ZoneInfo(get_settings().tz),
                                  portfolio_ids=set(pids))
    return {"goals": [{k: v for k, v in p.items() if k != "goal_id"} for p in items]}


def register(mcp: MCPServer) -> None:
    @mcp.tool(structured_output=False)
    async def get_goals() -> CallToolResult:
        """The return objectives set for the agent's work and their progress in the
        current period: {goals: [{name, horizon (week|month|quarter|year|custom), scope
        (total: every portfolio together | portfolio), portfolio_id, portfolio_name, kind
        (min: a minimum to exceed | cap: a ceiling not to pass), period_start, period_end
        (YYYY-MM-DD, end included), start_eur, current_eur, target_eur, pct, target_pct,
        gap_eur (target_eur - current_eur), elapsed_pct (business days of the period gone,
        0-100), status (raggiunto | in linea | in ritardo | oltre il tetto | non iniziato |
        concluso), as_of}]}. Amounts in EUR, percentages and amounts as strings, null
        without data yet. Total goals plus the goals of the agent's own portfolios; week,
        month, quarter and year are calendar periods (Europe/Rome). Risk limits always
        come first: a goal never justifies breaking a rule."""
        return await run_tool("get_goals", {}, _get_goals)
