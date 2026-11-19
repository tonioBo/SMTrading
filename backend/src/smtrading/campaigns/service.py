"""Campaigns: the «Nuova campagna guidata» (create), the goal templates of its third step,
the close wizard (what is still open, archive, conclude), the DTOs.

Everything reuses the existing services, never their rules: portfolios are created by
`portfolios.service.create_portfolio`, linked by `brokers.service.link_portfolio` (only
paper/demo links: a real-money link, or a portfolio already on one, is refused), goals are
created by `goals.service.create_goal` (its validation and the «piano» default:
`closes_positions` true unless an active piano already covers the portfolio) and
portfolios are archived by `admin.portfolio_ops.archive_portfolio` (refused with open
orders, a broker link, shorts or a margin loan).

Nothing here commits: the route runs each create/close in one transaction (an error rolls
everything back: no portfolio, link, goal or campaign is left half made)."""

from datetime import UTC, date, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from smtrading.admin.portfolio_ops import archive_portfolio
from smtrading.agents.service import write_audit
from smtrading.brokers.live import LIVE_KINDS
from smtrading.brokers.models import BrokerLink
from smtrading.brokers.service import link_portfolio
from smtrading.campaigns.models import (
    MAX_NAME_LEN,
    MAX_NOTES_LEN,
    Campaign,
    CampaignGoal,
    CampaignPortfolio,
)
from smtrading.common.errors import ErrorCode, SMTError
from smtrading.engine.models import OPEN_STATUSES, Order
from smtrading.events import discard_post_commit
from smtrading.goals import service as goals
from smtrading.goals.models import Goal
from smtrading.goals.plan_closer import plan_states
from smtrading.mcp.dto import parse_decimal
from smtrading.portfolios.models import Portfolio, PortfolioStatus
from smtrading.portfolios.service import create_portfolio
from smtrading.positions.models import Position
from smtrading.risk.models import RiskProfile

MAX_PORTFOLIOS = 20
MAX_GOALS = 60
OPEN = ("bozza", "attiva")
LIVE_LINK_REFUSED = ("una campagna usa solo conti di prova (paper/demo): i collegamenti a "
                     "denaro reale non sono ammessi")
LIVE_PORTFOLIO_REFUSED = ("il portafoglio «{name}» è collegato a denaro reale: non può far "
                          "parte di una campagna di prova")
IN_OTHER_CAMPAIGN = "il portafoglio «{name}» è già nella campagna aperta «{campaign}»"
ALREADY_CLOSED = "la campagna è già conclusa"


def _invalid(field: str, message: str, **details) -> SMTError:
    return SMTError(ErrorCode.INVALID_ORDER, message, {"field": field, **details})


def _text(value: Any, field: str, max_len: int, *, required: bool) -> str | None:
    if value is None or (isinstance(value, str) and not value.strip()):
        if required:
            raise _invalid(field, f"{field}: obbligatorio")
        return None
    if not isinstance(value, str):
        raise _invalid(field, f"{field}: testo non valido")
    text = value.strip() if field == "notes" else " ".join(value.split())
    if len(text) > max_len:
        raise _invalid(field, f"{field}: al massimo {max_len} caratteri")
    return text


def _date(value: Any, field: str) -> date:
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value).strip())
    except (TypeError, ValueError):
        raise _invalid(field, f"{field}: data non valida (AAAA-MM-GG)") from None


def _uuid(value: Any, field: str) -> UUID:
    try:
        return value if isinstance(value, UUID) else UUID(str(value))
    except ValueError:
        raise _invalid(field, f"{field}: identificativo non valido") from None


async def _check_name(session: AsyncSession, name: str, exclude: UUID | None = None) -> None:
    q = select(Campaign.id).where(Campaign.name == name)
    if exclude is not None:
        q = q.where(Campaign.id != exclude)
    if await session.scalar(q) is not None:
        raise _invalid("name", "esiste già una campagna con questo nome")


