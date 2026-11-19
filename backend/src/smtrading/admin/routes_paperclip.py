"""Admin API «Paperclip»: configuration (board key encrypted, only masked), the test read,
the reader status, the isolation baseline reconfirm («Riconferma») and the «Costi agenti»
data. Every call to Paperclip is a GET made before the transaction is opened (R9).

The board key never reaches a response, an error, the audit or a log; the activity-log
summary masks it by its field name (`board_key`)."""

import uuid
from datetime import UTC, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, Literal
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from smtrading.admin.auth import CurrentAdmin, DbSession, RedisClient, require_access
from smtrading.admin.common import _invalid, _transaction
from smtrading.agents.models import Agent, AgentPortfolio
from smtrading.agents.service import write_audit
from smtrading.common.config import get_settings
from smtrading.engine.models import Order
from smtrading.mcp.dto import to_jsonable
from smtrading.paperclip import budget, model_costs
from smtrading.paperclip.client import PaperclipClient, PaperclipError
from smtrading.paperclip.config import (
    KEY_PREFIX,
    SETTINGS_KEY,
    SmtAgent,
    config_dto,
    load_config,
    load_state,
    match_agent,
    save_config,
    save_state,
    smt_agents,
)
from smtrading.paperclip.models import AgentCostDay
from smtrading.paperclip.reader import isolation_policies, key_expiring, set_baseline
from smtrading.paperclip.sessions import load_sessions, sessions_dto
from smtrading.performance.service import get_performance
from smtrading.portfolios.models import Portfolio, PortfolioStatus
from smtrading.positions.protection import load_state as load_protection_state
from smtrading.risk.models import AppSetting

router = APIRouter(prefix="/api", tags=["paperclip"], dependencies=[Depends(require_access)])

MAX_TEXT = 256
# «Costi agenti» periods: days back from today (local) and the matching performance period
PERIODS: dict[str, tuple[int, Literal["1d", "1w", "1m"]]] = {
    "day": (0, "1d"), "week": (6, "1w"), "month": (29, "1m"),
}


class PaperclipConfigIn(BaseModel):
    """Only the fields present in the request are changed (`model_fields_set`);
    `board_key` absent or null keeps the stored key."""

    base_url: str | None = None
    company_id: str | None = None
    board_key: str | None = None
    enabled: bool | None = None
    agent_map: dict[str, str] | None = None
    # USD; null, "" or 0 = no budget of our own (the Paperclip company's one is used first)
    monthly_budget_usd: str | int | float | None = None


MAX_BUDGET_USD = Decimal(1_000_000)


def _monthly_budget(value: str | float | None) -> Decimal | None:
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    try:
        d = Decimal(str(value).strip().replace(",", "."))
    except InvalidOperation:
        raise _invalid("monthly_budget_usd", "budget mensile non valido") from None
    if not d.is_finite() or d < 0 or d > MAX_BUDGET_USD:
        raise _invalid("monthly_budget_usd", "il budget mensile va da 0 a 1.000.000 $")
    return d or None


def _text(field: str, value: str | None) -> str | None:
    value = (value or "").strip()
    if len(value) > MAX_TEXT:
        raise _invalid(field, f"{field} troppo lungo")
    return value or None


def _base_url(value: str | None) -> str | None:
    value = _text("base_url", value)
    if value is None:
        return None
    parts = urlsplit(value)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise _invalid("base_url", "l'URL deve iniziare con https:// (o http://)")
    try:
        parts.port  # noqa: B018  (raises on `:+443`, `:99999`, `:abc`)
    except ValueError:
        raise _invalid("base_url", "porta dell'URL non valida") from None
    return value.rstrip("/")


def _board_key(value: str) -> str:
    value = value.strip()
    if not value.startswith(KEY_PREFIX) or len(value) <= len(KEY_PREFIX) + 4 \
            or any(c.isspace() for c in value):
        raise _invalid("board_key", f"la chiave board deve iniziare con {KEY_PREFIX}")
    return value


