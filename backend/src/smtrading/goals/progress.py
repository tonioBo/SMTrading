"""The progress of the goals (the maths of the former `telegram.weekly_goal`, for every
horizon and scope).

The equity of a goal is the sum, over its portfolios (scope total: the live ones; scope
portfolio: that one, unless archived), of the equity in EUR of the `equity_snapshots`:
- start of a portfolio: its `day_open` of the period's first day, else its earliest
  snapshot within the period (a portfolio created later, or no snapshot at the very
  start);
- current: its latest snapshot up to now (or up to the end of a period already over).
A portfolio without a snapshot in the period is left out of both sums. Nothing is valued
here (the worker writes a snapshot every 5 minutes).

Status (`status_of`): «non iniziato» before the period or without data yet; kind min:
«raggiunto» once pct ≥ target, «in ritardo» while pct < target × elapsed − 0,5 pp,
else «in linea»; kind cap: «oltre il tetto» once pct > target, else «in linea»; kind
max_loss (loss_pct = max(0, −pct), target = the limit): «superato» once loss ≥ target,
«vicino al limite» once loss ≥ 80% of it, else «nel limite»; kind beat_index (diff_pp =
pct − benchmark_pct, target = the margin in points; «non iniziato» without the
benchmark's data): «avanti» once diff ≥ target, «indietro» while diff < target −
0,5 pp, else «in linea». A period over (goals that do not recur) is «raggiunto» /
«oltre il tetto» / «superato» / «avanti» as above, else «concluso».

`target_eur` is the equity matching the target: start × (1 + target) for min and cap,
the floor start × (1 − limit) for max_loss, start × (1 + benchmark + margin) for
beat_index; `gap_eur` = target_eur − current_eur.

`parts` (admin only: `get_goals` leaves it out) is each counted portfolio's share, by
name: `change_eur` = its current − its start, `contribution_pct` = that change over the
goal's whole start × 100 (the points it adds to `pct`; None when `pct` is). Rounded to
cents by largest remainder, so they add up exactly to current_eur − start_eur and to
`pct`; empty without data.

Every figure leaves `goal_progress` as a Decimal rounded to cents (None without data); the
routes turn them into strings."""

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import ROUND_HALF_EVEN, Decimal
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from smtrading.goals.benchmark import benchmark_change, local_midnight
from smtrading.goals.models import HORIZONS, Goal
from smtrading.goals.periods import Period, elapsed_pct, goal_period, previous_period
from smtrading.goals.service import portfolio_labels
from smtrading.jobs.runs import run_job_once
from smtrading.portfolios.models import Portfolio, live_portfolio_ids
from smtrading.risk.models import EquitySnapshot, SnapshotKind

PERIOD_END_JOB = "goals.period_end"
END_LOOKBACK = timedelta(days=7)  # a period end not announced within a week is dropped
BEHIND_MARGIN_PP = Decimal("0.5")
NEAR_SHARE = Decimal("0.8")  # max_loss: «vicino al limite» from 80% of the limit
_Q2 = Decimal("0.01")

REACHED, ON_TRACK, BEHIND, NOT_STARTED, OVER, ABOVE_CAP = (
    "raggiunto", "in linea", "in ritardo", "non iniziato", "concluso", "oltre il tetto")
WITHIN, NEAR_LIMIT, BREACHED = "nel limite", "vicino al limite", "superato"
AHEAD, LAGGING = "avanti", "indietro"
STATUSES = (REACHED, ON_TRACK, BEHIND, NOT_STARTED, OVER, ABOVE_CAP, WITHIN, NEAR_LIMIT,
            BREACHED, AHEAD, LAGGING)


# -- pure maths ------------------------------------------------------------------------


def goal_target(pct: Decimal, start: Decimal) -> Decimal:
    """The equity to reach: `start` grown by `pct` percent."""
    return start * (1 + pct / 100)


def kind_target(kind: str, target: Decimal, start: Decimal,
                benchmark_pct: Decimal | None) -> Decimal | None:
    """`target_eur` of a goal (see the module docstring)."""
    if kind == "max_loss":
        return goal_target(-target, start)
    if kind == "beat_index":
        return None if benchmark_pct is None else goal_target(benchmark_pct + target, start)
    return goal_target(target, start)