def header(body: dict[str, Any], *, today: date) -> dict[str, Any]:
    """Name, dates (end not before start nor before today, at most 3 years: the goals'
    custom period limit), notes, status (bozza | attiva, default attiva)."""
    name = _text(body.get("name"), "name", MAX_NAME_LEN, required=True)
    start = _date(body.get("start_date") or today, "start_date")
    end = _date(body.get("end_date"), "end_date") if body.get("end_date") else None
    if end is None:
        raise _invalid("end_date", "data di fine obbligatoria")
    if end < start:
        raise _invalid("end_date", "la data di fine precede quella di inizio")
    if end < today:
        raise _invalid("end_date", "la data di fine è già passata")
    if (end - start).days >= goals.MAX_CUSTOM_DAYS:
        raise _invalid("end_date", "una campagna dura al massimo 3 anni")
    status = body.get("status") or "attiva"
    if status not in OPEN:
        raise _invalid("status", "stato iniziale: bozza o attiva")
    return {"name": name, "start_date": start, "end_date": end,
            "notes": _text(body.get("notes"), "notes", MAX_NOTES_LEN, required=False),
            "status": status}


async def _test_link(session: AsyncSession, link_id: UUID) -> BrokerLink:
    """A paper/demo link, enabled: a real-money one is FORBIDDEN."""
    link = await session.get(BrokerLink, link_id)
    if link is None:
        raise SMTError(ErrorCode.NOT_FOUND, "collegamento broker non trovato",
                       {"field": "broker_link_id", "broker_link_id": str(link_id)})
    if link.kind in LIVE_KINDS or link.live_state is not None:
        raise SMTError(ErrorCode.FORBIDDEN, LIVE_LINK_REFUSED, {"field": "broker_link_id"})
    if not link.enabled:
        raise _invalid("broker_link_id", "collegamento broker disattivato")
    return link


async def open_campaign_of(session: AsyncSession, portfolio_id: UUID,
                           exclude: UUID | None = None) -> Campaign | None:
    q = (select(Campaign).join(CampaignPortfolio, CampaignPortfolio.campaign_id == Campaign.id)
         .where(CampaignPortfolio.portfolio_id == portfolio_id, Campaign.status.in_(OPEN)))
    if exclude is not None:
        q = q.where(Campaign.id != exclude)
    return await session.scalar(q.limit(1))


async def _existing(session: AsyncSession, raw: dict[str, Any]) -> Portfolio:
    pid = _uuid(raw.get("portfolio_id"), "portfolio_id")
    p = await session.get(Portfolio, pid)
    if p is None:
        raise SMTError(ErrorCode.NOT_FOUND, "portafoglio non trovato",
                       {"field": "portfolio_id", "portfolio_id": str(pid)})
    if p.status == PortfolioStatus.archived:
        raise _invalid("portfolio_id", f"il portafoglio «{p.name}» è archiviato")
    if p.broker_link_id is not None:
        link = await session.get(BrokerLink, p.broker_link_id)
        if link is not None and (link.kind in LIVE_KINDS or link.live_state is not None):
            raise SMTError(ErrorCode.FORBIDDEN, LIVE_PORTFOLIO_REFUSED.format(name=p.name),
                           {"field": "portfolio_id"})
    other = await open_campaign_of(session, p.id)
    if other is not None:
        raise _invalid("portfolio_id", IN_OTHER_CAMPAIGN.format(name=p.name,
                                                                campaign=other.name))
    return p


async def _new(session: AsyncSession, raw: dict[str, Any], by: str) -> Portfolio:
    name = _text(raw.get("name"), "name", MAX_NAME_LEN, required=True)
    cash = parse_decimal(raw.get("initial_cash"), "initial_cash")
    if cash is None or cash <= 0:
        raise _invalid("initial_cash", f"«{name}»: capitale iniziale maggiore di zero")
    profile_id = raw.get("risk_profile_id")
    if profile_id is not None:
        profile_id = _uuid(profile_id, "risk_profile_id")
        if await session.get(RiskProfile, profile_id) is None:
            raise SMTError(ErrorCode.NOT_FOUND, "profilo di rischio non trovato",
                           {"field": "risk_profile_id"})
    return await create_portfolio(
        session, name=name, initial_cash=cash, base_currency=raw.get("base_currency") or "EUR",
        author_id=by, risk_profile_id=profile_id)


