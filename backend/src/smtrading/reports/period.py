"""The data of a «Resoconto» (a campaign, or any date range and portfolios): everything is
read from what the other pages already compute, with their maths.

- Period: local days `start`..`end` (`settings.tz`), both included; nothing after `now`.
- Portfolios: those given (archived ones included: a concluded campaign), else every
  non-archived portfolio.
- Per portfolio (`equity_snapshots` of the period, `performance.service` maths): start and
  end equity in EUR (first and last snapshot of the period), change, `return_pct`,
  `max_drawdown_pct`, `sharpe` (day_open returns). External cash flows are not
  neutralised (as on the «Andamento» page).
- Total: the sums of the portfolios with data; `curve` one point per local day: the sum
  of each portfolio's latest equity up to the end of that day.
- Benchmarks SPY, QQQ, VWCE.DE (`goals.benchmark`, the samples of the beat_index goals):
  change over the period, `diff_pp` = total − benchmark, a daily series for the chart;
  None («dati non disponibili») when not sampled.
- Operations: the FIFO trades (`performance.strategies`) of the portfolios opened or
  closed in the period, realised P/L and costs in EUR at the current rate (gross;
  `net` = realised − costs); `fills` / `fees_eur`: the fills of the period and their costs.
- Strategies: `performance.agent_results._strategies` restricted to the period and the
  portfolios.
- Goals: the campaign's goals plus the per-portfolio goals of the portfolios (active ones),
  each over its period overlapping the report's (`goals.progress.period_progress`).
- Agent costs: `agent_cost_days` of the period (the whole team: costs are not per
  portfolio), USD.
- Lessons, team rules and notable decisions (strategy, risk_rule, allocation, veto) of
  the period, of the portfolios or of the whole team (no portfolio)."""

from datetime import date, datetime, timedelta
from decimal import Decimal, localcontext
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import false, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from smtrading.activity.models import Decision
from smtrading.agents.models import Agent
from smtrading.common.errors import ErrorCode, SMTError
from smtrading.common.money import quantize_money
from smtrading.engine.quotesource import QuoteSource
from smtrading.goals.benchmark import benchmark_change, benchmark_series, local_midnight
from smtrading.goals.models import BENCHMARKS, Goal
from smtrading.goals.periods import Period, goal_period
from smtrading.goals.progress import period_progress
from smtrading.paperclip.models import AgentCostDay
from smtrading.performance.agent_results import _strategies
from smtrading.performance.service import _max_drawdown_pct, _return_pct, _sharpe
from smtrading.performance.strategies import _fills, _money, _Rates, _sum, fifo_trades
from smtrading.portfolios.models import Portfolio, PortfolioStatus
from smtrading.risk.models import EquitySnapshot, SnapshotKind

MAX_DAYS = 3 * 366
MAX_TRADES = 200
MAX_DECISIONS = 60
NOTABLE_KINDS = ("strategy", "risk_rule", "allocation", "veto")
_PREC = 60
_Q2 = Decimal("0.01")


def _invalid(field: str, message: str) -> SMTError:
    return SMTError(ErrorCode.INVALID_ORDER, message, {"field": field})


def check_range(start: date, end: date) -> None:
    if end < start:
        raise _invalid("to", "la data di fine precede quella di inizio")
    if (end - start).days >= MAX_DAYS:
        raise _invalid("to", "un resoconto copre al massimo 3 anni")


def _eur(v: Decimal | None) -> Decimal | None:
    return None if v is None else quantize_money(v, "EUR")


def _pct2(v: Decimal | None) -> Decimal | None:
    return None if v is None else v.quantize(_Q2)


async def _portfolios(session: AsyncSession, ids: list[UUID] | None) -> list[Portfolio]:
    q = select(Portfolio).order_by(Portfolio.name)
    if ids is None:
        q = q.where(Portfolio.status != PortfolioStatus.archived)
    else:
        q = q.where(Portfolio.id.in_(ids))
    found = list(await session.scalars(q))
    if ids is not None and len(found) != len(set(ids)):
        missing = sorted(str(i) for i in set(ids) - {p.id for p in found})
        raise SMTError(ErrorCode.NOT_FOUND, "portafoglio non trovato",
                       {"field": "portfolio_ids", "portfolio_ids": missing})
    return found


