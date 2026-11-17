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
from smtrading.goals.models import HORIZONS, KINDS, MAX_NAME_LEN, SCOPES, Goal
from smtrading.portfolios.models import Portfolio, PortfolioStatus

MAX_PCT = Decimal(1000)
MAX_CUSTOM_DAYS = 3 * 366
_Q2 = Decimal("0.01")
FIELDS = ("name", "horizon", "start_date", "end_date", "recurring", "scope",
          "portfolio_id", "target_pct", "kind", "active")
WEEKLY_NAME = "settimanale"


def _invalid(field: str, message: str) -> SMTError:
    return SMTError(ErrorCode.INVALID_ORDER, message, {"field": field})


def parse_pct(value: Any, field: str = "target_pct", max_pct: Decimal = MAX_PCT) -> Decimal:
    """A percent above 0 and up to `max_pct`, at most 2 decimals (string or integer)."""
    if isinstance(value, bool) or not isinstance(value, str | int):
        raise _invalid(field, "percentuale non valida")
    try:
        pct = Decimal(str(value).strip())
    except InvalidOperation:
        raise _invalid(field, "percentuale non valida") from None
    if not pct.is_finite() or not 0 < pct <= max_pct:
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
            "target_pct": parse_pct(raw["target_pct"]), "kind": kind, "active": active}


DEFAULTS: dict[str, Any] = {"start_date": None, "end_date": None, "recurring": False,
                            "scope": "total", "portfolio_id": None, "kind": "min",
                            "active": True}


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
        "target_pct": goal.target_pct, "kind": goal.kind, "active": goal.active,
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


# -- «the weekly goal» -----------------------------------------------------------------


async def weekly_goal(session: AsyncSession) -> Goal | None:
    """The earliest recurring weekly minimum goal on the total (active or not)."""
    return await session.scalar(
        select(Goal).where(Goal.horizon == "week", Goal.recurring.is_(True),
                           Goal.scope == "total", Goal.kind == "min")
        .order_by(Goal.created_at, Goal.id).limit(1))


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