def link_ids(body: dict[str, Any]) -> list[UUID]:
    """The broker links named by the request's portfolios (for the route's pre-checks of
    a dedicated account, before the transaction)."""
    out: list[UUID] = []
    for raw in body.get("portfolios") or []:
        if isinstance(raw, dict) and raw.get("broker_link_id"):
            lid = _uuid(raw["broker_link_id"], "broker_link_id")
            if lid not in out:
                out.append(lid)
    return out


async def create_campaign(session: AsyncSession, body: dict[str, Any], *, by: str,
                          today: date) -> Campaign:
    """The wizard's confirm: the campaign, its new portfolios (with their deposit), the
    links to paper/demo accounts, the goals, the audit row. Raises on the first problem
    (the route rolls everything back)."""
    values = header(body, today=today)
    await _check_name(session, values["name"])
    specs = body.get("portfolios") or []
    if not isinstance(specs, list) or not specs:
        raise _invalid("portfolios", "scegli o crea almeno un portafoglio")
    if len(specs) > MAX_PORTFOLIOS:
        raise _invalid("portfolios", f"al massimo {MAX_PORTFOLIOS} portafogli")
    goal_specs = body.get("goals") or []
    if not isinstance(goal_specs, list) or len(goal_specs) > MAX_GOALS:
        raise _invalid("goals", f"al massimo {MAX_GOALS} obiettivi")

    campaign = Campaign(**values, created_by=by)
    session.add(campaign)
    await session.flush()
    by_key: dict[str, Portfolio] = {}
    rows = []
    for raw in specs:
        if not isinstance(raw, dict):
            raise _invalid("portfolios", "portafoglio non valido")
        key = str(raw.get("key") or raw.get("portfolio_id") or "").strip()
        if not key or key in by_key:
            raise _invalid("key", "ogni portafoglio ha una chiave diversa")
        created = not raw.get("portfolio_id")
        p = await (_new(session, raw, by) if created else _existing(session, raw))
        if p.id in {q.id for q in by_key.values()}:
            raise _invalid("portfolio_id", f"il portafoglio «{p.name}» è scelto due volte")
        if raw.get("broker_link_id"):
            link = await _test_link(session, _uuid(raw["broker_link_id"], "broker_link_id"))
            await link_portfolio(session, p.id, link.id)
        by_key[key] = p
        rows.append(CampaignPortfolio(campaign_id=campaign.id, portfolio_id=p.id,
                                      created=created))
    session.add_all(rows)
    await session.flush()

    goal_ids = []
    for i, raw in enumerate(goal_specs):
        if not isinstance(raw, dict):
            raise _invalid("goals", "obiettivo non valido", goal=i)
        p = by_key.get(str(raw.get("portfolio_key") or ""))
        if p is None:
            raise _invalid("portfolio_key", "obiettivo di un portafoglio fuori dalla campagna",
                           goal=i)
        goal_body = {k: v for k, v in raw.items() if k in goals.FIELDS}
        goal_body.update(scope="portfolio", portfolio_id=str(p.id))
        try:
            goal = await goals.create_goal(session, goal_body, today=today)
        except SMTError as e:
            label = raw.get("name") if isinstance(raw.get("name"), str) else f"n. {i + 1}"
            raise SMTError(e.code, f"obiettivo «{label}» ({p.name}): {e.message}",
                           {**(e.details or {}), "goal": i}) from None
        goal_ids.append(goal.id)
        session.add(CampaignGoal(campaign_id=campaign.id, goal_id=goal.id))
    await session.flush()
    await write_audit(
        session, agent_id=None, method="admin:campaign.create",
        params={"campaign_id": str(campaign.id), "name": campaign.name, "by": by,
                "start_date": campaign.start_date.isoformat(),
                "end_date": campaign.end_date.isoformat(), "status": campaign.status,
                "portfolios": [{"id": str(r.portfolio_id), "created": r.created}
                               for r in rows],
                "goals": [str(g) for g in goal_ids]},
        outcome="ok", latency_ms=0)
    await session.refresh(campaign)
    return campaign


