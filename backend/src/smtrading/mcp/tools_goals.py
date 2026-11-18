"""get_goals: the progress of the goals («Obiettivi», plan 14) visible to the agent.

Open to every agent with at least one portfolio (`read` or `trade`; FORBIDDEN without
any): the active total goals plus the active goals of the agent's own live portfolios,
never another agent's, and not the goals that do not recur once their period is over.
Each item is the Progress of `goals.progress` without the internal goal id and the
per-portfolio `parts` (admin only; `portfolio_id` is kept: the agent knows its
portfolios). A total goal sums every live
portfolio: unless the agent holds all of them, its EUR figures (`TOTAL_EUR_FIELDS`) are
null, so they never reveal the equity of portfolios it cannot read (the percentages,
status and elapsed share stay).
"""

from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

from mcp.server.mcpserver import MCPServer
from mcp.types import CallToolResult
from sqlalchemy import select

from smtrading.agents.service import agent_portfolio_ids
from smtrading.common.config import get_settings
from smtrading.common.errors import ErrorCode, SMTError
from smtrading.goals.models import Goal
from smtrading.goals.periods import Period
from smtrading.goals.plans import grace_days
from smtrading.goals.progress import active_progress
from smtrading.mcp.runtime import ToolCtx, run_tool
from smtrading.portfolios.models import live_portfolio_ids

TOTAL_EUR_FIELDS = ("start_eur", "current_eur", "target_eur", "gap_eur")


def _now() -> datetime:
    return datetime.now(UTC)


def _plan_actions(goal: Goal, p: dict[str, Any]) -> dict[str, Any]:
    days = grace_days(goal.horizon, Period(p["period_start"], p["period_end"]))
    return {
        "grace_market_days": days,
        "summary": (
            "At period_end this plan closes the positions of its portfolios. Close the "
            "ones not in loss yourself in the pre-close cycles of each market's last "
            "session before period_end (for a week: Friday; crypto: the last cycle "
            "before period_end), through the normal close flow. Positions still open: "
            "not in loss, SMTrading closes them at their market's first session after "
            f"period_end; in loss, they get {days} market session(s) of grace after "
            "period_end (no additions; close them when it makes sense), then SMTrading "
            "closes them. New entries near the end must have time to work before "
            "period_end."),
    }


async def _get_goals(ctx: ToolCtx) -> dict[str, Any]:
    pids = await agent_portfolio_ids(ctx.session, ctx.agent_id)
    if not pids:
        raise SMTError(ErrorCode.FORBIDDEN, "agent has no read permission on any portfolio",
                       {"needed": "read"})
    now, zone = _now(), ZoneInfo(get_settings().tz)
    today = now.astimezone(zone).date()
    sees_all = set(await ctx.session.scalars(live_portfolio_ids())) <= set(pids)
    out = []
    progress = await active_progress(ctx.session, now, zone, portfolio_ids=set(pids))
    goals = {g.id: g for g in await ctx.session.scalars(
        select(Goal).where(Goal.id.in_([p["goal_id"] for p in progress])))}
    for p in progress:
        if p["period_end"] < today:  # a goal that does not recur, over
            continue
        goal = goals[p["goal_id"]]
        item = {k: v for k, v in p.items() if k not in ("goal_id", "parts")}
        item["closes_positions"] = goal.closes_positions
        item["plan_end_actions"] = (_plan_actions(goal, p) if goal.closes_positions
                                    else None)
        if item["scope"] == "total" and not sees_all:
            item.update(dict.fromkeys(TOTAL_EUR_FIELDS))
        out.append(item)
    return {"goals": out}


def register(mcp: MCPServer) -> None:
    @mcp.tool(structured_output=False)
    async def get_goals() -> CallToolResult:
        """The return objectives set for the agent's work and their progress in the
        current period: {goals: [{name, horizon (week|month|quarter|year|custom), scope
        (total: every portfolio together | portfolio), portfolio_id, portfolio_name, kind
        (min: a minimum to exceed | cap: a ceiling not to pass | max_loss: a loss not to
        reach, target_pct = the limit | beat_index: beat `benchmark` by target_pct
        points), period_start, period_end (YYYY-MM-DD, end included), start_eur,
        current_eur, target_eur (max_loss: the floor; beat_index: the benchmark's change
        plus the margin), pct, target_pct, gap_eur (target_eur - current_eur),
        elapsed_pct (business days of the period gone, 0-100), status (raggiunto | in
        linea | in ritardo | oltre il tetto | nel limite | vicino al limite | superato |
        avanti | indietro | non iniziato | concluso), as_of, benchmark (SPY | QQQ |
        VWCE.DE for beat_index, else null), benchmark_pct (its change in EUR over the
        period), diff_pp (pct - benchmark_pct), loss_pct (max_loss: the loss so far, 0 or
        more), closes_positions (true: the goal is a plan, its positions are closed at
        period_end), plan_end_actions (plans only, else null: {grace_market_days,
        summary} what happens to the positions at period_end)}]}. Amounts in EUR, percentages and amounts as strings, null without data
        yet; the EUR figures of a total goal are null unless the agent holds every
        portfolio. Total goals plus the goals of the agent's own portfolios; week,
        month, quarter and year are calendar periods (Europe/Rome). Risk limits always
        come first: a goal never justifies breaking a rule; a max_loss goal «vicino al
        limite» or «superato» means reducing risk (nothing is blocked automatically)."""
        return await run_tool("get_goals", {}, _get_goals)
