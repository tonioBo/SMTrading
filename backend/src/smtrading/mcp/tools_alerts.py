"""Alert tools: get_triggered_alerts (notifications of the agent's portfolios)."""

from mcp.server.mcpserver import MCPServer
from mcp.types import CallToolResult
from sqlalchemy import or_, select

from smtrading.agents.service import agent_portfolio_ids
from smtrading.mcp.runtime import ToolCtx, run_tool
from smtrading.mcp.tools_portfolio import _parse_int, _parse_ts
from smtrading.risk.models import Notification

MAX_ALERTS = 500


async def _get_triggered_alerts(ctx: ToolCtx, since: str | None, limit: object) -> list[Notification]:
    after = _parse_ts(since, "since")
    n = _parse_int(limit, "limit", 1, MAX_ALERTS) or 100
    pids = await agent_portfolio_ids(ctx.session, ctx.agent_id)
    stmt = select(Notification).where(
        or_(Notification.portfolio_id.in_(pids), Notification.agent_id == str(ctx.agent_id))
    )
    if after is not None:
        stmt = stmt.where(Notification.created_at > after)
    rows = await ctx.session.scalars(
        stmt.order_by(Notification.created_at.desc(), Notification.id.desc()).limit(n)
    )
    return list(rows)


def register(mcp: MCPServer) -> None:
    @mcp.tool(structured_output=False)
    async def get_triggered_alerts(since: str | None = None, limit: int = 100) -> CallToolResult:
        """Notifications (triggered alerts, risk and margin events) for the agent's
        portfolios or addressed to the agent, newest first. since: ISO 8601 timestamp,
        only newer ones; limit: 1-500 (default 100)."""
        return await run_tool("get_triggered_alerts", {"since": since, "limit": limit},
                              lambda ctx: _get_triggered_alerts(ctx, since, limit))
