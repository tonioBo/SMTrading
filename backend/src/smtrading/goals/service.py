"""Goals: validation, create, update, deactivate, the admin DTO, and «the weekly goal»
(the earliest recurring weekly minimum goal on the total, the one migrated from the old
setting) behind the compatibility surfaces (`GET /api/weekly-goal`, the `weekly_goal`
app setting, the 1w goal of the «Andamento» chart)."""

from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from smtrading.common.errors import ErrorCode, SMTError
from smtrading.goals.models import BENCHMARKS, HORIZONS, KINDS, MAX_NAME_LEN, SCOPES, Goal
from smtrading.portfolios.models import Portfolio, PortfolioStatus
from smtrading.risk.models import RiskProfile

MAX_PCT = Decimal(1000)
MAX_CUSTOM_DAYS = 3 * 366
_Q2 = Decimal("0.01")
FIELDS = ("name", "horizon", "start_date", "end_date", "recurring", "scope",
          "portfolio_id", "target_pct", "kind", "benchmark", "active")
WEEKLY_NAME = "settimanale"


def _invalid(field: str, message: str) -> SMTError:
    return SMTError(ErrorCode.INVALID_ORDER, message, {"field": field})


def parse_pct(value: Any, field: str = "target_pct", max_pct: Decimal = MAX_PCT, *,
              zero: bool = False) -> Decimal:
    """A percent above 0 (from 0 with `zero`) and up to `max_pct`, at most 2 decimals
    (string or integer)."""
    if isinstance(value, bool) or not isinstance(value, str | int):
        raise _invalid(field, "percentuale non valida")
    try:
        pct = Decimal(str(value).strip())
    except InvalidOperation:
        raise _invalid(field, "percentuale non valida") from None
    if not pct.is_finite() or not (0 <= pct if zero else 0 < pct) or pct > max_pct:
        raise _invalid(field, f"percentuale tra 0 e {max_pct}")
    if pct != pct.quantize(_Q2):
        raise _invalid(field, "al massimo 2 decimali")
    return pct


def _date(value: Any, field: str) -> date | None:
    if value is None or isinstance(value, date):
        return value
    if not isinstance(value, str):
        raise _invalid(field, f"{field}: data non valida (AAAA-MM-GG)")
    try:
        return date.fromisoformat(value.strip())
    except ValueError:
        raise _invalid(field, f"{field}: data non valida (AAAA-MM-GG)") from None


def _choice(value: Any, allowed: tuple[str, ...], field: str) -> str:
    if value not in allowed:
        raise _invalid(field, f"{field} deve essere uno tra {', '.join(allowed)}")
    return value


def _bool(value: Any, field: str) -> bool:
    if not isinstance(value, bool):
        raise _invalid(field, f"{field} deve essere vero o falso")
    return value


async def _portfolio(session: AsyncSession, value: Any) -> UUID:
    try:
        pid = value if isinstance(value, UUID) else UUID(str(value))
    except ValueError:
        raise _invalid("portfolio_id", "portafoglio non valido") from None
    p = await session.get(Portfolio, pid)
    if p is None:
        raise SMTError(ErrorCode.NOT_FOUND, "portafoglio non trovato",
                       {"portfolio_id": str(pid)})
    if p.status == PortfolioStatus.archived:
        raise _invalid("portfolio_id", "il portafoglio è archiviato")
    return pid


