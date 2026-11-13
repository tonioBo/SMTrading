"""Alert tools: get_triggered_alerts (notifications of the agent's portfolios).

Polling contract: the first call (no cursor) returns the newest alerts, newest first, and
`next_after_id` = the newest id returned. Each next call passes `after_id=next_after_id`
and gets the alerts created after that one, oldest first (ascending created_at, id), and
a new `next_after_id` (the last id returned, or the same cursor when nothing is new). A
page of `limit` alerts means more may be waiting: call again at once.
"""

from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.types import CallToolResult
from sqlalchemy import or_, select, tuple_

from smtrading.agents.service import agent_portfolio_ids
from smtrading.common.errors import ErrorCode, SMTError
from smtrading.mcp.dto import notification_dto, parse_int, parse_ts
from smtrading.mcp.runtime import ToolCtx, run_tool
from smtrading.risk.models import Notification

MAX_ALERTS = 500


async def _get_triggered_alerts(
    ctx: ToolCtx, since: Any, after_id: Any, limit: Any
) -> dict:
    after = parse_ts(since, "since")
    cursor = parse_int(after_id, "after_id", 0)
    n = parse_int(limit, "limit", 1, MAX_ALERTS) or 100
    pids = await agent_portfolio_ids(ctx.session, ctx.agent_id)
    stmt = select(Notification).where(
        or_(Notification.portfolio_id.in_(pids), Notification.agent_id == str(ctx.agent_id))
    )
    if after is not None:
        stmt = stmt.where(Notification.created_at > after)
    if cursor is not None:
        cursor_ts = await ctx.session.scalar(
            select(Notification.created_at).where(Notification.id == cursor)
        )
        if cursor_ts is None:
            raise SMTError(ErrorCode.INVALID_ORDER, "unknown after_id", {"field": "after_id"})
        stmt = stmt.where(
            tuple_(Notification.created_at, Notification.id) > tuple_(cursor_ts, cursor)
        )

    if after is None and cursor is None:
        rows = list(await ctx.session.scalars(
            stmt.order_by(Notification.created_at.desc(), Notification.id.desc()).limit(n)
        ))
        next_after = rows[0].id if rows else None
    else:
        rows = list(await ctx.session.scalars(
            stmt.order_by(Notification.created_at, Notification.id).limit(n)
        ))
        next_after = rows[-1].id if rows else cursor
    return {"alerts": [notification_dto(r) for r in rows], "next_after_id": next_after}


def register(mcp: MCPServer) -> None:
    @mcp.tool(structured_output=False)
    async def get_triggered_alerts(
        since: str | None = None, after_id: int | str | None = None,
        limit: int | str | None = 100,
    ) -> CallToolResult:
        """Notifications (triggered alerts, risk and margin events) for the agent's
        portfolios or addressed to the agent: {alerts: [{id, portfolio_id, kind, payload,
        created_at}], next_after_id}. Without since/after_id: the newest, newest first.
        With since (ISO 8601) and/or after_id (an alert id): only later ones, oldest
        first. To poll, pass after_id = the previous next_after_id. limit: 1-500
        (default 100)."""
        params = {"since": since, "after_id": after_id, "limit": limit}
        return await run_tool(
            "get_triggered_alerts", params,
            lambda ctx: _get_triggered_alerts(ctx, since, after_id, limit),
        )