async def get_campaign(session: AsyncSession, campaign_id: UUID) -> Campaign:
    c = await session.get(Campaign, campaign_id)
    if c is None:
        raise SMTError(ErrorCode.NOT_FOUND, "campagna non trovata",
                       {"campaign_id": str(campaign_id)})
    return c


async def update_campaign(session: AsyncSession, campaign: Campaign, body: dict[str, Any],
                          *, by: str) -> Campaign:
    """Name and notes; status only bozza → attiva («Avvia»). A concluded campaign is
    read-only."""
    if campaign.status == "conclusa":
        raise _invalid("status", ALREADY_CLOSED)
    changes: dict[str, Any] = {}
    if "name" in body:
        name = _text(body["name"], "name", MAX_NAME_LEN, required=True)
        await _check_name(session, name, campaign.id)
        changes["name"] = name
    if "notes" in body:
        changes["notes"] = _text(body["notes"], "notes", MAX_NOTES_LEN, required=False)
    if "status" in body and body["status"] != campaign.status:
        if body["status"] != "attiva":
            raise _invalid("status", "una bozza si può solo avviare; per concluderla usa "
                                     "«Chiudi campagna»")
        changes["status"] = "attiva"
    for k, v in changes.items():
        setattr(campaign, k, v)
    await session.flush()
    if changes:
        await write_audit(session, agent_id=None, method="admin:campaign.update",
                          params={"campaign_id": str(campaign.id), "by": by,
                                  "changes": sorted(changes)},
                          outcome="ok", latency_ms=0)
    await session.refresh(campaign)
    return campaign


async def portfolio_ids(session: AsyncSession, campaign_id: UUID) -> list[UUID]:
    return list(await session.scalars(
        select(CampaignPortfolio.portfolio_id)
        .join(Portfolio, Portfolio.id == CampaignPortfolio.portfolio_id)
        .where(CampaignPortfolio.campaign_id == campaign_id).order_by(Portfolio.name)))


async def goal_ids(session: AsyncSession, campaign_id: UUID) -> list[UUID]:
    return list(await session.scalars(
        select(CampaignGoal.goal_id).where(CampaignGoal.campaign_id == campaign_id)))


# -- DTOs -------------------------------------------------------------------------------


async def campaign_dto(session: AsyncSession, c: Campaign) -> dict[str, Any]:
    prow = (await session.execute(
        select(Portfolio, CampaignPortfolio.created, BrokerLink.name, BrokerLink.kind)
        .join(CampaignPortfolio, CampaignPortfolio.portfolio_id == Portfolio.id)
        .outerjoin(BrokerLink, BrokerLink.id == Portfolio.broker_link_id)
        .where(CampaignPortfolio.campaign_id == c.id).order_by(Portfolio.name))).all()
    grows = list(await session.scalars(
        select(Goal).join(CampaignGoal, CampaignGoal.goal_id == Goal.id)
        .where(CampaignGoal.campaign_id == c.id).order_by(Goal.created_at, Goal.id)))
    names = {p.id: p.name for p, *_ in prow}
    return {
        "id": c.id, "name": c.name, "start_date": c.start_date, "end_date": c.end_date,
        "notes": c.notes, "status": c.status, "created_by": c.created_by,
        "created_at": c.created_at, "closed_at": c.closed_at, "closed_by": c.closed_by,
        "portfolios": [{"id": p.id, "name": p.name, "status": p.status,
                        "base_currency": p.base_currency, "created": created,
                        "broker_link_name": link_name, "broker_kind": kind}
                       for p, created, link_name, kind in prow],
        "goals": [{**goals.goal_dto(g), "portfolio_name": names.get(g.portfolio_id)}
                  for g in grows],
    }