def loss_of(pct: Decimal | None) -> Decimal | None:
    return None if pct is None else max(Decimal(0), -pct)


def goal_change_pct(start: Decimal, current: Decimal) -> Decimal | None:
    """The change in percent, or None without a positive starting equity."""
    if start <= 0:
        return None
    return (current / start - 1) * 100


def status_of(kind: str, pct: Decimal | None, target: Decimal, elapsed: Decimal, *,
              started: bool, ended: bool, benchmark_pct: Decimal | None = None) -> str:
    """The status of a goal (see the module docstring); `elapsed` in percent."""
    if kind == "beat_index" and benchmark_pct is None:
        pct = None
    if not started or pct is None:
        return OVER if ended and started else NOT_STARTED
    if kind == "max_loss":
        loss = max(Decimal(0), -pct)
        if loss >= target:
            return BREACHED
        if ended:
            return OVER
        return NEAR_LIMIT if loss >= target * NEAR_SHARE else WITHIN
    if kind == "beat_index":
        diff = pct - benchmark_pct
        if diff >= target:
            return AHEAD
        if ended:
            return OVER
        return LAGGING if diff < target - BEHIND_MARGIN_PP else ON_TRACK
    if kind == "cap":
        if pct > target:
            return ABOVE_CAP
        return OVER if ended else ON_TRACK
    if pct >= target:
        return REACHED
    if ended:
        return OVER
    if pct < target * elapsed / 100 - BEHIND_MARGIN_PP:
        return BEHIND
    return ON_TRACK


def round_cents(value: Decimal | None) -> Decimal | None:
    return None if value is None else value.quantize(_Q2, rounding=ROUND_HALF_EVEN)


def apportion(values: list[Decimal], total: Decimal) -> list[Decimal]:
    """`values` rounded to cents so that they add up to `total` (itself in cents and
    close to their sum): the leftover cents go, one each, to the values the rounding
    moved furthest the other way (largest remainder)."""
    rounded = [v.quantize(_Q2, rounding=ROUND_HALF_EVEN) for v in values]
    cents = int((total - sum(rounded, Decimal(0))) / _Q2)
    if not cents or not values:
        return [r if r else abs(r) for r in rounded]
    sign = 1 if cents > 0 else -1
    order = sorted(range(len(values)), key=lambda i: sign * (values[i] - rounded[i]),
                   reverse=True)
    for k in range(abs(cents)):
        rounded[order[k % len(order)]] += sign * _Q2
    return [r if r else abs(r) for r in rounded]  # no «-0.00»


def goal_parts(weeks: list["PortfolioPeriod"], names: dict[UUID, str],
               pct: Decimal | None) -> list[dict[str, Any]]:
    """The `parts` of a progress (see the module docstring), `pct` = its rounded pct."""
    if not weeks:
        return []
    start = sum((w.start for w in weeks), Decimal(0))
    current = sum((w.current for w in weeks), Decimal(0))
    changes = apportion([w.current - w.start for w in weeks],
                        round_cents(current) - round_cents(start))
    shares: list[Decimal | None] = [None] * len(weeks)
    if pct is not None and start > 0:
        shares = apportion([(w.current - w.start) / start * 100 for w in weeks], pct)
    parts = [{"portfolio_id": w.portfolio_id, "name": names.get(w.portfolio_id, ""),
              "change_eur": c, "contribution_pct": s}
             for w, c, s in zip(weeks, changes, shares, strict=True)]
    return sorted(parts, key=lambda p: (p["name"].casefold(), str(p["portfolio_id"])))


# -- snapshots -------------------------------------------------------------------------


@dataclass(frozen=True)
class PortfolioPeriod:
    portfolio_id: UUID
    start: Decimal
    current: Decimal
    as_of: datetime  # ts of the snapshot giving `current`