async def _equity(session: AsyncSession, portfolios: list[Portfolio], start_ts: datetime,
                  end_ts: datetime, days: list[date], zone: ZoneInfo
                  ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    rows_by: dict[UUID, list] = {}
    out = []
    for p in portfolios:
        rows = (await session.execute(
            select(EquitySnapshot.ts, EquitySnapshot.kind, EquitySnapshot.equity_eur)
            .where(EquitySnapshot.portfolio_id == p.id, EquitySnapshot.ts >= start_ts,
                   EquitySnapshot.ts < end_ts)
            .order_by(EquitySnapshot.ts, EquitySnapshot.id))).all()
        rows_by[p.id] = rows
        item: dict[str, Any] = {
            "portfolio_id": p.id, "name": p.name, "base_currency": p.base_currency,
            "archived": p.status == PortfolioStatus.archived,
            "start_eur": None, "end_eur": None, "change_eur": None, "return_pct": None,
            "max_drawdown_pct": None, "sharpe": None, "first_ts": None, "last_ts": None}
        if rows:
            values = [eq for _, _, eq in rows]
            with localcontext() as ctx:
                ctx.prec = _PREC
                item.update(
                    start_eur=_eur(values[0]), end_eur=_eur(values[-1]),
                    change_eur=_eur(values[-1] - values[0]),
                    return_pct=_return_pct(values[0], values[-1]),
                    max_drawdown_pct=_max_drawdown_pct(values),
                    sharpe=_sharpe([eq for _, k, eq in rows if k == SnapshotKind.day_open]),
                    first_ts=rows[0][0], last_ts=rows[-1][0])
        out.append(item)

    with_data = [i for i in out if i["start_eur"] is not None]
    start = sum((i["start_eur"] for i in with_data), Decimal(0)) if with_data else None
    end = sum((i["end_eur"] for i in with_data), Decimal(0)) if with_data else None
    curve = []
    for day in days:
        limit = local_midnight(day + timedelta(days=1), zone)
        total, seen = Decimal(0), False
        for rows in rows_by.values():
            last = None
            for ts, _, eq in rows:
                if ts >= limit:
                    break
                last = eq
            if last is not None:
                total, seen = total + last, True
        if seen:
            curve.append({"day": day, "equity_eur": _eur(total)})
    with localcontext() as ctx:
        ctx.prec = _PREC
        total_item = {
            "start_eur": start, "end_eur": end,
            "change_eur": end - start if with_data else None,
            "return_pct": _return_pct(start, end) if with_data else None,
            "max_drawdown_pct": (_max_drawdown_pct([c["equity_eur"] for c in curve])
                                 if curve else None),
            "curve": curve,
        }
    return out, total_item


async def _benchmarks(session: AsyncSession, period: Period, now: datetime, zone: ZoneInfo,
                      total_pct: Decimal | None, days: list[date]) -> list[dict[str, Any]]:
    out = []
    for symbol in BENCHMARKS:
        change = await benchmark_change(session, symbol, period, now, zone)
        series = await benchmark_series(session, symbol, period, None, now, zone)
        daily: dict[date, Decimal] = {}
        for ts, pct in series:
            if pct is not None:
                daily[ts.astimezone(zone).date()] = pct
        points = [{"day": d, "pct": _pct2(daily[d])} for d in days if d in daily]
        out.append({
            "symbol": symbol, "label": symbol.removesuffix(".DE"),
            "change_pct": _pct2(change),
            "diff_pp": (_pct2(total_pct - change)
                        if total_pct is not None and change is not None else None),
            "series": points,
        })
    return out


async def _operations(session: AsyncSession, qs: QuoteSource, ids: list[UUID],
                      names: dict[UUID, str], start_ts: datetime, end_ts: datetime
                      ) -> dict[str, Any]:
    fills, _, _ = await _fills(session, ids)

    def within(ts: datetime | None) -> bool:
        return ts is not None and start_ts <= ts < end_ts

    rates = _Rates(qs)
    period_fills = [f for f in fills if within(f.ts)]
    fee_values = []
    for f in period_fills:
        fee_values.append(_sum([await rates.eur(a, c) for c, a in sorted(f.costs.items())]))
    trades = [t for t in fifo_trades(fills) if within(t.opened_at) or within(t.closed_at)]
    trades.sort(key=lambda t: (t.closed_at or t.opened_at), reverse=True)
    items, reals, costs = [], [], []
    for t in trades:
        closed = within(t.closed_at)
        real = await rates.eur(t.realized, t.currency) if t.exit_qty else Decimal(0)
        cost = _sum([await rates.eur(a, c) for c, a in sorted(t.costs.items())])
        if closed:
            reals.append(real)
            costs.append(cost)
        items.append({
            "portfolio": names.get(t.portfolio_id, ""), "symbol": t.symbol,
            "direction": t.direction, "opened_at": t.opened_at, "closed_at": t.closed_at,
            "qty": t.qty, "open_qty": t.open_qty, "currency": t.currency,
            "entry_price": t.entry_price, "exit_price": t.exit_price,
            "realized_pnl_eur": _money(real), "costs_eur": _money(cost),
            "net_pnl_eur": _money(None if None in (real, cost) else real - cost),
        })
    real_sum, cost_sum = _sum(reals), _sum(costs)
    return {
        "fills": len(period_fills),
        "fees_eur": _money(_sum(fee_values)),
        "trades_opened": sum(1 for t in trades if within(t.opened_at)),
        "trades_closed": len(reals),
        "realized_pnl_eur": _money(real_sum), "costs_eur": _money(cost_sum),
        "net_pnl_eur": _money(None if None in (real_sum, cost_sum) else real_sum - cost_sum),
        "trades": items[:MAX_TRADES], "trades_total": len(items),
    }


async def _goals(session: AsyncSession, ids: list[UUID], names: dict[UUID, str],
                 campaign_goal_ids: list[UUID], period: Period, now: datetime,
                 zone: ZoneInfo) -> list[dict[str, Any]]:
    q = select(Goal).where(or_(
        Goal.id.in_(campaign_goal_ids) if campaign_goal_ids else false(),
        Goal.active.is_(True) & (Goal.scope == "portfolio") & Goal.portfolio_id.in_(ids)))
    goals = sorted(await session.scalars(q), key=lambda g: (
        names.get(g.portfolio_id, ""), g.horizon != "custom", g.created_at, str(g.id)))
    today = now.astimezone(zone).date()
    out = []
    for g in goals:
        ref = min(period.end, today)
        gp = goal_period(g.horizon, g.start_date, g.end_date, g.recurring, ref)
        if gp.end < period.start or gp.start > period.end:
            continue
        p = await period_progress(session, g, gp, now, zone, names.get(g.portfolio_id),
                                  include_archived=True)
        p.pop("parts", None)
        p["closes_positions"] = g.closes_positions
        p["campaign"] = g.id in campaign_goal_ids
        out.append(p)
    return out


async def _costs(session: AsyncSession, start: date, end: date) -> dict[str, Any]:
    rows = (await session.execute(
        select(AgentCostDay.paperclip_agent_id, func.max(AgentCostDay.paperclip_agent_name),
               func.sum(AgentCostDay.api_usd), func.sum(AgentCostDay.subscription_usd),
               func.sum(AgentCostDay.other_usd), func.sum(AgentCostDay.runs))
        .where(AgentCostDay.day >= start, AgentCostDay.day <= end)
        .group_by(AgentCostDay.paperclip_agent_id))).all()
    agents = []
    cost = billed = Decimal(0)
    runs_total = 0
    for pid, name, api, sub, other, runs in rows:
        api, sub, other = Decimal(api or 0), Decimal(sub or 0), Decimal(other or 0)
        cost += api + sub + other
        billed += api + other
        runs_total += int(runs or 0)
        agents.append({"agent": name or pid, "cost_usd": (api + sub + other).quantize(_Q2),
                       "billed_usd": (api + other).quantize(_Q2), "runs": int(runs or 0)})
    agents.sort(key=lambda a: (-a["cost_usd"], a["agent"]))
    return {"cost_usd": cost.quantize(_Q2), "billed_usd": billed.quantize(_Q2),
            "runs": runs_total, "agents": agents}


async def _decisions(session: AsyncSession, ids: list[UUID], names: dict[UUID, str],
                     kinds: tuple[str, ...], start_ts: datetime, end_ts: datetime,
                     limit: int) -> list[dict[str, Any]]:
    rows = (await session.execute(
        select(Decision, Agent.name).outerjoin(Agent, Agent.id == Decision.agent_id)
        .where(Decision.kind.in_(kinds), Decision.created_at >= start_ts,
               Decision.created_at < end_ts,
               or_(Decision.portfolio_id.is_(None), Decision.portfolio_id.in_(ids)))
        .order_by(Decision.created_at.desc(), Decision.id.desc()).limit(limit))).all()
    out = []
    for d, agent in rows:
        det = d.details if isinstance(d.details, dict) else {}
        out.append({"id": d.id, "created_at": d.created_at, "kind": d.kind,
                    "agent": agent, "portfolio": names.get(d.portfolio_id),
                    "summary": d.summary, "outcome": d.outcome, "reason": d.reason,
                    "status": det.get("status") if isinstance(det.get("status"), str)
                    else None})
    return out


async def period_report(session: AsyncSession, qs: QuoteSource, *, start: date, end: date,
                        portfolio_ids: list[UUID] | None, now: datetime, zone: ZoneInfo,
                        campaign: dict[str, Any] | None = None,
                        campaign_goal_ids: list[UUID] | None = None) -> dict[str, Any]:
    """The report's data (Decimals, dates; the routes serialise or render it)."""
    check_range(start, end)
    portfolios = await _portfolios(session, portfolio_ids)
    ids = [p.id for p in portfolios]
    names = {p.id: p.name for p in portfolios}
    start_ts = local_midnight(start, zone)
    end_ts = min(local_midnight(end + timedelta(days=1), zone), now)
    period = Period(start, end)
    today = now.astimezone(zone).date()
    days = [start + timedelta(days=i) for i in range((min(end, today) - start).days + 1)]
    per_portfolio, total = await _equity(session, portfolios, start_ts, end_ts, days, zone)
    strategies, _ = (await _strategies(session, qs, start_ts, until=end_ts, portfolio_ids=ids)
                     if ids else ([], {}))
    lessons = await _decisions(session, ids, names, ("lesson", "team_rule"), start_ts,
                               end_ts, MAX_DECISIONS)
    return {
        "title": (f"Resoconto campagna «{campaign['name']}»" if campaign
                  else "Resoconto del periodo"),
        "campaign": campaign, "from": start, "to": end, "generated_at": now,
        "timezone": str(zone),
        "portfolios": per_portfolio, "total": total,
        "benchmarks": await _benchmarks(session, period, now, zone, total["return_pct"],
                                        days),
        "operations": await _operations(session, qs, ids, names, start_ts, end_ts),
        "strategies": strategies,
        "goals": await _goals(session, ids, names, campaign_goal_ids or [], period, now,
                              zone),
        "agent_costs": await _costs(session, start, end),
        "lessons": [x for x in lessons if x["kind"] == "lesson"],
        "team_rules": [x for x in lessons if x["kind"] == "team_rule"],
        "decisions": await _decisions(session, ids, names, NOTABLE_KINDS, start_ts, end_ts,
                                      MAX_DECISIONS),
    }