async def validated(session: AsyncSession, raw: dict[str, Any], *,
                    today: date, check_portfolio: bool = True) -> dict[str, Any]:
    """The goal fields of `raw` (every field of FIELDS present) checked as a whole."""
    name = raw["name"]
    if not isinstance(name, str) or not " ".join(name.split()):
        raise _invalid("name", "nome obbligatorio")
    name = " ".join(name.split())
    if len(name) > MAX_NAME_LEN:
        raise _invalid("name", f"nome di al massimo {MAX_NAME_LEN} caratteri")
    horizon = _choice(raw["horizon"], HORIZONS, "horizon")
    scope = _choice(raw["scope"], SCOPES, "scope")
    kind = _choice(raw["kind"], KINDS, "kind")
    benchmark = None
    if kind == "beat_index":
        value = raw.get("benchmark")
        if value is None:
            raise _invalid("benchmark", "indice obbligatorio per «battere un indice»")
        benchmark = _choice(value.strip().upper() if isinstance(value, str) else value,
                            BENCHMARKS, "benchmark")
    recurring = _bool(raw["recurring"], "recurring")
    active = _bool(raw["active"], "active")
    start = _date(raw["start_date"], "start_date") or today
    end = _date(raw["end_date"], "end_date")
    if horizon == "custom":
        if end is None:
            raise _invalid("end_date", "data di fine obbligatoria per un periodo personalizzato")
        if end < start:
            raise _invalid("end_date", "la data di fine precede quella di inizio")
        if (end - start).days >= MAX_CUSTOM_DAYS:
            raise _invalid("end_date", "periodo personalizzato di al massimo 3 anni")
        if recurring:
            raise _invalid("recurring", "un periodo personalizzato non si ripete")
    else:
        end = None  # computed from the horizon
    portfolio_id = None
    if scope == "portfolio":
        if raw["portfolio_id"] is None:
            raise _invalid("portfolio_id", "portafoglio obbligatorio")
        portfolio_id = (await _portfolio(session, raw["portfolio_id"]) if check_portfolio
                        else raw["portfolio_id"])
    return {"name": name, "horizon": horizon, "start_date": start, "end_date": end,
            "recurring": recurring, "scope": scope, "portfolio_id": portfolio_id,
            "target_pct": parse_pct(raw["target_pct"], zero=kind == "beat_index"),
            "kind": kind, "benchmark": benchmark, "active": active}


DEFAULTS: dict[str, Any] = {"start_date": None, "end_date": None, "recurring": False,
                            "scope": "total", "portfolio_id": None, "kind": "min",
                            "benchmark": None, "active": True}


async def create_goal(session: AsyncSession, body: dict[str, Any], *, today: date) -> Goal:
    raw = {**DEFAULTS, **{k: v for k, v in body.items() if k in FIELDS}}
    for f in ("name", "horizon", "target_pct"):
        if raw.get(f) is None:
            raise _invalid(f, f"{f} obbligatorio")
    goal = Goal(**await validated(session, raw, today=today))
    session.add(goal)
    await session.flush()
    await session.refresh(goal)
    return goal


async def get_goal(session: AsyncSession, goal_id: UUID) -> Goal:
    goal = await session.get(Goal, goal_id)
    if goal is None:
        raise SMTError(ErrorCode.NOT_FOUND, "obiettivo non trovato", {"goal_id": str(goal_id)})
    return goal


async def update_goal(session: AsyncSession, goal: Goal, body: dict[str, Any], *,
                      today: date) -> Goal:
    """Only the fields of `body` change; the result is validated as a whole (a total goal
    drops its portfolio; the portfolio is checked again only when it changes or the goal
    is reactivated)."""
    raw = {f: getattr(goal, f) for f in FIELDS}
    raw["target_pct"] = str(goal.target_pct)
    for f, v in body.items():
        if f in FIELDS:
            raw[f] = v
    if raw["scope"] == "total":
        raw["portfolio_id"] = None
    for f in ("name", "horizon", "target_pct", "scope", "kind", "recurring", "active",
              "start_date"):
        if raw[f] is None:
            raise _invalid(f, f"{f} non può essere nullo")
    recheck = ("portfolio_id" in body or "scope" in body
               or (body.get("active") is True and not goal.active))
    values = await validated(session, raw, today=today, check_portfolio=recheck)
    for key, value in values.items():
        setattr(goal, key, value)
    await session.flush()
    await session.refresh(goal)
    return goal


async def deactivate_goal(session: AsyncSession, goal: Goal) -> Goal:
    goal.active = False
    await session.flush()
    await session.refresh(goal)
    return goal