async def portfolio_periods(session: AsyncSession, period: Period, now: datetime,
                            zone: ZoneInfo,
                            portfolio_ids: list[UUID] | None = None) -> list[PortfolioPeriod]:
    """(start, current) of every live portfolio (or of the live ones of `portfolio_ids`)
    with a snapshot in `period` up to `now`."""
    start_ts = local_midnight(period.start, zone)
    end_ts = local_midnight(period.end + timedelta(days=1), zone)
    if now < start_ts:
        return []
    ids_stmt = live_portfolio_ids()
    if portfolio_ids is not None:
        ids_stmt = ids_stmt.where(Portfolio.id.in_(portfolio_ids))
    out = []
    for pid in list(await session.scalars(ids_stmt.order_by(Portfolio.id))):
        in_period = (select(EquitySnapshot.equity_eur, EquitySnapshot.ts)
                     .where(EquitySnapshot.portfolio_id == pid, EquitySnapshot.ts <= now,
                            EquitySnapshot.ts < end_ts))
        start = await session.scalar(
            select(EquitySnapshot.equity_eur)
            .where(EquitySnapshot.portfolio_id == pid,
                   EquitySnapshot.kind == SnapshotKind.day_open,
                   EquitySnapshot.local_date == period.start, EquitySnapshot.ts <= now))
        if start is None:
            start = (await session.execute(
                in_period.where(EquitySnapshot.ts >= start_ts)
                .order_by(EquitySnapshot.ts, EquitySnapshot.id).limit(1))).scalar()
        if start is None:
            continue
        row = (await session.execute(
            in_period.order_by(EquitySnapshot.ts.desc(), EquitySnapshot.id.desc())
            .limit(1))).first()
        if row is None:  # only a day_open stamped before its period (never in practice)
            continue
        out.append(PortfolioPeriod(pid, start, row[0], row[1]))
    return out


# -- progress --------------------------------------------------------------------------


async def period_progress(session: AsyncSession, goal: Goal, period: Period, now: datetime,
                          zone: ZoneInfo, portfolio_name: str | None) -> dict[str, Any]:
    """The progress of `goal` over `period` at `now` (the Progress of the contract, with
    Decimals, dates and datetimes)."""
    local = now.astimezone(zone)
    today = local.date()
    weeks = await portfolio_periods(
        session, period, now, zone,
        None if goal.scope == "total" else [goal.portfolio_id])
    target = goal.target_pct
    start = sum((w.start for w in weeks), Decimal(0)) if weeks else None
    current = sum((w.current for w in weeks), Decimal(0)) if weeks else None
    pct = goal_change_pct(start, current) if weeks else None
    bench = (await benchmark_change(session, goal.benchmark, period, now, zone)
             if goal.kind == "beat_index" and goal.benchmark else None)
    target_eur = kind_target(goal.kind, target, start, bench) if weeks else None
    elapsed = elapsed_pct(period, local)
    pct_r, bench_r = round_cents(pct), round_cents(bench)
    labels = await portfolio_labels(session, {w.portfolio_id for w in weeks})
    names = {pid: name for pid, (name, _) in labels.items()}
    return {
        "goal_id": goal.id, "name": goal.name, "horizon": goal.horizon, "scope": goal.scope,
        "portfolio_id": goal.portfolio_id, "portfolio_name": portfolio_name,
        "kind": goal.kind, "period_start": period.start, "period_end": period.end,
        "start_eur": round_cents(start), "current_eur": round_cents(current),
        "target_eur": round_cents(target_eur), "pct": pct_r, "target_pct": target,
        "gap_eur": (round_cents(target_eur - current)
                    if target_eur is not None and current is not None else None),
        "elapsed_pct": elapsed,
        "status": status_of(goal.kind, pct, target, elapsed, started=today >= period.start,
                            ended=today > period.end, benchmark_pct=bench),
        "as_of": max(w.as_of for w in weeks) if weeks else None,
        "benchmark": goal.benchmark, "benchmark_pct": bench_r,
        "diff_pp": pct_r - bench_r if pct_r is not None and bench_r is not None else None,
        "loss_pct": loss_of(pct_r) if goal.kind == "max_loss" else None,
        "parts": goal_parts(weeks, names, pct_r),
    }


def _order(goal: Goal) -> tuple:
    return (HORIZONS.index(goal.horizon), goal.scope != "total", goal.name.casefold(),
            goal.created_at or datetime.min.replace(tzinfo=UTC), str(goal.id))


