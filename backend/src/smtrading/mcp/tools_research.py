"""Strategy research tools: run_walk_forward, run_param_sweep, run_portfolio_backtest
(queued like run_backtest, run by the worker), get_research, list_research.

Runs only read historical bars and the fee schedule: no portfolio, order or broker is
touched. With portfolio_id the portfolio's fees and slippage apply (and, for the
portfolio backtest, its risk profile's daily-loss limit) and `read` permission on it is
required. Reading: the agent's own runs, plus those on a portfolio it can read (the CEO
reads the Quant's this way); any other run is FORBIDDEN. Limits in
`backtest.research_runner` (RATE_LIMITED / RISK_LIMIT_EXCEEDED).
"""

from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.types import CallToolResult
from sqlalchemy import or_, select

from smtrading.agents.models import Agent
from smtrading.agents.service import agent_portfolio_ids, get_permission, require_permission
from smtrading.backtest import research_runner as rr
from smtrading.backtest.models import ResearchKind, ResearchRun
from smtrading.backtest.research_runner import run_view
from smtrading.common.errors import ErrorCode, SMTError
from smtrading.mcp.dto import parse_decimal, parse_int, parse_ts, parse_uuid
from smtrading.mcp.instruments import ensure_instruments
from smtrading.mcp.runtime import ToolCtx, run_tool

Num = str | int | float | None
STATUSES = ("queued", "running", "completed", "failed")
KINDS = tuple(k.value for k in ResearchKind)


def _invalid(field: str, message: str) -> SMTError:
    return SMTError(ErrorCode.INVALID_ORDER, message, {"field": field})


async def _common(ctx: ToolCtx, symbols: Any, from_date: Any, to_date: Any,
                  initial_cash: Any, portfolio_id: Any, timeframe: Any, max_symbols: int):
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
    if len(symbols) <= max_symbols:  # over the limit: the enqueue refuses
        await ensure_instruments(ctx, symbols)
    return start, end, cash, pid


def _int_arg(v: Any, field: str) -> int:
    if isinstance(v, bool) or not isinstance(v, int):
        raise _invalid(field, f"{field} must be an integer")
    return v


def _queued(run: ResearchRun) -> dict[str, Any]:
    return {"research_id": run.id, "kind": run.kind.value, "status": run.status.value}


async def _readable(ctx: ToolCtx, run: ResearchRun) -> None:
    if run.agent_id == ctx.agent_id:
        return
    if run.portfolio_id is not None and await get_permission(
            ctx.session, ctx.agent_id, run.portfolio_id) is not None:
        return
    raise SMTError(ErrorCode.FORBIDDEN,
                   "research run of another agent on a portfolio this agent cannot read")


