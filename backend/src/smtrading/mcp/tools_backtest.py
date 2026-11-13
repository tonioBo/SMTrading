"""Backtest tools: run_backtest, get_backtest, list_backtests.

A backtest is a pure simulation of a long-only strategy on historical bars; it never
touches portfolios, orders or brokers. It is queued and run by the worker. Another agent's
backtest is FORBIDDEN. With portfolio_id the portfolio's fee schedule is used and `read`
permission on it is required. Limits: 2 queued or running per agent (RATE_LIMITED); at
most 5 years of daily bars or 90 days of intraday bars (RISK_LIMIT_EXCEEDED).
"""

from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.types import CallToolResult
from sqlalchemy import select

from smtrading.agents.service import require_permission
from smtrading.backtest.models import Backtest
from smtrading.backtest.runner import enqueue_backtest
from smtrading.common.errors import ErrorCode, SMTError
from smtrading.mcp.dto import parse_decimal, parse_int, parse_ts, parse_uuid
from smtrading.mcp.runtime import ToolCtx, run_tool

Num = str | int | float | None
STATUSES = ("queued", "running", "completed", "failed")


def _invalid(field: str, message: str) -> SMTError:
    return SMTError(ErrorCode.INVALID_ORDER, message, {"field": field})


def _view(bt: Backtest, *, full: bool) -> dict[str, Any]:
    out: dict[str, Any] = {
        "id": bt.id, "strategy": bt.strategy, "symbols": bt.symbols,
        "timeframe": bt.timeframe, "from_date": bt.start, "to_date": bt.end,
        "initial_cash": bt.initial_cash, "params": bt.params, "portfolio_id": bt.portfolio_id,
        "status": bt.status.value, "warnings": bt.warnings, "error": bt.error,
        "created_at": bt.created_at, "started_at": bt.started_at,
        "finished_at": bt.finished_at,
    }
    if full:
        out["result"] = bt.result
    else:
        out["metrics"] = (bt.result or {}).get("metrics")
    return out


def register(mcp: MCPServer) -> None:
    @mcp.tool(structured_output=False)
    async def run_backtest(
        strategy: str, symbols: list[str], from_date: str, to_date: str,
        timeframe: str = "1d", initial_cash: Num = "10000", params: dict | None = None,
        portfolio_id: str | None = None, slippage_bps: int | None = None,
    ) -> CallToolResult:
        """Queue a backtest of a long-only strategy (pure simulation, no orders):
        {backtest_id, status: "queued"}; poll get_backtest. strategy: sma_cross
        (fast=20, slow=50) | rsi_reversion (period=14, buy_below=30, sell_above=70) |
        breakout (lookback=20, exit_lookback=10) | buy_and_hold. Signals execute at the
        next bar's open with slippage; cash is split equally between symbols. With
        portfolio_id (read permission) the portfolio's fees apply, else the global rule.
        Slippage: slippage_bps, else the portfolio's, else 5 bps. At most 2 queued/running per agent, 5 years of daily or 90 days of intraday bars."""
        args = {"strategy": strategy, "symbols": symbols, "timeframe": timeframe,
                "from_date": from_date, "to_date": to_date, "initial_cash": initial_cash,
                "params": params, "portfolio_id": portfolio_id,
                "slippage_bps": slippage_bps}

        async def fn(ctx: ToolCtx) -> Any:
            if not isinstance(strategy, str):
                raise _invalid("strategy", "strategy must be a string")
            if not isinstance(symbols, list):
                raise _invalid("symbols", "symbols must be a list of symbols")
            if not isinstance(timeframe, str):
                raise _invalid("timeframe", "timeframe must be a string")
            start, end = parse_ts(from_date, "from_date"), parse_ts(to_date, "to_date")
            if start is None or end is None:
                raise _invalid("from_date", "from_date and to_date are required")
            cash = parse_decimal(initial_cash, "initial_cash")
            if cash is None:
                raise _invalid("initial_cash", "initial_cash is required")
            pid = parse_uuid(portfolio_id, "portfolio_id")
            if pid is not None:
                await require_permission(ctx.session, ctx.agent_id, pid, "read")
            bt = await enqueue_backtest(
                ctx.session, ctx.agent_id, strategy=strategy, symbols=symbols,
                timeframe=timeframe, start=start, end=end, initial_cash=cash,
                params={**(params or {}), **({} if slippage_bps is None else {"slippage_bps": slippage_bps})},
                portfolio_id=pid,
            )
            return {"backtest_id": bt.id, "status": bt.status.value}

        return await run_tool("run_backtest", args, fn)

    @mcp.tool(structured_output=False)
    async def get_backtest(backtest_id: str) -> CallToolResult:
        """Status and result of one of the agent's backtests. result (when completed):
        {metrics: {total_return_pct, cagr_pct, max_drawdown_pct, sharpe, win_rate_pct,
        trades, exposure_pct}, equity: [{ts, equity}] (at most 1000 points), trades:
        [{symbol, entry_ts, entry_price, exit_ts, exit_price, qty, pnl, fees}],
        warnings}. A failed backtest has `error`."""

        async def fn(ctx: ToolCtx) -> Any:
            bid = parse_uuid(backtest_id, "backtest_id")
            if bid is None:
                raise _invalid("backtest_id", "backtest_id is required")
            bt = await ctx.session.get(Backtest, bid)
            if bt is None:
                raise SMTError(ErrorCode.NOT_FOUND, "backtest not found",
                               {"backtest_id": str(bid)})
            if bt.agent_id != ctx.agent_id:
                raise SMTError(ErrorCode.FORBIDDEN, "backtest belongs to another agent")
            return _view(bt, full=True)

        return await run_tool("get_backtest", {"backtest_id": backtest_id}, fn)

    @mcp.tool(structured_output=False)
    async def list_backtests(status: str | None = None, limit: int = 20) -> CallToolResult:
        """The agent's backtests, newest first, without equity curve and trades (use
        get_backtest); `metrics` is set once completed. status: queued | running |
        completed | failed; limit 1-100 (default 20)."""

        async def fn(ctx: ToolCtx) -> Any:
            if status is not None and status not in STATUSES:
                raise _invalid("status", f"status must be one of {list(STATUSES)}")
            n = parse_int(limit, "limit", 1, 100) or 20
            q = select(Backtest).where(Backtest.agent_id == ctx.agent_id)
            if status is not None:
                q = q.where(Backtest.status == status)
            rows = (await ctx.session.execute(
                q.order_by(Backtest.created_at.desc(), Backtest.id).limit(n)
            )).scalars().all()
            return [_view(b, full=False) for b in rows]

        return await run_tool("list_backtests", {"status": status, "limit": limit}, fn)
