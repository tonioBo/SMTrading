"""Portfolio tools: list_portfolios, get_portfolio, get_positions, get_transactions,
get_performance, get_risk_limits. Every per-portfolio tool needs `read` on it.

`portfolio_snapshot` is shared with the portfolio:// resource (mcp/resources.py).
"""

from decimal import Decimal, localcontext
from typing import Any
from uuid import UUID

from mcp.server.mcpserver import MCPServer
from mcp.types import CallToolResult
from sqlalchemy import select

from smtrading.agents.models import AgentPortfolio
from smtrading.agents.service import require_permission
from smtrading.common.errors import ErrorCode, SMTError
from smtrading.common.money import quantize_money
from smtrading.ledger.models import JournalType
from smtrading.mcp.dto import (
    parse_enum,
    parse_int,
    parse_ts,
    parse_uuid,
    position_valuation_dto,
    valuation_dto,
)
from smtrading.mcp.runtime import ToolCtx, run_tool
from smtrading.performance import service as performance
from smtrading.portfolios.mode import PortfolioMode, portfolio_mode
from smtrading.portfolios.models import Portfolio, PortfolioStatus
from smtrading.portfolios.queries import day_pnl, list_portfolio_journals, today_day_open
from smtrading.risk.daily_loss import net_cash_flows_eur
from smtrading.risk.models import RiskProfile
from smtrading.risk.profiles import effective_risk_profile
from smtrading.risk.settings import get_mcp_hide_mode, is_kill_switch_on
from smtrading.valuation.service import PortfolioValuation, value_portfolio

MAX_TRANSACTIONS = 500
_PREC = 60


def _eur(amount: Decimal) -> Decimal:
    return quantize_money(amount, "EUR")


def _invalid(field: str, message: str) -> SMTError:
    return SMTError(ErrorCode.INVALID_ORDER, message, {"field": field})


def _required_uuid(v: str | None, field: str) -> UUID:
    parsed = parse_uuid(v, field)
    if parsed is None:
        raise _invalid(field, f"{field} is required")
    return parsed


async def _readable_portfolio(ctx: ToolCtx, portfolio_id: str | None) -> Portfolio:
    pid = _required_uuid(portfolio_id, "portfolio_id")
    await require_permission(ctx.session, ctx.agent_id, pid, "read")
    portfolio = await ctx.session.get(Portfolio, pid)
    if portfolio is None:  # unreachable while agent_portfolios has its FK
        raise SMTError(ErrorCode.NOT_FOUND, "portfolio not found", {"portfolio_id": str(pid)})
    return portfolio


async def _valuation(ctx: ToolCtx, portfolio: Portfolio) -> PortfolioValuation:
    return await value_portfolio(ctx.session, ctx.qs, portfolio.id, redis=ctx.redis)


async def _mode_field(ctx: ToolCtx, portfolio: Portfolio) -> dict[str, Any]:
    """`{"mode": ...}`, or nothing for a non-live portfolio while the admin setting
    `mcp_hide_mode` is on (agents must not learn that a portfolio is simulated/paper)."""
    mode = portfolio_mode(portfolio)
    if mode != PortfolioMode.reale and await get_mcp_hide_mode(ctx.session):
        return {}
    return {"mode": mode}


async def _list_portfolios(ctx: ToolCtx) -> list[dict]:
    rows = await ctx.session.execute(
        select(Portfolio, AgentPortfolio.permission)
        .join(AgentPortfolio, AgentPortfolio.portfolio_id == Portfolio.id)
        .where(
            AgentPortfolio.agent_id == ctx.agent_id,
            Portfolio.status != PortfolioStatus.archived,  # invisible to agents (Task 13)
        )
        .order_by(Portfolio.name)
    )
    return [
        {
            "id": p.id,
            "name": p.name,
            "base_currency": p.base_currency,
            "status": p.status,
            **await _mode_field(ctx, p),
            "permission": permission,
        }
        for p, permission in rows.all()
    ]


async def portfolio_snapshot(ctx: ToolCtx, portfolio_id: str | None) -> dict:
    """get_portfolio: valuation_dto + status, reduce_only_reason, leverage and the day
    P&L (equity - today's day_open equity - net deposits since it, EUR and %; None
    without a day_open). External cash flows are not P&L, as in risk.daily_loss."""
    portfolio = await _readable_portfolio(ctx, portfolio_id)
    valuation = await _valuation(ctx, portfolio)
    pnl, pct = await day_pnl(ctx.session, ctx.qs, portfolio, valuation)
    return {
        **valuation_dto(valuation),
        "status": portfolio.status,
        **await _mode_field(ctx, portfolio),
        "reduce_only_reason": portfolio.reduce_only_reason,
        "leverage": portfolio.leverage,
        "day_pnl_eur": pnl,
        "day_pnl_pct": pct,
    }