async def list_campaigns(session: AsyncSession) -> list[dict[str, Any]]:
    """Open ones first (attiva, bozza), then the concluded; newest start first."""
    rank = {"attiva": 0, "bozza": 1, "conclusa": 2}
    items = sorted(await session.scalars(select(Campaign)),
                   key=lambda c: (rank.get(c.status, 3), -c.start_date.toordinal(), c.name))
    return [await campaign_dto(session, c) for c in items]


# -- the third step's templates ---------------------------------------------------------


def _custom(name: str, kind: str, target: str, start: date, end: date, *,
            portfolio_id: str | None, benchmark: str | None = None) -> dict[str, Any]:
    return {"name": name, "horizon": "custom", "start_date": start.isoformat(),
            "end_date": end.isoformat(), "recurring": False, "scope": "portfolio",
            "portfolio_id": portfolio_id, "target_pct": target, "kind": kind,
            "benchmark": benchmark, "active": True}


async def campaign_templates(session: AsyncSession, body: dict[str, Any], *,
                             today: date) -> list[dict[str, Any]]:
    """The goal templates of the wizard's third step, per campaign portfolio ({key,
    name, portfolio_id?, risk_profile_id?}): first the campaign's own (custom period
    from the campaign start to its end: a minimum, a maximum loss, beating an index),
    then the per-portfolio «Modelli» of the Obiettivi page (`goals.portfolio_templates`,
    starting at the campaign start). Each template: {key, label, portfolio_key, body,
    recommended}. `body.closes_positions` follows the goals' default (true only on the
    campaign minimum, and only when no active piano covers the portfolio already: at most
    one piano per portfolio)."""
    values = header({**body, "name": body.get("name") or "-"}, today=today)
    start, end = values["start_date"], values["end_date"]
    out = []
    for raw in body.get("portfolios") or []:
        key = str(raw.get("key") or raw.get("portfolio_id") or "")
        pid = raw.get("portfolio_id")
        name = raw.get("name") or ""
        profile_id = raw.get("risk_profile_id")
        if pid:
            p = await session.get(Portfolio, _uuid(pid, "portfolio_id"))
            if p is None:
                continue
            name, profile_id = p.name, p.risk_profile_id
        loss = None
        if profile_id:
            loss = await session.scalar(select(RiskProfile.max_daily_loss_pct)
                                        .where(RiskProfile.id == _uuid(profile_id,
                                                                       "risk_profile_id")))
        elif not pid:  # a new portfolio without a profile gets the Default one
            loss = await session.scalar(select(RiskProfile.max_daily_loss_pct)
                                        .where(RiskProfile.name == "Default"))
        dyn = goals._dynamic(loss)
        spid = str(pid) if pid else None
        plan_free = await goals.covering_plan(session, "portfolio", spid) is None
        target, risk = ("8", "10") if dyn else ("3", "5")
        bench = "QQQ" if dyn else "VWCE.DE"
        own = [
            {"key": f"campaign_min:{key}", "label": f"Campagna +{target}% · {name}",
             "body": {**_custom("campagna", "min", target, start, end, portfolio_id=spid),
                      "closes_positions": plan_free},
             "recommended": True},
            {"key": f"campaign_risk:{key}", "label": f"Rischio campagna −{risk}% · {name}",
             "body": {**_custom("rischio campagna", "max_loss", risk, start, end,
                                portfolio_id=spid), "closes_positions": False},
             "recommended": True},
            {"key": f"campaign_beat:{key}",
             "label": f"Battere {bench.removesuffix('.DE')} nella campagna · {name}",
             "body": {**_custom(f"battere {bench.removesuffix('.DE')}", "beat_index", "0",
                                start, end, portfolio_id=spid, benchmark=bench),
                      "closes_positions": False},
             "recommended": False},
        ]
        for t in goals.portfolio_templates(key, None, name, loss, today=start):
            t["body"].update(portfolio_id=spid, closes_positions=False)
            t["recommended"] = False
            own.append(t)
        for t in own:
            t["portfolio_key"] = key
        out += own
    return out


# -- the close wizard -------------------------------------------------------------------