async def _agent_map(session: AsyncSession, value: dict[str, str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for pid, sid in value.items():
        pid = (pid or "").strip()
        if not pid or len(pid) > 64:
            raise _invalid("agent_map", "id agente Paperclip non valido")
        try:
            agent_id = uuid.UUID(str(sid))
        except ValueError:
            raise _invalid("agent_map", "id agente SMTrading non valido") from None
        if await session.get(Agent, agent_id) is None:
            raise _invalid("agent_map", "agente SMTrading non trovato")
        out[pid] = str(agent_id)
    return out


def _client(cfg) -> PaperclipClient:
    return PaperclipClient(cfg)


async def _status(session: AsyncSession) -> dict[str, Any]:
    cfg = await load_config(session)
    state = await load_state(session)
    return {
        "enabled": cfg.enabled,
        "isolation": state["isolation"] if cfg.enabled else "off",
        "changes": state["changes"],
        "last_ok_at": state["last_ok_at"],
        "last_error": state["last_error"],
        "consecutive_errors": state["consecutive_errors"],
        "baseline_at": state["baseline_at"],
        "rebaseline_reason": state["rebaseline_reason"],
        "baseline_count": len(state["baseline"] or []),
        "key_expires_at": state["key_expires_at"],
        "key_expiring": key_expiring(state, datetime.now(UTC)),
        "sessions": sessions_dto(await load_sessions(session)),
        # the last failed «PROTEGGI» issue of the protection alarm (cleared by a success)
        "protection_error": (await load_protection_state(session))["last_error"],
    }


# --- routes ----------------------------------------------------------------------------------

@router.get("/paperclip/config")
async def get_paperclip_config(session: DbSession) -> dict:
    return config_dto(await load_config(session))


@router.put("/paperclip/config")
async def put_paperclip_config(
    body: PaperclipConfigIn, admin: CurrentAdmin, session: DbSession, redis: RedisClient
) -> dict:
    fields = body.model_fields_set
    async with _transaction(session, redis):
        await session.execute(select(AppSetting.key).where(AppSetting.key == SETTINGS_KEY)
                              .with_for_update())
        cur = await load_config(session)
        base_url = _base_url(body.base_url) if "base_url" in fields else cur.base_url
        company_id = _text("company_id", body.company_id) if "company_id" in fields \
            else cur.company_id
        board_key = _board_key(body.board_key) if body.board_key else None
        agent_map = await _agent_map(session, body.agent_map) \
            if body.agent_map is not None else cur.agent_map
        monthly_budget = _monthly_budget(body.monthly_budget_usd) \
            if "monthly_budget_usd" in fields else cur.monthly_budget_usd
        enabled = cur.enabled if body.enabled is None else body.enabled
        if enabled and not (base_url and company_id and (board_key or cur.board_key)):
            raise _invalid("enabled", "inserisci URL, id azienda e chiave board prima di "
                                      "attivare la lettura")
        cfg = await save_config(session, base_url=base_url, company_id=company_id,
                                board_key=board_key, enabled=enabled, agent_map=agent_map,
                                by=admin.email, monthly_budget_usd=monthly_budget)
    return config_dto(cfg)


@router.post("/paperclip/verify")
async def verify_paperclip(session: DbSession) -> dict:
    """A test read (agents and policies) with the stored configuration."""
    cfg = await load_config(session)
    agents = await smt_agents(session)
    await session.rollback()  # no transaction open during the requests (R9)
    try:
        async with _client(cfg) as client:
            remote = await client.agents()
            policies = isolation_policies(await client.tool_policies())
    except PaperclipError as e:
        return {"ok": False, "agents": [], "policies_count": 0, "error": e.message}
    out = []
    for a in remote:
        pid, name = str(a.get("id") or ""), a.get("name")
        if not pid:
            continue
        smt = match_agent(pid, name, agents, cfg.agent_map)
        out.append({"id": pid, "name": name,
                    "smt_agent": {"id": str(smt.id), "name": smt.name} if smt else None})
    return {"ok": True, "agents": out, "policies_count": len(policies), "error": None}


@router.get("/paperclip/status")
async def get_paperclip_status(session: DbSession) -> dict:
    return await _status(session)


@router.post("/paperclip/baseline")
async def reconfirm_paperclip_baseline(
    admin: CurrentAdmin, session: DbSession, redis: RedisClient
) -> dict:
    """«Riconferma»: the current isolation policies become the expected set."""
    cfg, by = await load_config(session), admin.email  # plain values: they outlive the
    await session.rollback()  # transaction, closed during the request (R9)
    try:
        async with _client(cfg) as client:
            policies = isolation_policies(await client.tool_policies())
    except PaperclipError as e:
        raise _invalid("baseline", f"lettura di Paperclip non riuscita: {e.message}") from None
    if not policies:
        raise _invalid("baseline", "nessuna regola di isolamento trovata: controlla l'id "
                                   "azienda e i permessi della chiave")
    async with _transaction(session, redis):
        state = await load_state(session, for_update=True)
        set_baseline(state, policies, datetime.now(UTC))
        await save_state(session, state)
        await write_audit(session, agent_id=None, method="admin:paperclip.baseline",
                          params={"by": by, "policies": len(policies),
                                  "ids": [p["id"] for p in policies]},
                          outcome="ok", latency_ms=0)
    return await _status(session)


@router.get("/agent-costs")
async def get_agent_costs(session: DbSession, period: str = Query("day")) -> dict:
    if period not in PERIODS:
        raise _invalid("period", f"period deve essere uno tra {list(PERIODS)}")
    days_back, perf_period = PERIODS[period]
    zone = ZoneInfo(get_settings().tz)
    to = datetime.now(zone).date()
    from_ = to - timedelta(days=days_back)
    since = datetime.combine(from_, time(0), tzinfo=zone)
    cfg = await load_config(session)
    state = await load_state(session)
    costs = (await session.execute(
        select(AgentCostDay.paperclip_agent_id, func.max(AgentCostDay.paperclip_agent_name),
               func.sum(AgentCostDay.api_usd), func.sum(AgentCostDay.subscription_usd),
               func.sum(AgentCostDay.other_usd), func.sum(AgentCostDay.runs))
        .where(AgentCostDay.day >= from_, AgentCostDay.day <= to)
        .group_by(AgentCostDay.paperclip_agent_id)
        .order_by(AgentCostDay.paperclip_agent_id)
    )).all()
    agents = await smt_agents(session)
    models = await model_costs.load_models(session)
    current = model_costs.current_models(models)
    model_of: dict[uuid.UUID, set[str]] = {}
    for c in current:
        smt = match_agent(c["paperclip_agent_id"], c["name"], agents, cfg.agent_map)
        if smt is not None and c["model"]:
            model_of.setdefault(smt.id, set()).add(c["model"])
    by_agent: dict[uuid.UUID, list] = {a.id: [] for a in agents}
    unmatched = []
    for pid, name, api, sub, other, runs in costs:
        smt = match_agent(pid, name, agents, cfg.agent_map)
        if smt is not None:
            by_agent[smt.id].append((name, api, sub, other, runs))
        else:
            unmatched.append({"paperclip_agent_id": pid, "name": name,
                              "api_usd": api, "subscription_usd": sub, "other_usd": other})
    orders = dict((await session.execute(
        select(Order.agent_id, func.count()).where(Order.created_at >= since)
        .group_by(Order.agent_id)
    )).all())
    rows = []
    for a in agents:
        matched = by_agent[a.id]
        rows.append({
            "agent_id": str(a.id),
            "name": a.name,
            "paperclip_name": ", ".join(sorted({m[0] for m in matched if m[0]})) or None,
            "api_usd": sum((m[1] for m in matched), start=Decimal(0)),
            "subscription_usd": sum((m[2] for m in matched), start=Decimal(0)),
            "other_usd": sum((m[3] for m in matched), start=Decimal(0)),
            "runs": int(sum(m[4] for m in matched)),
            "model": ", ".join(sorted(model_of.get(a.id, set()))) or None,
            "orders": orders.get(str(a.id), 0),
            "portfolios": await _portfolios(session, a.id, perf_period),
        })
    return to_jsonable({"currency": "USD", "from": from_, "to": to,
                        "last_read_at": state["last_ok_at"], "agents": rows,
                        "unmatched": unmatched,
                        "month": await _month(session, to, cfg, state, agents),
                        "models": {"rows": model_costs.by_model(models, from_, to),
                                   "agents": current,
                                   "changes": list(reversed(models.get("changes") or [])),
                                   "updated_at": models.get("updated_at")}})


COMPARE_MAX_DAYS = 30


@router.get("/agent-costs/compare")
async def compare_agent_costs(session: DbSession, at: str | None = Query(None),
                              days: int = Query(7)) -> dict:
    """Per model, the `days` days before `at` against the time after it (default `at`:
    the last model change seen; a local time without an offset is in the app's zone)."""
    if not 1 <= days <= COMPARE_MAX_DAYS:
        raise _invalid("days", f"days va da 1 a {COMPARE_MAX_DAYS}")
    zone = ZoneInfo(get_settings().tz)
    models = await model_costs.load_models(session)
    if at is None:
        changes = models.get("changes") or []
        if not changes:
            raise _invalid("at", "nessun cambio di modello registrato: indica data e ora")
        at = changes[-1]["at"]
    try:
        when = datetime.fromisoformat(at)
    except ValueError:
        raise _invalid("at", "data e ora non valide") from None
    if when.tzinfo is None:
        when = when.replace(tzinfo=zone)
    now = datetime.now(UTC)
    if when > now:
        raise _invalid("at", "data e ora nel futuro")
    return to_jsonable(model_costs.compare(models, when, days, get_settings().tz, now))


async def _month(session: AsyncSession, today, cfg, state: dict, agents: list[SmtAgent]
                 ) -> dict:
    """The current month against the budget (whatever the period), with each agent's share
    of the billed spend (SMTrading agents, then the unmatched Paperclip ones by name)."""
    spend = await budget.month_spend(session, today)
    shares: dict[tuple[str | None, str], Decimal] = {}
    for pid, name, usd in spend:
        smt = match_agent(pid, name, agents, cfg.agent_map)
        key = (str(smt.id), smt.name) if smt else (None, name or pid)
        shares[key] = shares.get(key, Decimal(0)) + usd
    total = sum(shares.values(), start=Decimal(0))
    out = budget.summary(today, total, budget.resolve_budget(
        state.get("company_budget_cents"), cfg.monthly_budget_usd))
    out["shares"] = [
        {"agent_id": aid, "name": name, "usd": usd.quantize(budget.CENT),
         "pct": budget.pct(usd, total)}
        for (aid, name), usd in sorted(shares.items(), key=lambda kv: (-kv[1], kv[0][1]))
        if usd > 0]
    return out


async def _portfolios(session: AsyncSession, agent_id: uuid.UUID, perf_period) -> list[dict]:
    rows = (await session.execute(
        select(Portfolio.id, Portfolio.name, AgentPortfolio.permission)
        .join(AgentPortfolio, AgentPortfolio.portfolio_id == Portfolio.id)
        .where(AgentPortfolio.agent_id == agent_id,
               Portfolio.status != PortfolioStatus.archived)
        .order_by(Portfolio.name)
    )).all()
    out = []
    for pid, name, permission in rows:
        perf = await get_performance(session, pid, perf_period)
        out.append({"id": pid, "name": name, "permission": permission,
                    "return_pct": perf["return_pct"]})
    return out