async def _positions(ctx: ToolCtx, portfolio_id: str | None) -> list[dict]:
    valuation = await _valuation(ctx, await _readable_portfolio(ctx, portfolio_id))
    return [
        {**position_valuation_dto(p), "side": "long" if p.qty > 0 else "short"}
        for p in valuation.positions
    ]


def _parse_types(v: str | list[str] | None) -> list[JournalType] | None:
    if v is None:
        return None
    values = [v] if isinstance(v, str) else v
    if not isinstance(values, list) or not values:
        raise _invalid("type", "type must be a journal type or a non-empty list of them")
    return [parse_enum(JournalType, t, "type") for t in values]  # type: ignore[misc]


async def _transactions(
    ctx: ToolCtx, portfolio_id: str | None, from_ts: str | None, to_ts: str | None,
    type: str | list[str] | None, limit: Any, before_seq: Any,
) -> list[dict]:
    start, end = parse_ts(from_ts, "from_ts"), parse_ts(to_ts, "to_ts")
    types = _parse_types(type)
    n = parse_int(limit, "limit", 1, MAX_TRANSACTIONS) or 100
    before = parse_int(before_seq, "before_seq", 1)
    portfolio = await _readable_portfolio(ctx, portfolio_id)

    return await list_portfolio_journals(
        ctx.session, portfolio.id, limit=n, types=types, start=start, end=end,
        before_seq=before,
    )


async def _performance(ctx: ToolCtx, portfolio_id: str | None, period: str) -> dict:
    portfolio = await _readable_portfolio(ctx, portfolio_id)
    return await performance.get_performance(ctx.session, portfolio.id, period)  # type: ignore[arg-type]


def _profile_dto(profile: RiskProfile | None) -> dict | None:
    if profile is None:
        return None
    return {
        "name": profile.name,
        "max_order_notional_eur": profile.max_order_notional_eur,
        "max_position_pct": profile.max_position_pct,
        "max_daily_loss_pct": profile.max_daily_loss_pct,
        "max_daily_loss_eur": profile.max_daily_loss_eur,
        "allowed_symbols": profile.allowed_symbols,
        "allowed_asset_classes": profile.allowed_asset_classes,
        "allowed_markets": profile.allowed_markets,
        "short_allowed": profile.short_allowed,
        "description": profile.description,
    }


async def _risk_limits(ctx: ToolCtx, portfolio_id: str | None) -> dict:
    portfolio = await _readable_portfolio(ctx, portfolio_id)
    profile = await effective_risk_profile(ctx.session, portfolio)
    v = await _valuation(ctx, portfolio)
    day_open = await today_day_open(ctx.session, portfolio, v.ts)

    current = limit = None
    if day_open is not None:
        # as risk.daily_loss.check_daily_loss: external cash flows are not losses
        flows = await net_cash_flows_eur(ctx.session, ctx.qs, portfolio.id, day_open.ts, v.ts)
        with localcontext() as c:
            c.prec = _PREC
            start = day_open.equity_eur
            current = _eur(start - v.equity_raw + flows)
            limits: list[Decimal] = []
            if profile is not None and profile.max_daily_loss_eur is not None:
                limits.append(profile.max_daily_loss_eur)
            if profile is not None and profile.max_daily_loss_pct is not None and start > 0:
                limits.append(start * profile.max_daily_loss_pct / 100)
            limit = _eur(min(limits)) if limits else None

    with localcontext() as c:
        c.prec = _PREC
        distance = _eur(v.equity_raw - v.maint_req_raw)
    return {
        "portfolio_id": portfolio.id,
        "status": portfolio.status,
        "reduce_only_reason": portfolio.reduce_only_reason,
        "reduce_only_until": portfolio.reduce_only_until,
        "leverage": portfolio.leverage,
        "maintenance_margin_pct": portfolio.maintenance_margin_pct,
        "warning_margin_pct": portfolio.warning_margin_pct,
        "profile": _profile_dto(profile),
        "margin": {
            "equity_eur": v.equity_eur,
            "maint_req_eur": v.maint_req_eur,
            "warn_req_eur": v.warn_req_eur,
            "buying_power_eur": v.buying_power_eur,
            "margin_level": v.margin_level(),
        },
        "distance_to_maintenance_eur": distance,
        "daily_loss": {
            "day_open_equity_eur": _eur(day_open.equity_eur) if day_open is not None else None,
            "current_eur": current,
            "limit_eur": limit,
        },
        "kill_switch": await is_kill_switch_on(ctx.session),
    }