def goal_dto(goal: Goal, portfolio: tuple[str, bool] | None = None) -> dict[str, Any]:
    """The Goal of the contract (Decimals, dates, datetimes; the route serialises), plus
    `portfolio_name` and `portfolio_archived` (a goal of an archived portfolio is hidden
    from the progress)."""
    return {
        "id": goal.id, "name": goal.name, "horizon": goal.horizon,
        "start_date": goal.start_date, "end_date": goal.end_date,
        "recurring": goal.recurring, "scope": goal.scope, "portfolio_id": goal.portfolio_id,
        "target_pct": goal.target_pct, "kind": goal.kind, "benchmark": goal.benchmark,
        "active": goal.active,
        "created_at": goal.created_at, "updated_at": goal.updated_at,
        "portfolio_name": portfolio[0] if portfolio else None,
        "portfolio_archived": bool(portfolio and portfolio[1]),
    }


async def portfolio_labels(session: AsyncSession,
                           ids: set[UUID]) -> dict[UUID, tuple[str, bool]]:
    """(name, archived) of the portfolios `ids`."""
    if not ids:
        return {}
    rows = (await session.execute(select(Portfolio.id, Portfolio.name, Portfolio.status)
                                  .where(Portfolio.id.in_(ids)))).all()
    return {pid: (name, status == PortfolioStatus.archived) for pid, name, status in rows}


async def list_goals(session: AsyncSession) -> list[dict[str, Any]]:
    """Every goal (active first, then by creation)."""
    goals = list(await session.scalars(
        select(Goal).order_by(Goal.active.desc(), Goal.created_at, Goal.id)))
    names = await portfolio_labels(session, {g.portfolio_id for g in goals if g.portfolio_id})
    return [goal_dto(g, names.get(g.portfolio_id)) for g in goals]


# -- templates («Modelli») -------------------------------------------------------------


def _body(name: str, horizon: str, target: str, *, kind: str = "min",
          portfolio_id: UUID | None = None, benchmark: str | None = None,
          today: date) -> dict[str, Any]:
    return {"name": name, "horizon": horizon, "start_date": today.isoformat(),
            "end_date": None, "recurring": True,
            "scope": "portfolio" if portfolio_id else "total",
            "portfolio_id": str(portfolio_id) if portfolio_id else None,
            "target_pct": target, "kind": kind, "benchmark": benchmark, "active": True}


def _same(kind: str, horizon: str, scope: str, portfolio_id, target, benchmark,
          recurring: bool) -> tuple:
    return (kind, horizon, scope, str(portfolio_id) if portfolio_id else None,
            Decimal(str(target)), benchmark, bool(recurring))


# a portfolio whose daily-loss breaker is at least this wide gets the «dynamic» values
_DYNAMIC_LOSS_PCT = Decimal(7)


def _dynamic(loss_pct: Decimal | None) -> bool:
    """Dynamic (larger targets and risk) when the profile's daily-loss breaker is wide
    (≥ 7%) or absent; prudent otherwise."""
    return loss_pct is None or loss_pct >= _DYNAMIC_LOSS_PCT


