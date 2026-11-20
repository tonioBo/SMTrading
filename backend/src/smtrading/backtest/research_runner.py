"""Research runs (walk-forward, parameter sweep, portfolio backtest): queueing, limits and
the worker job, on the backtests' lifecycle (`runner.BacktestRunner`: claim, timeout,
requeue of stale rows, completed / failed). Like a backtest a run only reads bars and the
fee schedule (and, for a portfolio backtest, the portfolio's risk profile): it never
touches the ledger, orders or brokers.

Limits per agent: at most MAX_ACTIVE runs queued or running (RATE_LIMITED, counted apart
from the backtests); the backtests' span limits (5 years of daily bars, 90 days of
intraday ones). Walk-forward: at most MAX_WF_SYMBOLS symbols and MAX_WF_COMBINATIONS
grid combinations; sweep: MAX_SWEEP_SYMBOLS and MAX_SWEEP_COMBINATIONS; portfolio:
`portfolio.MAX_SLEEVES` sleeves, `portfolio.MAX_PAIRS` strategy x symbol pairs
(RISK_LIMIT_EXCEEDED).

Costs: the fee schedule per symbol (portfolio's, else the global rule) and the slippage
(explicit `slippage_bps`, else the portfolio's, else 5 bps), as for a backtest. The
portfolio backtest's daily-loss breaker: explicit `max_daily_loss_pct` /
`max_daily_loss_eur`, else the portfolio's risk profile, else off.
"""

import asyncio
from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from smtrading.backtest import portfolio as pf
from smtrading.backtest import research, strategies
from smtrading.backtest.models import BacktestStatus, ResearchKind, ResearchRun
from smtrading.backtest.runner import BacktestRunner, _invalid, check_request, check_symbols
from smtrading.common.errors import ErrorCode, SMTError
from smtrading.fees.resolver import load_rules
from smtrading.instruments.service import canonical_symbol, get_instrument
from smtrading.marketdata.types import Timeframe
from smtrading.mcp.dto import to_jsonable
from smtrading.portfolios.models import Portfolio
from smtrading.risk.models import RiskProfile

MAX_ACTIVE = 2
MAX_WF_SYMBOLS = 10
MAX_WF_COMBINATIONS = 50
MAX_SWEEP_SYMBOLS = 20
MAX_SWEEP_COMBINATIONS = 200
TIMEOUT_S = 600.0


def _slippage(v: Any) -> int | None:
    if v is not None and (isinstance(v, bool) or not isinstance(v, int) or not 0 <= v <= 1000):
        raise _invalid("slippage_bps", "slippage_bps must be an integer 0-1000")
    return v


def _loss_limit(v: Any, field: str, hi: Decimal | None) -> Decimal | None:
    if v is None:
        return None
    if isinstance(v, bool) or not isinstance(v, int | float | str | Decimal):
        raise _invalid(field, f"{field} must be a positive number")
    try:
        d = Decimal(str(v))
    except ArithmeticError:
        raise _invalid(field, f"{field} must be a positive number") from None
    if not d.is_finite() or d <= 0 or (hi is not None and d > hi):
        raise _invalid(field, f"{field} must be a positive number"
                       + (f" up to {hi}" if hi is not None else ""))
    return d


async def _queue(session: AsyncSession, agent_id: UUID, *, kind: ResearchKind,
                 strategy: str | None, symbols: list[str], max_symbols: int, timeframe: str,
                 start: datetime, end: datetime, initial_cash: Decimal, spec: dict,
                 portfolio_id: UUID | None) -> ResearchRun:
    tf, names = await check_request(
        session, agent_id, symbols=symbols, timeframe=timeframe, start=start, end=end,
        initial_cash=initial_cash, max_symbols=max_symbols, model=ResearchRun,
        max_active=MAX_ACTIVE, what="research runs")
    run = ResearchRun(
        agent_id=agent_id, kind=kind, strategy=strategy, symbols=names,
        timeframe=tf.value, start=start, end=end, initial_cash=initial_cash,
        spec=to_jsonable(spec), portfolio_id=portfolio_id, status=BacktestStatus.queued,
        warnings=[])
    session.add(run)
    await session.flush()
    return run


