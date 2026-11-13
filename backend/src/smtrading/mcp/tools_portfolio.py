"""Portfolio tools: list_portfolios, get_portfolio, get_positions, get_transactions,
get_performance, get_risk_limits. Every per-portfolio tool needs `read` on it.

`portfolio_snapshot` is shared with the portfolio:// resource (mcp/resources.py).
"""

from datetime import UTC, datetime
from decimal import ROUND_HALF_EVEN, Decimal, localcontext
from typing import Any
from uuid import UUID

from mcp.server.mcpserver import MCPServer
from mcp.types import CallToolResult
from sqlalchemy import select

from smtrading.agents.models import AgentPortfolio
from smtrading.agents.service import require_permission
from smtrading.common.errors import ErrorCode, SMTError
from smtrading.common.money import quantize_money
from smtrading.corporate.snapshots import local_date
from smtrading.ledger.models import Account, Journal, JournalLine, JournalType
from smtrading.mcp.dto import (
    parse_decimal,
    parse_enum,
    parse_uuid,
    position_valuation_dto,
    valuation_dto,
)
from smtrading.mcp.runtime import ToolCtx, run_tool
from smtrading.performance import service as performance
from smtrading.portfolios.models import Portfolio
from smtrading.risk.daily_loss import net_cash_flows_eur
from smtrading.risk.models import EquitySnapshot, RiskProfile, SnapshotKind
from smtrading.risk.profiles import effective_risk_profile
from smtrading.risk.settings import is_kill_switch_on
from smtrading.valuation.service import PortfolioValuation, value_portfolio

MAX_TRANSACTIONS = 500
_PREC = 60
_Q4 = Decimal("0.0001")


def _eur(amount: Decimal) -> Decimal:
    return quantize_money(amount, "EUR")


def _pct(value: Decimal) -> Decimal:
    return value.quantize(_Q4, rounding=ROUND_HALF_EVEN)


def _invalid(field: str, message: str) -> SMTError:
    return SMTError(ErrorCode.INVALID_ORDER, message, {"field": field})


def _required_uuid(v: str | None, field: str) -> UUID:
    parsed = parse_uuid(v, field)
    if parsed is None:
        raise _invalid(field, f"{field} is required")
    return parsed


def _parse_ts(v: str | None, field: str) -> datetime | None:
    """ISO 8601; a naive timestamp is taken as UTC."""
    if v is None:
        return None
    try:
        ts = datetime.fromisoformat(str(v))
    except ValueError:
        raise _invalid(field, f"{field} must be an ISO 8601 timestamp") from None
    return ts if ts.tzinfo else ts.replace(tzinfo=UTC)


def _parse_int(v: Any, field: str, lo: int, hi: int | None = None) -> int | None:
    d = parse_decimal(v, field)
    if d is None:
        return None
    if d != d.to_integral_value() or d < lo or (hi is not None and d > hi):
        bound = f"between {lo} and {hi}" if hi is not None else f">= {lo}"
        raise _invalid(field, f"{field} must be an integer {bound}")
    return int(d)


async def _readable_portfolio(ctx: ToolCtx, portfolio_id: str | None) -> Portfolio:
    pid = _required_uuid(portfolio_id, "portfolio_id")
    await require_permission(ctx.session, ctx.agent_id, pid, "read")
    portfolio = await ctx.session.get(Portfolio, pid)
    if portfolio is None:  # unreachable while agent_portfolios has its FK
        raise SMTError(ErrorCode.NOT_FOUND, "portfolio not found", {"portfolio_id": str(pid)})
    return portfolio


async def _day_open(ctx: ToolCtx, portfolio: Portfolio, now: datetime) -> EquitySnapshot | None:
    """Today's (portfolio-local) day_open snapshot, if taken; never created here."""
    return await ctx.session.scalar(
        select(EquitySnapshot).where(
            EquitySnapshot.portfolio_id == portfolio.id,
            EquitySnapshot.kind == SnapshotKind.day_open,
            EquitySnapshot.local_date == local_date(portfolio, now),
        )
    )


async def _valuation(ctx: ToolCtx, portfolio: Portfolio) -> PortfolioValuation:
    return await value_portfolio(ctx.session, ctx.qs, portfolio.id, redis=ctx.redis)


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


async def portfolio_snapshot(ctx: ToolCtx, portfolio_id: str | None) -> dict:
    """get_portfolio: valuation_dto + status, reduce_only_reason, leverage and the day
    P&L (equity - today's day_open equity - net deposits since it, EUR and %; None
    without a day_open). External cash flows are not P&L, as in risk.daily_loss."""
    portfolio = await _readable_portfolio(ctx, portfolio_id)
    valuation = await _valuation(ctx, portfolio)
    day_open = await _day_open(ctx, portfolio, valuation.ts)
    pnl = pct = None
    if day_open is not None:
        flows = await net_cash_flows_eur(
            ctx.session, ctx.qs, portfolio.id, day_open.ts, valuation.ts
        )
        with localcontext() as c:
            c.prec = _PREC
            raw = valuation.equity_raw - day_open.equity_eur - flows
            pnl = _eur(raw)
            if day_open.equity_eur > 0:
                pct = _pct(raw / day_open.equity_eur * 100)
    return {
        **valuation_dto(valuation),
        "status": portfolio.status,
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
    start, end = _parse_ts(from_ts, "from_ts"), _parse_ts(to_ts, "to_ts")
    types = _parse_types(type)
    n = _parse_int(limit, "limit", 1, MAX_TRANSACTIONS) or 100
    before = _parse_int(before_seq, "before_seq", 1)
    portfolio = await _readable_portfolio(ctx, portfolio_id)

    own_lines = (
        select(JournalLine.id)
        .join(Account, Account.id == JournalLine.account_id)
        .where(JournalLine.journal_id == Journal.id, Account.portfolio_id == portfolio.id)
    )
    stmt = select(Journal).where(own_lines.exists()).order_by(Journal.seq.desc()).limit(n)
    if start is not None:
        stmt = stmt.where(Journal.created_at >= start)
    if end is not None:
        stmt = stmt.where(Journal.created_at <= end)
    if types is not None:
        stmt = stmt.where(Journal.type.in_(types))
    if before is not None:
        stmt = stmt.where(Journal.seq < before)
    journals = list(await ctx.session.scalars(stmt))

    lines: dict[UUID, list[dict]] = {j.id: [] for j in journals}
    if journals:
        rows = await ctx.session.execute(
            select(JournalLine, Account.asset, Account.kind)
            .join(Account, Account.id == JournalLine.account_id)
            .where(JournalLine.journal_id.in_(lines), Account.portfolio_id == portfolio.id)
            .order_by(JournalLine.id)
        )
        for line, asset, kind in rows.all():
            lines[line.journal_id].append({
                "asset": asset, "kind": kind, "quantity": line.quantity,
                "unit_price": line.unit_price, "price_ccy": line.price_ccy,
            })
    return [
        {
            "id": j.id, "seq": j.seq, "type": j.type, "created_at": j.created_at,
            "reason": j.reason, "order_id": j.order_id, "lines": lines[j.id],
        }
        for j in journals
    ]


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
    }


async def _risk_limits(ctx: ToolCtx, portfolio_id: str | None) -> dict:
    portfolio = await _readable_portfolio(ctx, portfolio_id)
    profile = await effective_risk_profile(ctx.session, portfolio)
    v = await _valuation(ctx, portfolio)
    day_open = await _day_open(ctx, portfolio, v.ts)

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
        limit: float | str | None = 100,
        before_seq: float | str | None = None,
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
