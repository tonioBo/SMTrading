"""Portfolio tools. This task: `list_portfolios`."""

from mcp.server.mcpserver import MCPServer
from mcp.types import CallToolResult
from sqlalchemy import select

from smtrading.agents.models import AgentPortfolio
from smtrading.mcp.runtime import ToolCtx, run_tool
from smtrading.portfolios.models import Portfolio


async def _list_portfolios(ctx: ToolCtx) -> list[dict]:
    rows = await ctx.session.execute(
        select(Portfolio, AgentPortfolio.permission)
        .join(AgentPortfolio, AgentPortfolio.portfolio_id == Portfolio.id)
        .where(AgentPortfolio.agent_id == ctx.agent_id)
        .order_by(Portfolio.name)
    )
    return [
        {
            "id": p.id,
            "name": p.name,
            "base_currency": p.base_currency,
            "status": p.status,
            "permission": permission,
        }
        for p, permission in rows
    ]


def register(mcp: MCPServer) -> None:
    @mcp.tool(structured_output=False)
    async def list_portfolios() -> CallToolResult:
        """The portfolios this agent may use: [{id, name, base_currency, status,
        permission}] where permission is "read" or "trade" (trade implies read)."""
        return await run_tool("list_portfolios", {}, _list_portfolios)
