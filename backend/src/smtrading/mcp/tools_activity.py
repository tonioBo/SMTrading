"""Decisions log and activity feed tools: log_decision, get_decisions, get_activity_feed.

Open to every agent. An agent sees the decisions and activity of its (non archived)
portfolios and the global ones (`activity.service` visibility rules); naming a portfolio
needs `read` on it (FORBIDDEN otherwise).

Feed polling contract (like get_triggered_alerts): the first call (no cursor) returns
the newest items, newest first, and a `next_after_id` that only covers settled rows;
each next call passes `after_id=next_after_id` and gets later items oldest first. Items
newer than the cursor can come twice: dedupe by id.
"""

import uuid
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.types import CallToolResult
from sqlalchemy import select

from smtrading.activity import service as activity
from smtrading.activity.models import DecisionKind
from smtrading.agents.models import Agent
from smtrading.agents.service import require_permission
from smtrading.mcp.dto import decision_dto, parse_enum, parse_int, parse_ts, parse_uuid
from smtrading.mcp.runtime import ToolCtx, run_tool
from smtrading.portfolios.models import Portfolio

MAX_DECISIONS = 200
MAX_FEED = 200


async def _names(ctx: ToolCtx, model, ids: set) -> dict[uuid.UUID, str]:
    ids = {i for i in ids if i is not None}
    if not ids:
        return {}
    rows = await ctx.session.execute(select(model.id, model.name).where(model.id.in_(ids)))
    return {i: name for i, name in rows.all()}


async def _log_decision(ctx: ToolCtx, kind: Any, summary: Any, portfolio_id: Any,
                        symbols: Any, details: Any, outcome: Any, reason: Any,
                        subject: Any) -> dict:
    decision = await activity.log_decision(
        ctx.session, ctx.agent_id, kind=kind, summary=summary,
        portfolio_id=parse_uuid(portfolio_id, "portfolio_id"), symbols=symbols,
        details=details, outcome=outcome, reason=reason, subject=subject,
    )
    await ctx.session.refresh(decision, ["created_at"])
    return {"decision_id": decision.id, "created_at": decision.created_at}


async def _agent_filter(ctx: ToolCtx, agent: str) -> uuid.UUID | None:
    """An agent id or name; None when no agent matches."""
    try:
        return uuid.UUID(agent)
    except ValueError:
        return await ctx.session.scalar(select(Agent.id).where(Agent.name == agent))


async def _get_decisions(ctx: ToolCtx, portfolio_id: Any, agent: Any, kind: Any, since: Any,
                         limit: Any) -> dict:
    pid = parse_uuid(portfolio_id, "portfolio_id")
    kind_ = parse_enum(DecisionKind, kind, "kind")
    after = parse_ts(since, "since")
    n = parse_int(limit, "limit", 1, MAX_DECISIONS) or 50
    if pid is not None:
        await require_permission(ctx.session, ctx.agent_id, pid, "read")
    agent_id = None
    if agent is not None:
        agent_id = await _agent_filter(ctx, str(agent))
        if agent_id is None:
            return {"decisions": []}
    rows = await activity.list_decisions(
        ctx.session, viewer_agent_id=ctx.agent_id, portfolio_id=pid, agent_id=agent_id,
        kind=kind_.value if kind_ else None, since=after, limit=n,
    )
    agents = await _names(ctx, Agent, {d.agent_id for d in rows})
    portfolios = await _names(ctx, Portfolio, {d.portfolio_id for d in rows})
    return {"decisions": [
        decision_dto(d, agents.get(d.agent_id), portfolios.get(d.portfolio_id)) for d in rows
    ]}


async def _get_activity_feed(ctx: ToolCtx, after_id: Any, since: Any, limit: Any) -> dict:
    cursor = parse_int(after_id, "after_id", 0)
    after = parse_ts(since, "since")
    n = parse_int(limit, "limit", 1, MAX_FEED) or 100
    items, next_after = await activity.list_feed(
        ctx.session, ctx.agent_id, after_id=cursor, since=after, limit=n
    )
    return {"items": items, "next_after_id": next_after}  # run_tool makes it JSON safe


def register(mcp: MCPServer) -> None:
    @mcp.tool(structured_output=False)
    async def log_decision(
        kind: str, summary: str, portfolio_id: str | None = None,
        symbols: list[str] | None = None, details: dict | None = None,
        outcome: str | None = None, reason: str | None = None, subject: str | None = None,
    ) -> CallToolResult:
        """Record a decision in SMTrading's permanent decisions log (append-only, shown in
        the web app): {decision_id, created_at}. kind: proposal, approval, veto,
        allocation, strategy, risk_rule or other. summary: 1-500 characters. portfolio_id:
        optional, needs read on it (omit for a global decision). symbols: at most 50.
        details: an object, at most 8 KB of JSON.
        outcome: optional, approvata, respinta, sospesa, modificata or informativa.
        subject: optional, what the decision is about, at most 200 characters.
        reason: optional, why, at most 300 characters.
        Logging a decision sends no Telegram message."""
        params = {"kind": kind, "summary": summary, "portfolio_id": portfolio_id,
                  "symbols": symbols, "details": details, "outcome": outcome,
                  "reason": reason, "subject": subject}
        return await run_tool(
            "log_decision", params,
            lambda ctx: _log_decision(ctx, kind, summary, portfolio_id, symbols, details,
                                      outcome, reason, subject),
        )

    @mcp.tool(structured_output=False)
    async def get_decisions(
        portfolio_id: str | None = None, agent: str | None = None, kind: str | None = None,
        since: str | None = None, limit: int | str | None = 50,
    ) -> CallToolResult:
        """Decisions visible to the agent (its portfolios' and the global ones), newest
        first: {decisions: [{id, agent, agent_id, portfolio, portfolio_id, kind, summary,
        symbols, details, outcome, reason, subject, created_at}]} (outcome, reason, subject:
        null when not given). Filters: portfolio_id (needs read), agent (name or
        id), kind, since (ISO 8601). limit: 1-200 (default 50)."""
        params = {"portfolio_id": portfolio_id, "agent": agent, "kind": kind, "since": since,
                  "limit": limit}
        return await run_tool(
            "get_decisions", params,
            lambda ctx: _get_decisions(ctx, portfolio_id, agent, kind, since, limit),
        )

    @mcp.tool(structured_output=False)
    async def get_activity_feed(
        after_id: int | str | None = None, since: str | None = None,
        limit: int | str | None = 100,
    ) -> CallToolResult:
        """SMTrading's activity feed (orders, fills, risk events, alerts, decisions) for
        the agent's portfolios and the global events: {items: [{id, ts, type, level, agent,
        agent_id, portfolio, portfolio_id, summary, data, already_sent}], next_after_id}.
        already_sent: SMTrading sends it to Telegram itself (sent, queued, in the night
        digest or about to be planned): do not repeat it.
        Without after_id/since: the newest, newest first. With after_id (pass the previous
        next_after_id) and/or since (ISO 8601): later items, oldest first. Items may come
        twice: dedupe by id. limit: 1-200 (default 100)."""
        params = {"after_id": after_id, "since": since, "limit": limit}
        return await run_tool(
            "get_activity_feed", params,
            lambda ctx: _get_activity_feed(ctx, after_id, since, limit),
        )