def register(mcp: MCPServer) -> None:
    @mcp.tool(structured_output=False)
    async def list_portfolios() -> CallToolResult:
        """The portfolios this agent may use: [{id, name, base_currency, status,
        permission}] where permission is "read" or "trade" (trade implies read)."""
        return await run_tool("list_portfolios", {}, _list_portfolios)

    @mcp.tool(structured_output=False)
    async def get_portfolio(portfolio_id: str) -> CallToolResult:
        """Mark-to-market valuation of a portfolio (EUR, cents): cash, equity_eur,
        exposure_eur, margin requirements, buying_power_eur, margin_level, positions,
        stale_symbols, plus status, reduce_only_reason, leverage and the day P&L
        (day_pnl_eur / day_pnl_pct vs today's day_open equity, net of deposits and
        withdrawals; null before the day_open is taken)."""
        params = {"portfolio_id": portfolio_id}
        return await run_tool(
            "get_portfolio", params, lambda ctx: portfolio_snapshot(ctx, portfolio_id)
        )

    @mcp.tool(structured_output=False)
    async def get_positions(portfolio_id: str) -> CallToolResult:
        """Open positions: [{symbol, side ("long"|"short"), qty (signed), avg_price, mark,
        currency, market_value, market_value_eur, unrealized_pnl, unrealized_pnl_eur,
        stale}]."""
        params = {"portfolio_id": portfolio_id}
        return await run_tool("get_positions", params, lambda ctx: _positions(ctx, portfolio_id))

    @mcp.tool(structured_output=False)
    async def get_transactions(
        portfolio_id: str,
        from_ts: str | None = None,
        to_ts: str | None = None,
        type: str | list[str] | None = None,
        limit: int | str | None = 100,
        before_seq: int | str | None = None,
    ) -> CallToolResult:
        """Ledger journals touching this portfolio, newest first (seq descending):
        [{id, seq, type, created_at, reason, order_id, lines: [{asset, kind, quantity,
        unit_price, price_ccy}]}], only the lines of this portfolio's accounts.
        from_ts/to_ts: ISO 8601, inclusive. type: a journal type (DEPOSIT, WITHDRAWAL,
        TRADE, FEE, FX, INTEREST, DIVIDEND, CORPORATE_ACTION, LIQUIDATION, RESET,
        REVERSAL) or a list of them. limit: 1..500 (default 100). Page with
        before_seq = the last seq received."""
        params = {
            "portfolio_id": portfolio_id, "from_ts": from_ts, "to_ts": to_ts, "type": type,
            "limit": limit, "before_seq": before_seq,
        }
        return await run_tool(
            "get_transactions", params,
            lambda ctx: _transactions(ctx, portfolio_id, from_ts, to_ts, type, limit, before_seq),
        )

    @mcp.tool(structured_output=False)
    async def get_performance(portfolio_id: str, period: str = "1m") -> CallToolResult:
        """Equity series and statistics over a period (1d, 1w, 1m, 3m, 1y, all):
        {series: [{ts, equity_eur}], return_pct, max_drawdown_pct (0 or negative),
        sharpe (annualised, from daily day_open returns; null with < 3 points)}."""
        params = {"portfolio_id": portfolio_id, "period": period}
        return await run_tool(
            "get_performance", params, lambda ctx: _performance(ctx, portfolio_id, period)
        )

    @mcp.tool(structured_output=False)
    async def get_risk_limits(portfolio_id: str) -> CallToolResult:
        """Risk state: status, leverage, margin thresholds (%), the effective risk profile,
        current margin {equity_eur, maint_req_eur, warn_req_eur, buying_power_eur,
        margin_level}, distance_to_maintenance_eur, daily_loss {day_open_equity_eur,
        current_eur (net of deposits/withdrawals), limit_eur} and the global kill_switch."""
        params = {"portfolio_id": portfolio_id}
        return await run_tool(
            "get_risk_limits", params, lambda ctx: _risk_limits(ctx, portfolio_id)
        )