async def close_check(session: AsyncSession, campaign: Campaign) -> dict[str, Any]:
    """What is still open before closing: per portfolio its open positions, open orders,
    broker link and whether it can be archived now (the archive itself is tried in a
    savepoint and rolled back: the reason is the archive rules' own message); the
    plan-close state of the pianos covering its portfolios. Changes nothing."""
    ids = await portfolio_ids(session, campaign.id)
    ports = []
    for pid in ids:
        p = await session.get(Portfolio, pid)
        positions = (await session.execute(
            select(Position.symbol, Position.qty)
            .where(Position.portfolio_id == pid, Position.qty != 0)
            .order_by(Position.symbol))).all()
        orders = list(await session.scalars(
            select(Order).where(Order.portfolio_id == pid, Order.status.in_(OPEN_STATUSES))
            .order_by(Order.created_at)))
        link = await session.get(BrokerLink, p.broker_link_id) if p.broker_link_id else None
        blocker = None
        archived = p.status == PortfolioStatus.archived
        if not archived:
            try:
                async with session.begin_nested() as sp:
                    await archive_portfolio(session, pid, by="verifica")
                    await sp.rollback()
            except SMTError as e:
                blocker = e.message
            discard_post_commit(session)
            await session.refresh(p)
        ports.append({
            "id": pid, "name": p.name, "status": p.status, "archived": archived,
            "broker_link_name": link.name if link else None,
            "positions": [{"symbol": s, "qty": q} for s, q in positions],
            "open_orders": [{"id": o.id, "symbol": o.symbol, "side": o.side, "type": o.type,
                             "status": o.status, "qty": o.qty} for o in orders],
            "can_archive": not archived and blocker is None, "archive_blocker": blocker,
        })
    goal_names = {g.id: g.name for g in await session.scalars(select(Goal))}
    plans = []
    idset = set(ids)
    for st in await plan_states(session):
        positions = [x for x in st["positions"] if x["portfolio_id"] in idset]
        if not positions:
            continue
        counts = dict.fromkeys(("pending", "grace", "closing", "closed"), 0)
        for x in positions:
            counts[x["state"]] += 1
        plans.append({**st, "goal_name": goal_names.get(st["goal_id"]), "counts": counts,
                      "positions": positions})
    return {"campaign_id": campaign.id, "status": campaign.status, "portfolios": ports,
            "plans": plans,
            "open_positions": sum(len(p["positions"]) for p in ports),
            "open_orders": sum(len(p["open_orders"]) for p in ports)}


async def close_campaign(session: AsyncSession, campaign: Campaign, body: dict[str, Any],
                         *, by: str, now: datetime | None = None) -> Campaign:
    """Concludes the campaign, archiving the chosen portfolios first (each through the
    archive rules: the first refusal stops everything, named after its portfolio)."""
    if campaign.status == "conclusa":
        raise _invalid("status", ALREADY_CLOSED)
    ids = set(await portfolio_ids(session, campaign.id))
    archive = body.get("archive_portfolio_ids") or []
    if not isinstance(archive, list):
        raise _invalid("archive_portfolio_ids", "elenco di portafogli non valido")
    archived = []
    for raw in archive:
        pid = _uuid(raw, "archive_portfolio_ids")
        if pid not in ids:
            raise _invalid("archive_portfolio_ids", "portafoglio fuori dalla campagna")
        p = await session.get(Portfolio, pid)
        try:
            await archive_portfolio(session, pid, by=by)
        except SMTError as e:
            raise SMTError(e.code, f"«{p.name}»: {e.message}",
                           {**(e.details or {}), "field": "archive_portfolio_ids"}) from None
        archived.append(str(pid))
    if "notes" in body:
        campaign.notes = _text(body["notes"], "notes", MAX_NOTES_LEN, required=False)
    campaign.status = "conclusa"
    campaign.closed_at = now or datetime.now(UTC)
    campaign.closed_by = by
    await session.flush()
    await write_audit(session, agent_id=None, method="admin:campaign.close",
                      params={"campaign_id": str(campaign.id), "name": campaign.name,
                              "by": by, "archived": archived},
                      outcome="ok", latency_ms=0)
    await session.refresh(campaign)
    return campaign