async def enqueue_walk_forward(
    session: AsyncSession, agent_id: UUID, *, strategy: str, symbols: list[str],
    timeframe: str, start: datetime, end: datetime, initial_cash: Decimal,
    params: dict | None, grid: dict | None, windows: int, is_pct: int, mode: str,
    metric: str, slippage_bps: int | None, portfolio_id: UUID | None,
) -> ResearchRun:
    """Validate and queue a walk-forward (the caller has checked portfolio permission)."""
    research.check_metric(metric)
    research.check_windows(windows, is_pct, mode)
    research.expand_grid(strategy, params, grid or {}, limit=MAX_WF_COMBINATIONS)
    spec = {"params": strategies.validate_params(strategy, params), "grid": grid or {},
            "windows": windows, "is_pct": is_pct, "mode": mode, "metric": metric,
            "slippage_bps": _slippage(slippage_bps)}
    return await _queue(session, agent_id, kind=ResearchKind.walk_forward, strategy=strategy,
                        symbols=symbols, max_symbols=MAX_WF_SYMBOLS, timeframe=timeframe,
                        start=start, end=end, initial_cash=initial_cash, spec=spec,
                        portfolio_id=portfolio_id)


async def enqueue_param_sweep(
    session: AsyncSession, agent_id: UUID, *, strategy: str, symbols: list[str],
    timeframe: str, start: datetime, end: datetime, initial_cash: Decimal,
    params: dict | None, grid: dict | None, metric: str, sort: str,
    slippage_bps: int | None, portfolio_id: UUID | None,
) -> ResearchRun:
    research.check_metric(metric)
    if sort not in ("metric", "robust"):
        raise _invalid("sort", "sort must be metric or robust")
    if not grid:
        raise _invalid("grid", "grid must list at least one parameter with its values")
    research.expand_grid(strategy, params, grid, limit=MAX_SWEEP_COMBINATIONS)
    spec = {"params": strategies.validate_params(strategy, params), "grid": grid,
            "metric": metric, "sort": sort, "slippage_bps": _slippage(slippage_bps)}
    return await _queue(session, agent_id, kind=ResearchKind.param_sweep, strategy=strategy,
                        symbols=symbols, max_symbols=MAX_SWEEP_SYMBOLS, timeframe=timeframe,
                        start=start, end=end, initial_cash=initial_cash, spec=spec,
                        portfolio_id=portfolio_id)


async def enqueue_portfolio_backtest(
    session: AsyncSession, agent_id: UUID, *, sleeves: list[dict], timeframe: str,
    start: datetime, end: datetime, initial_cash: Decimal, sizing: dict | None,
    max_daily_loss_pct: Any, max_daily_loss_eur: Any, slippage_bps: int | None,
    portfolio_id: UUID | None,
) -> ResearchRun:
    checked = pf.validate_sleeves(sleeves)
    for s in checked:
        s["symbols"] = list(dict.fromkeys(canonical_symbol(x) for x in s["symbols"]))
    checked = pf.validate_sleeves(checked)  # the pair limit again, after deduplication
    size = pf.validate_sizing(sizing)
    symbols = check_symbols([x for s in checked for x in s["symbols"]], pf.MAX_PAIRS)
    spec = {"sleeves": checked, "sizing": size,
            "max_daily_loss_pct": _loss_limit(max_daily_loss_pct, "max_daily_loss_pct",
                                              Decimal(100)),
            "max_daily_loss_eur": _loss_limit(max_daily_loss_eur, "max_daily_loss_eur", None),
            "slippage_bps": _slippage(slippage_bps)}
    return await _queue(session, agent_id, kind=ResearchKind.portfolio, strategy=None,
                        symbols=symbols, max_symbols=pf.MAX_PAIRS, timeframe=timeframe,
                        start=start, end=end, initial_cash=initial_cash, spec=spec,
                        portfolio_id=portfolio_id)


def _num(v: Any) -> Decimal | None:
    return None if v is None else Decimal(str(v))


def run_summary(run: ResearchRun) -> dict[str, Any] | None:
    """walk-forward: verdict, oos_metrics, stability_pct; sweep: best params and the top
    row; portfolio: metrics. None until completed."""
    r = run.result
    if not isinstance(r, dict):
        return None
    if run.kind == ResearchKind.walk_forward:
        return {"verdict": r.get("verdict"), "oos_metrics": r.get("oos_metrics"),
                "stability_pct": (r.get("stability") or {}).get("stability_pct")}
    if run.kind == ResearchKind.param_sweep:
        top = (r.get("rows") or [None])[0]
        return {"best_by_metric": r.get("best_by_metric"),
                "best_by_robust": r.get("best_by_robust"),
                "isolated_peak": r.get("isolated_peak"),
                "top": top}
    return {"metrics": r.get("metrics")}