async def visible_goals(session: AsyncSession, *, portfolio_ids: set[UUID] | None = None
                        ) -> list[tuple[Goal, str | None]]:
    """The active goals with the name of their portfolio: the total ones and the ones of
    live portfolios (of `portfolio_ids` only, when given), in display order."""
    goals = list(await session.scalars(select(Goal).where(Goal.active.is_(True))))
    names = await portfolio_labels(session, {g.portfolio_id for g in goals if g.portfolio_id})
    out = []
    for g in goals:
        if g.scope == "portfolio":
            name, archived = names.get(g.portfolio_id, (None, True))
            if archived or (portfolio_ids is not None and g.portfolio_id not in portfolio_ids):
                continue
            out.append((g, name))
        else:
            out.append((g, None))
    return sorted(out, key=lambda item: _order(item[0]))


def current_period(goal: Goal, today: date) -> Period:
    return goal_period(goal.horizon, goal.start_date, goal.end_date, goal.recurring, today)


async def goal_progress(session: AsyncSession, goal: Goal, now: datetime, zone: ZoneInfo,
                        portfolio_name: str | None = None) -> dict[str, Any]:
    """The progress of `goal` in its current period."""
    period = current_period(goal, now.astimezone(zone).date())
    return await period_progress(session, goal, period, now, zone, portfolio_name)


async def active_progress(session: AsyncSession, now: datetime, zone: ZoneInfo, *,
                          portfolio_ids: set[UUID] | None = None) -> list[dict[str, Any]]:
    """The progress of the active goals in their current period (`visible_goals`)."""
    return [await goal_progress(session, g, now, zone, name)
            for g, name in await visible_goals(session, portfolio_ids=portfolio_ids)]


# -- Telegram lines --------------------------------------------------------------------


def _number(value: Decimal) -> str:
    """Italian digits with 2 decimals, no sign: `1.004,20`."""
    q = abs(value.quantize(_Q2, rounding=ROUND_HALF_EVEN))
    return f"{q:,.2f}".replace(",", "_").replace(".", ",").replace("_", ".")


def _signed(value: Decimal) -> str:
    q = value.quantize(_Q2, rounding=ROUND_HALF_EVEN)
    return ("+" if q > 0 else "-" if q < 0 else "") + _number(q)


def _goal_pct(pct: Decimal) -> str:
    return "+" + format(pct.normalize(), "f").replace(".", ",") + "%"


def _points(value: Decimal) -> str:
    return format(value.normalize(), "f").replace(".", ",")


def _head(p: dict[str, Any]) -> str:
    if p["kind"] == "max_loss":
        head = f"Obiettivo {p['name']} perdita max {_points(p['target_pct'])}%"
    elif p["kind"] == "beat_index":
        head = f"Obiettivo {p['name']} meglio di {p['benchmark']}"
        if p["target_pct"] > 0:
            head += f" di {_points(p['target_pct'])} punti"
    else:
        head = f"Obiettivo {p['name']} {_goal_pct(p['target_pct'])}"
    if p["kind"] == "cap":
        head += " (tetto)"
    if p["portfolio_name"]:
        head += f" · {p['portfolio_name']}"
    return head


def progress_line(p: dict[str, Any], today: date) -> str:
    """«🎯 Obiettivo settimanale +2%: 1.000,00 → 1.020,00 € — attuale 1.004,20 €
    (+0,42%) · in linea» (pure, from a `period_progress`); before the period «dal
    <date>», without data «dati del periodo non ancora disponibili»."""
    head = f"🎯 {_head(p)}"
    if today < p["period_start"]:
        return f"{head}: dal {p['period_start']:%d/%m/%Y}"
    if p["pct"] is None:
        return f"{head}: dati del periodo non ancora disponibili"
    if p["kind"] == "max_loss":
        return (f"{head}: da {_number(p['start_eur'])} € — attuale "
                f"{_number(p['current_eur'])} € ({_signed(p['pct'])}%) · {p['status']}")
    if p["kind"] == "beat_index":
        if p["benchmark_pct"] is None:
            return f"{head}: dati dell'indice non ancora disponibili"
        return (f"{head}: {_signed(p['pct'])}% contro {_signed(p['benchmark_pct'])}% "
                f"({_signed(p['diff_pp'])} punti) · {p['status']}")
    return (f"{head}: {_number(p['start_eur'])} → {_number(p['target_eur'])} € — attuale "
            f"{_number(p['current_eur'])} € ({_signed(p['pct'])}%) · {p['status']}")


