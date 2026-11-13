"""MCP resources: portfolio://{portfolio_id}, fees://schedule, markets://hours.

Reads go through `run_tool` like the tools (same agent auth, permission checks and one
audit row, method "resource:<uri>"); an error is raised as a `ResourceError` whose
message is the JSON `{"code","message","details"}` text.
"""

from datetime import UTC, datetime
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ResourceError

from smtrading.agents.service import agent_portfolio_ids
from smtrading.common.errors import ErrorCode, SMTError
from smtrading.fees.calculator import FeeRuleData
from smtrading.fees.resolver import load_rules, resolve_rule
from smtrading.instruments.market_hours import all_market_status
from smtrading.instruments.models import AssetClass
from smtrading.mcp.runtime import ToolCtx, run_tool
from smtrading.mcp.tools_portfolio import portfolio_snapshot

JSON = "application/json"
_SIDES = ("buy", "sell")


async def _read(uri: str, params: dict[str, Any], fn) -> str:
    result = await run_tool(f"resource:{uri}", params, fn)
    text = "".join(c.text for c in result.content if c.type == "text")
    if result.is_error:
        raise ResourceError(text)
    return text


def _rule_dto(rule: FeeRuleData) -> dict[str, Any]:
    return {
        "id": rule.id, "scope": rule.scope, "scope_ref": rule.scope_ref, "side": rule.side,
        "currency": rule.currency, "fixed": rule.fixed, "pct": rule.pct,
        "min_fee": rule.min_fee, "max_fee": rule.max_fee,
        "tiers": [{"up_to": t.up_to, "pct": t.pct} for t in rule.tiers or ()] or None,
        "fx_pct": rule.fx_pct, "priority": rule.priority,
    }


def _resolve_or_none(rules, portfolio_id, asset_class: AssetClass, side: str):
    try:
        return _rule_dto(resolve_rule(
            rules, portfolio_id=portfolio_id, asset_class=asset_class, side=side
        ))
    except SMTError as e:
        if e.code != ErrorCode.FEE_RULE_NOT_FOUND:
            raise
        return None


async def _fee_schedule(ctx: ToolCtx) -> dict:
    """Per agent portfolio, the rule resolve_rule picks for each asset class and side
    (None when no active rule applies)."""
    rules = await load_rules(ctx.session)
    return {
        "portfolios": [
            {
                "portfolio_id": pid,
                "rules": {
                    ac.value: {side: _resolve_or_none(rules, pid, ac, side) for side in _SIDES}
                    for ac in AssetClass
                },
            }
            for pid in await agent_portfolio_ids(ctx.session, ctx.agent_id)
        ]
    }


async def _market_hours(ctx: ToolCtx) -> list:
    return list(all_market_status(datetime.now(UTC)))


def register(mcp: MCPServer) -> None:
    @mcp.resource("portfolio://{portfolio_id}", mime_type=JSON)
    async def portfolio(portfolio_id: str) -> str:
        """The portfolio valuation, as the get_portfolio tool."""
        return await _read(
            f"portfolio://{portfolio_id}", {"portfolio_id": portfolio_id},
            lambda ctx: portfolio_snapshot(ctx, portfolio_id),
        )

    @mcp.resource("fees://schedule", mime_type=JSON)
    async def fee_schedule() -> str:
        """Active fee rules for this agent's portfolios: for each portfolio, the rule that
        applies to each asset class and side ({id, scope, scope_ref, side, currency,
        fixed, pct, min_fee, max_fee, tiers, fx_pct, priority} or null)."""
        return await _read("fees://schedule", {}, _fee_schedule)

    @mcp.resource("markets://hours", mime_type=JSON)
    async def market_hours() -> str:
        """Open/closed status of every market: [{market, is_open, next_open,
        next_close}] (UTC)."""
        return await _read("markets://hours", {}, _market_hours)