async def goal_templates(session: AsyncSession, *, today: date) -> list[dict[str, Any]]:
    """The «Modelli» of the Obiettivi page: [{key, label, body, exists}], `body` a create
    body to prefill the dialog (the admin confirms); week to year on the total, the
    per-portfolio ones once per live portfolio, by name, with prudent or dynamic values
    from its profile's daily-loss breaker (`_dynamic`); `exists` when an active goal already is the same (kind, horizon,
    scope, portfolio, target, benchmark, recurring; the name aside)."""
    portfolios = list(await session.execute(
        select(Portfolio.id, Portfolio.name, RiskProfile.max_daily_loss_pct)
        .outerjoin(RiskProfile, RiskProfile.id == Portfolio.risk_profile_id)
        .where(Portfolio.status != PortfolioStatus.archived).order_by(Portfolio.name)))
    out = [
        {"key": "weekly_total", "label": "Settimanale +2% totale",
         "body": _body("settimanale", "week", "2", today=today)},
        {"key": "risk_weekly_total", "label": "Rischio settimanale −3% totale",
         "body": _body("rischio settimanale", "week", "3", kind="max_loss", today=today)},
        {"key": "monthly_total", "label": "Mensile +5% totale",
         "body": _body("mensile", "month", "5", today=today)},
        {"key": "quarterly_total", "label": "Trimestrale +10% totale",
         "body": _body("trimestrale", "quarter", "10", today=today)},
        {"key": "yearly_total", "label": "Annuale +20% totale",
         "body": _body("annuale", "year", "20", today=today)},
    ]
    for pid, name, loss_pct in portfolios:
        dyn = _dynamic(loss_pct)
        month, year, risk_year = ("8", "30", "20") if dyn else ("3", "10", "10")
        out += [
            {"key": f"monthly_portfolio:{pid}", "label": f"Mensile +{month}% · {name}",
             "body": _body("mensile", "month", month, portfolio_id=pid, today=today)},
            {"key": f"quarterly_portfolio:{pid}", "label": f"Trimestrale +10% · {name}",
             "body": _body("trimestrale", "quarter", "10", portfolio_id=pid, today=today)},
            {"key": f"yearly_portfolio:{pid}", "label": f"Annuale +{year}% · {name}",
             "body": _body("annuale", "year", year, portfolio_id=pid, today=today)},
            {"key": f"risk_monthly:{pid}", "label": f"Rischio mensile −5% · {name}",
             "body": _body("rischio mensile", "month", "5", kind="max_loss",
                           portfolio_id=pid, today=today)},
            {"key": f"risk_quarterly:{pid}", "label": f"Rischio trimestrale −8% · {name}",
             "body": _body("rischio trimestrale", "quarter", "8", kind="max_loss",
                           portfolio_id=pid, today=today)},
            {"key": f"risk_yearly:{pid}", "label": f"Rischio annuale −{risk_year}% · {name}",
             "body": _body("rischio annuale", "year", risk_year, kind="max_loss",
                           portfolio_id=pid, today=today)},
        ]
        if dyn:
            out.append({"key": f"beat_qqq_quarter:{pid}",
                        "label": f"Battere QQQ nel trimestre · {name}",
                        "body": _body("battere QQQ", "quarter", "0", kind="beat_index",
                                      portfolio_id=pid, benchmark="QQQ", today=today)})
    out += [
        {"key": "beat_vwce_quarter", "label": "Battere VWCE nel trimestre (totale)",
         "body": _body("battere VWCE", "quarter", "0", kind="beat_index",
                       benchmark="VWCE.DE", today=today)},
        {"key": "beat_spy_year", "label": "Battere SPY nell'anno (totale)",
         "body": _body("battere SPY", "year", "0", kind="beat_index", benchmark="SPY",
                       today=today)},
        {"key": "beat_vwce_year", "label": "Battere VWCE nell'anno (totale)",
         "body": _body("battere VWCE", "year", "0", kind="beat_index",
                       benchmark="VWCE.DE", today=today)},
    ]
    existing = {_same(g.kind, g.horizon, g.scope, g.portfolio_id, g.target_pct, g.benchmark,
                      g.recurring)
                for g in await session.scalars(select(Goal).where(Goal.active.is_(True)))}
    for t in out:
        b = t["body"]
        t["exists"] = _same(b["kind"], b["horizon"], b["scope"], b["portfolio_id"],
                            b["target_pct"], b["benchmark"], b["recurring"]) in existing
    return out


# -- «the weekly goal» -----------------------------------------------------------------


async def weekly_goal(session: AsyncSession) -> Goal | None:
    """The earliest active recurring weekly minimum goal on the total, else the earliest
    such goal (none active)."""
    return await session.scalar(
        select(Goal).where(Goal.horizon == "week", Goal.recurring.is_(True),
                           Goal.scope == "total", Goal.kind == "min")
        .order_by(Goal.active.desc(), Goal.created_at, Goal.id).limit(1))


async def set_weekly_goal(session: AsyncSession, *, on: bool, pct: Decimal,
                          today: date) -> Goal:
    """The old setting's PUT: turns the weekly goal on/off and sets its percent, creating
    it (from this week) when there is none."""
    goal = await weekly_goal(session)
    if goal is None:
        goal = Goal(name=WEEKLY_NAME, horizon="week", recurring=True, scope="total",
                    kind="min", start_date=today - timedelta(days=today.weekday()),
                    target_pct=pct, active=on)
        session.add(goal)
    else:
        goal.active, goal.target_pct = on, pct
    await session.flush()
    return goal