def end_line(p: dict[str, Any]) -> str:
    """«🏁 Obiettivo settimanale: raggiunto (+2,10% su +2%)» / «non raggiunto (…)» for a
    period just over (pure); kind cap: «tetto rispettato» / «tetto superato»; max_loss:
    «limite rispettato» / «limite superato» (from the status, so from the unrounded
    loss); beat_index: «<indice> battuto» / «non battuto»; without data «nessun dato del
    periodo (obiettivo|tetto|limite …)»."""
    name = p["name"] + (f" · {p['portfolio_name']}" if p["portfolio_name"] else "")
    target = _goal_pct(p["target_pct"])
    if p["kind"] == "beat_index":
        if p["pct"] is None or p["benchmark_pct"] is None:
            return f"🏁 Obiettivo {name}: nessun dato del periodo ({p['benchmark']})"
        word = "battuto" if p["diff_pp"] >= p["target_pct"] else "non battuto"
        return (f"🏁 Obiettivo {name}: {p['benchmark']} {word} ({_signed(p['pct'])}% "
                f"contro {_signed(p['benchmark_pct'])}%)")
    if p["pct"] is None:
        what = {"max_loss": f"limite -{_points(p['target_pct'])}%",
                "cap": f"tetto {target}"}.get(p["kind"], f"obiettivo {target}")
        return f"🏁 Obiettivo {name}: nessun dato del periodo ({what})"
    if p["kind"] == "max_loss":
        word = "limite superato" if p["status"] == BREACHED else "limite rispettato"
        return (f"🏁 Obiettivo {name}: {word} ({_signed(p['pct'])}%, limite "
                f"-{_points(p['target_pct'])}%)")
    if p["kind"] == "cap":
        word = "tetto rispettato" if p["pct"] <= p["target_pct"] else "tetto superato"
    else:
        word = "raggiunto" if p["pct"] >= p["target_pct"] else "non raggiunto"
    return f"🏁 Obiettivo {name}: {word} ({_signed(p['pct'])}% su {target})"


async def summary_lines(session: AsyncSession, now: datetime, zone: ZoneInfo) -> list[str]:
    """One line per active goal whose period is not over (the summaries, the pre-open
    check); a period not started yet reads «dal <date>»."""
    today = now.astimezone(zone).date()
    out = []
    for p in await active_progress(session, now, zone):
        if today <= p["period_end"]:
            out.append(progress_line(p, today))
    return out


def _ending_periods(goal: Goal, today: date) -> list[Period]:
    period = current_period(goal, today)
    candidates = [period]
    if goal.recurring:
        candidates.append(previous_period(goal.horizon, period))
    return [c for c in candidates if c.end < today and c.end >= goal.start_date
            and today - END_LOOKBACK <= c.end]


async def period_end_lines(session: AsyncSession, now: datetime, zone: ZoneInfo) -> list[str]:
    """«🏁 …» once per goal and period over: a period is over once its last day is, so
    the daily summary of the next day (or of a later one within END_LOOKBACK) carries
    it with the final numbers — the 18:00 summary of the last day itself would freeze
    them while US markets are still open (a month ending on a weekday). Each
    is claimed in `job_runs` (`goals.period_end`) in the caller's transaction. Periods
    that ended before the goal was created, or without data, are never announced."""
    today = now.astimezone(zone).date()
    out = []
    for goal, name in await visible_goals(session):
        created = (goal.created_at or now).astimezone(zone).date()
        for period in _ending_periods(goal, today):
            if period.end < created:
                continue
            key = f"{goal.id}:{period.start.isoformat()}:{period.end.isoformat()}"
            if not await run_job_once(session, PERIOD_END_JOB, key):
                continue
            progress = await period_progress(session, goal, period, now, zone, name)
            if progress["pct"] is not None:  # a period without data is not announced
                out.append(end_line(progress))
    return out