def register(mcp: MCPServer) -> None:
    @mcp.tool(structured_output=False)
    async def run_walk_forward(
        strategy: str, symbols: list[str], from_date: str, to_date: str,
        grid: dict | None = None, params: dict | None = None, windows: int = 5,
        is_pct: int = 70, mode: str = "rolling", metric: str = "sharpe",
        timeframe: str = "1d", initial_cash: Num = "10000", portfolio_id: str | None = None,
        slippage_bps: int | None = None,
    ) -> CallToolResult:
        """Queue a walk-forward analysis of a long-only strategy on historical bars (no
        orders): {research_id, kind, status: "queued"}; poll get_research. The period is
        cut into `windows` (2-10) in-sample/out-of-sample pairs: out-of-sample segments
        follow each other up to to_date, in-sample is is_pct % (50-90, default 70) of a
        window; mode rolling (in-sample slides) or anchored (in-sample starts at
        from_date). In each window the best combination of `grid` ({param: [values]},
        over the fixed `params`; empty: params only) by `metric` on the in-sample bars is
        run on the out-of-sample bars, which it never saw. metric: sharpe (default) |
        total_return_pct | cagr_pct | win_rate_pct | max_drawdown_pct (lower is better).
        Strategies, params, costs and execution as run_backtest. Result: windows [{window,
        is_from, is_to, oos_from, oos_to, best_params, is_metrics, oos_metrics}],
        oos_metrics (chained out-of-sample: total_return_pct, max_drawdown_pct, sharpe,
        trades, win_rate_pct, positive_windows_pct, mean_is_return_pct,
        mean_oos_return_pct, efficiency), stability {param_changes, stability_pct,
        most_common_params}, verdict (solida | fragile | da scartare | dati insufficienti)
        with reasons, oos_equity. Limits: 10 symbols, 50 grid combinations, 2 research
        runs queued/running per agent, 5 years of daily or 90 days of intraday bars."""
        args = {"strategy": strategy, "symbols": symbols, "from_date": from_date,
                "to_date": to_date, "grid": grid, "params": params, "windows": windows,
                "is_pct": is_pct, "mode": mode, "metric": metric, "timeframe": timeframe,
                "initial_cash": initial_cash, "portfolio_id": portfolio_id,
                "slippage_bps": slippage_bps}

        async def fn(ctx: ToolCtx) -> Any:
            if not isinstance(strategy, str):
                raise _invalid("strategy", "strategy must be a string")
            start, end, cash, pid = await _common(ctx, symbols, from_date, to_date,
                                                  initial_cash, portfolio_id, timeframe,
                                                  rr.MAX_WF_SYMBOLS)
            run = await rr.enqueue_walk_forward(
                ctx.session, ctx.agent_id, strategy=strategy, symbols=symbols,
                timeframe=timeframe, start=start, end=end, initial_cash=cash, params=params,
                grid=grid, windows=_int_arg(windows, "windows"),
                is_pct=_int_arg(is_pct, "is_pct"), mode=mode, metric=metric,
                slippage_bps=slippage_bps, portfolio_id=pid)
            return _queued(run)

        return await run_tool("run_walk_forward", args, fn)

    @mcp.tool(structured_output=False)
    async def run_param_sweep(
        strategy: str, symbols: list[str], from_date: str, to_date: str, grid: dict,
        params: dict | None = None, metric: str = "sharpe", sort: str = "metric",
        timeframe: str = "1d", initial_cash: Num = "10000", portfolio_id: str | None = None,
        slippage_bps: int | None = None,
    ) -> CallToolResult:
        """Queue a parameter sweep of a long-only strategy on historical bars (no orders):
        {research_id, kind, status: "queued"}; poll get_research. Every combination of
        `grid` ({param: [values]}, at most 20 values each, over the fixed `params`;
        combinations the strategy refuses, e.g. fast >= slow, are skipped) runs on the
        whole period. Result: rows [{rank, params, metrics, neighbours, neighbour_median,
        robust}] sorted by `metric` (sharpe default; also total_return_pct, cagr_pct,
        win_rate_pct, max_drawdown_pct lower is better) or with sort "robust" by `robust`
        = the median of the metric over the combination and its grid neighbours (one step
        in any parameter): prefer a plateau to an isolated peak (isolated_peak true when
        the best by metric is not the best by robustness); best_by_metric,
        best_by_robust; heatmap {x_param, y_param, x_values, y_values, z} with exactly 2
        varying parameters. Strategies, params, costs and execution as run_backtest.
        Limits: 200 combinations, 20 symbols, 2 research runs queued/running per agent."""
        args = {"strategy": strategy, "symbols": symbols, "from_date": from_date,
                "to_date": to_date, "grid": grid, "params": params, "metric": metric,
                "sort": sort, "timeframe": timeframe, "initial_cash": initial_cash,
                "portfolio_id": portfolio_id, "slippage_bps": slippage_bps}

        async def fn(ctx: ToolCtx) -> Any:
            if not isinstance(strategy, str):
                raise _invalid("strategy", "strategy must be a string")
            start, end, cash, pid = await _common(ctx, symbols, from_date, to_date,
                                                  initial_cash, portfolio_id, timeframe,
                                                  rr.MAX_SWEEP_SYMBOLS)
            run = await rr.enqueue_param_sweep(
                ctx.session, ctx.agent_id, strategy=strategy, symbols=symbols,
                timeframe=timeframe, start=start, end=end, initial_cash=cash, params=params,
                grid=grid, metric=metric, sort=sort, slippage_bps=slippage_bps,
                portfolio_id=pid)
            return _queued(run)

        return await run_tool("run_param_sweep", args, fn)

    @mcp.tool(structured_output=False)
    async def run_portfolio_backtest(
        sleeves: list[dict], from_date: str, to_date: str, sizing: dict | None = None,
        max_daily_loss_pct: Num = None, max_daily_loss_eur: Num = None,
        timeframe: str = "1d", initial_cash: Num = "10000", portfolio_id: str | None = None,
        slippage_bps: int | None = None,
    ) -> CallToolResult:
        """Queue a backtest of several long-only strategies sharing one capital (no
        orders): {research_id, kind, status: "queued"}; poll get_research. sleeves: [{strategy,
        params, symbols}] (at most 5, at most 20 strategy x symbol pairs; strategies and
        params as run_backtest). sizing: risk_pct (1: % of equity risked per entry at the
        stop), stop_pct (5: stop under the entry price, also enforced), max_positions
        (10, across sleeves), max_weight_pct (20: cap per position, % of equity); entries
        over a limit or without cash are refused and counted. Daily-loss breaker:
        max_daily_loss_pct / max_daily_loss_eur, else the risk profile of portfolio_id
        (read permission), else off; once the day's loss reaches it no new entries for
        the rest of the day and on the next bar. Costs and execution as run_backtest.
        Result: metrics (run_backtest's plus stops, breaker_trips, refused_*), equity
        [{ts, equity, drawdown_pct}], contribution per sleeve {pnl, contribution_pct,
        trades, win_rate_pct, fees}, correlation {labels, matrix} of the sleeves' daily
        returns, breaker_events, trades. At most 2 research runs queued/running per
        agent."""
        args = {"sleeves": sleeves, "from_date": from_date, "to_date": to_date,
                "sizing": sizing, "max_daily_loss_pct": max_daily_loss_pct,
                "max_daily_loss_eur": max_daily_loss_eur, "timeframe": timeframe,
                "initial_cash": initial_cash, "portfolio_id": portfolio_id,
                "slippage_bps": slippage_bps}

        async def fn(ctx: ToolCtx) -> Any:
            if not isinstance(sleeves, list):
                raise _invalid("sleeves", "sleeves must be a list of {strategy, params, symbols}")
            syms = [x for s in sleeves if isinstance(s, dict)
                    for x in (s.get("symbols") if isinstance(s.get("symbols"), list) else [])]
            start, end, cash, pid = await _common(ctx, syms, from_date, to_date,
                                                  initial_cash, portfolio_id, timeframe,
                                                  rr.pf.MAX_PAIRS)
            run = await rr.enqueue_portfolio_backtest(
                ctx.session, ctx.agent_id, sleeves=sleeves, timeframe=timeframe,
                start=start, end=end, initial_cash=cash, sizing=sizing,
                max_daily_loss_pct=max_daily_loss_pct,
                max_daily_loss_eur=max_daily_loss_eur, slippage_bps=slippage_bps,
                portfolio_id=pid)
            return _queued(run)

        return await run_tool("run_portfolio_backtest", args, fn)

    @mcp.tool(structured_output=False)
    async def get_research(research_id: str) -> CallToolResult:
        """Status and full result of a research run (run_walk_forward, run_param_sweep,
        run_portfolio_backtest): the agent's own, or one on a portfolio the agent can
        read (else FORBIDDEN). Read-only. A failed run has `error`."""

        async def fn(ctx: ToolCtx) -> Any:
            rid = parse_uuid(research_id, "research_id")
            if rid is None:
                raise _invalid("research_id", "research_id is required")
            run = await ctx.session.get(ResearchRun, rid)
            if run is None:
                raise SMTError(ErrorCode.NOT_FOUND, "research run not found",
                               {"research_id": str(rid)})
            await _readable(ctx, run)
            name = await ctx.session.scalar(select(Agent.name).where(Agent.id == run.agent_id))
            return run_view(run, name, full=True)

        return await run_tool("get_research", {"research_id": research_id}, fn)

    @mcp.tool(structured_output=False)
    async def list_research(kind: str | None = None, status: str | None = None,
                            limit: int = 20) -> CallToolResult:
        """Research runs the agent can read (its own and those on its portfolios), newest
        first, with a summary instead of the full result (use get_research): walk-forward
        verdict and oos_metrics, sweep best params, portfolio metrics. kind: walk_forward
        | param_sweep | portfolio; status: queued | running | completed | failed; limit
        1-100 (default 20). Read-only."""

        async def fn(ctx: ToolCtx) -> Any:
            if kind is not None and kind not in KINDS:
                raise _invalid("kind", f"kind must be one of {list(KINDS)}")
            if status is not None and status not in STATUSES:
                raise _invalid("status", f"status must be one of {list(STATUSES)}")
            n = parse_int(limit, "limit", 1, 100) or 20
            pids = await agent_portfolio_ids(ctx.session, ctx.agent_id)
            q = (select(ResearchRun, Agent.name)
                 .outerjoin(Agent, Agent.id == ResearchRun.agent_id)
                 .where(or_(ResearchRun.agent_id == ctx.agent_id,
                            ResearchRun.portfolio_id.in_(pids))))
            if kind is not None:
                q = q.where(ResearchRun.kind == kind)
            if status is not None:
                q = q.where(ResearchRun.status == status)
            rows = (await ctx.session.execute(
                q.order_by(ResearchRun.created_at.desc(), ResearchRun.id).limit(n))).all()
            return [run_view(r, name, full=False) for r, name in rows]

        return await run_tool("list_research", {"kind": kind, "status": status,
                                                "limit": limit}, fn)