def run_view(run: ResearchRun, agent_name: str | None, *, full: bool) -> dict[str, Any]:
    """A run as the MCP tools and the portal show it: `result` (full) or `summary`."""
    out: dict[str, Any] = {
        "id": run.id, "kind": run.kind.value, "agent": agent_name,
        "strategy": run.strategy, "symbols": run.symbols, "timeframe": run.timeframe,
        "from_date": run.start, "to_date": run.end, "initial_cash": run.initial_cash,
        "spec": run.spec, "portfolio_id": run.portfolio_id, "status": run.status.value,
        "warnings": run.warnings, "error": run.error, "created_at": run.created_at,
        "started_at": run.started_at, "finished_at": run.finished_at,
    }
    if full:
        out["result"] = run.result
    else:
        out["summary"] = run_summary(run)
    return out


class ResearchRunner(BacktestRunner):
    """BacktestRunner's loop over `research_runs`, with the research engines."""

    model = ResearchRun
    name = "research run"
    no_fee_note = "computed without fees"

    def __init__(self, sessionmaker, router, *, timeout_s: float = TIMEOUT_S, **kw) -> None:
        super().__init__(sessionmaker, router, timeout_s=timeout_s, **kw)

    async def _compute(self, run_id: UUID) -> tuple[dict, list[str]]:
        async with self._sm() as s:
            run = await s.get(ResearchRun, run_id)
            assert run is not None
            instruments = {n: await get_instrument(s, n) for n in run.symbols}
            rules = await load_rules(s)
            portfolio = await s.get(Portfolio, run.portfolio_id) if run.portfolio_id else None
            profile = (await s.get(RiskProfile, portfolio.risk_profile_id)
                       if portfolio is not None and portfolio.risk_profile_id else None)
            kind, strategy, spec = run.kind, run.strategy, dict(run.spec or {})
            cash, pid, tf = run.initial_cash, run.portfolio_id, Timeframe(run.timeframe)
            start, end = run.start, run.end
        portfolio_slip = portfolio.slippage_bps if portfolio else None
        slippage = next(v for v in (spec.get("slippage_bps"), portfolio_slip,
                                    self._slippage_bps) if v is not None)
        warnings: list[str] = []
        fee_rules = await self._fee_rules(rules, instruments, pid, warnings)
        bars = {sym: await self._router.get_bars(sym, tf, start, end) for sym in instruments}
        cfg = {"initial_cash": cash, "fee_rule": fee_rules, "slippage_bps": slippage,
               "lot_by_symbol": {n: i.lot_size for n, i in instruments.items()}}
        if kind == ResearchKind.walk_forward:
            result = await asyncio.to_thread(
                research.run_walk_forward, bars, strategy, spec["params"], spec["grid"],
                windows=spec["windows"], is_pct=spec["is_pct"], mode=spec["mode"],
                metric=spec["metric"], max_combinations=MAX_WF_COMBINATIONS, **cfg)
        elif kind == ResearchKind.param_sweep:
            result = await asyncio.to_thread(
                research.run_param_sweep, bars, strategy, spec["params"], spec["grid"],
                metric=spec["metric"], sort=spec["sort"],
                max_combinations=MAX_SWEEP_COMBINATIONS, **cfg)
        elif kind == ResearchKind.portfolio:
            loss_pct = _num(spec.get("max_daily_loss_pct"))
            loss_eur = _num(spec.get("max_daily_loss_eur"))
            source = "explicit" if loss_pct is not None or loss_eur is not None else None
            if source is None and profile is not None and (
                    profile.max_daily_loss_pct or profile.max_daily_loss_eur):
                loss_pct, loss_eur = profile.max_daily_loss_pct, profile.max_daily_loss_eur
                source = "risk_profile"
            result = await asyncio.to_thread(
                pf.run_portfolio_backtest, bars, spec["sleeves"], sizing=spec["sizing"],
                max_daily_loss_pct=loss_pct, max_daily_loss_eur=loss_eur, **cfg)
            result["daily_loss_source"] = source
        else:  # pragma: no cover - the enum is closed
            raise SMTError(ErrorCode.INVALID_ORDER, f"unknown research kind {kind}")
        warnings += result.pop("warnings")
        result["slippage_bps"] = slippage
        result["fee_rule_ids"] = {sym: str(r.id) for sym, r in fee_rules.items()}
        return to_jsonable(result), warnings


__all__ = ["MAX_ACTIVE", "MAX_SWEEP_COMBINATIONS", "MAX_SWEEP_SYMBOLS",
           "MAX_WF_COMBINATIONS", "MAX_WF_SYMBOLS", "ResearchRunner",
           "enqueue_param_sweep", "enqueue_portfolio_backtest", "enqueue_walk_forward",
           "run_summary", "run_view"]
