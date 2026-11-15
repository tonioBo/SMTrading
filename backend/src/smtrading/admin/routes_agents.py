"""Admin API: agents (token only on create/rotate), portfolio assignments, MCP audit log.

Token cache invalidation after a commit is automatic (agents.service listener).
"""

from datetime import datetime
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from smtrading.admin.auth import CurrentAdmin, DbSession, RedisClient, require_admin
from smtrading.admin.common import _invalid, _transaction
from smtrading.admin.schemas import (
    AgentCreateIn,
    AgentPatchIn,
    AgentPortfolioIn,
)
from smtrading.agents import service as agents
from smtrading.agents.models import Agent, AgentPortfolio
from smtrading.agents.service import write_audit
from smtrading.common.errors import ErrorCode, SMTError
from smtrading.common.fingerprints import name_warning
from smtrading.mcp.dto import parse_ts, to_jsonable
from smtrading.portfolios.mode import admin_mode_fields, portfolio_links
from smtrading.portfolios.models import Portfolio, PortfolioStatus

router = APIRouter(prefix="/api", tags=["agents"], dependencies=[Depends(require_admin)])

MAX_NAME_LEN = 128
DEFAULT_AUDIT_LIMIT, MAX_AUDIT_LIMIT = 100, 500
_PERMISSIONS = ("read", "trade")
AGENT_FIELDS = ("id", "name", "active", "can_notify", "can_halt", "created_at", "last_seen_at")


def _clean_name(name: str) -> str:
    cleaned = name.strip()
    if not cleaned or len(cleaned) > MAX_NAME_LEN:
        raise _invalid("name", f"il nome deve avere da 1 a {MAX_NAME_LEN} caratteri")
    return cleaned


def _duplicate_name(name: str) -> SMTError:
    return SMTError(
        ErrorCode.INVALID_ORDER, "esiste già un agente con questo nome",
        {"field": "name", "name": name},
    )


async def _agent_dtos(session: AsyncSession, agents_: list[Agent]) -> list[dict[str, Any]]:
    links = (
        await session.execute(
            select(AgentPortfolio, Portfolio)
            .join(Portfolio, Portfolio.id == AgentPortfolio.portfolio_id)
            .where(AgentPortfolio.agent_id.in_([a.id for a in agents_]))
            .order_by(Portfolio.name)
        )
    ).all()
    by_agent: dict[UUID, list[dict[str, Any]]] = {a.id: [] for a in agents_}
    brokers = await portfolio_links(session, [portfolio for _, portfolio in links])
    for link, portfolio in links:
        broker = brokers.get(portfolio.broker_link_id)
        by_agent[link.agent_id].append(
            {
                "portfolio_id": str(link.portfolio_id),
                "portfolio_name": portfolio.name,
                "permission": link.permission,
                "archived": portfolio.status == PortfolioStatus.archived,
                **to_jsonable(admin_mode_fields(portfolio, broker)),
            }
        )
    return [
        {**{f: to_jsonable(getattr(a, f)) for f in AGENT_FIELDS}, "portfolios": by_agent[a.id]}
        for a in agents_
    ]


async def _agent_dto(session: AsyncSession, agent: Agent) -> dict[str, Any]:
    return (await _agent_dtos(session, [agent]))[0]


async def _agent(session: AsyncSession, agent_id: UUID) -> Agent:
    agent = await session.get(Agent, agent_id)
    if agent is None:
        raise SMTError(ErrorCode.NOT_FOUND, "agente non trovato", {"agent_id": str(agent_id)})
    return agent


@router.get("/agents")
async def list_agents(session: DbSession) -> list[dict]:
    rows = list((await session.scalars(select(Agent).order_by(Agent.name))).all())
    return await _agent_dtos(session, rows)


@router.post("/agents")
async def create_agent(body: AgentCreateIn, session: DbSession, redis: RedisClient) -> dict:
    name = _clean_name(body.name)
    if await session.scalar(select(Agent.id).where(Agent.name == name)) is not None:
        raise _duplicate_name(name)
    try:
        async with _transaction(session, redis):
            agent, token = await agents.create_agent(session, name)
    except IntegrityError:  # a concurrent creation with the same name won the race
        raise _duplicate_name(name) from None
    return {"agent": await _agent_dto(session, agent), "token": token,
            "name_warning": name_warning(agent.name)}


@router.post("/agents/{agent_id}/rotate")
async def rotate_agent_token(agent_id: UUID, session: DbSession, redis: RedisClient) -> dict:
    await _agent(session, agent_id)
    async with _transaction(session, redis):
        token = await agents.rotate_token(session, agent_id)
    return {"token": token}


@router.patch("/agents/{agent_id}")
async def patch_agent(
    agent_id: UUID, body: AgentPatchIn, admin: CurrentAdmin, session: DbSession,
    redis: RedisClient,
) -> dict:
    """Only the fields present are changed; a change of `can_notify` / `can_halt` is
    audited."""
    fields = body.model_fields_set
    for name in ("active", "can_notify", "can_halt"):
        if name in fields and getattr(body, name) is None:
            raise _invalid(name, f"{name} non può essere nullo")
    agent = await _agent(session, agent_id)
    async with _transaction(session, redis):
        if "active" in fields:
            agent = await agents.set_agent_active(session, agent_id, body.active)
        if "can_notify" in fields:
            agent = await agents.set_agent_can_notify(session, agent_id, body.can_notify)
            await write_audit(
                session, agent_id=None, method="admin:agent.notify",
                params={"agent_id": str(agent_id), "agent": agent.name,
                        "can_notify": body.can_notify, "by": admin.email},
                outcome="ok", latency_ms=0,
            )
        if "can_halt" in fields:
            agent = await agents.set_agent_can_halt(session, agent_id, body.can_halt)
            await write_audit(
                session, agent_id=None, method="admin:agent.halt",
                params={"agent_id": str(agent_id), "agent": agent.name,
                        "can_halt": body.can_halt, "by": admin.email},
                outcome="ok", latency_ms=0,
            )
    return {**await _agent_dto(session, agent), "name_warning": name_warning(agent.name)}


@router.put("/agents/{agent_id}/portfolios")
async def set_agent_portfolios(
    agent_id: UUID, body: list[AgentPortfolioIn], session: DbSession, redis: RedisClient
) -> dict:
    """Replace the agent's whole set of assignments, atomically."""
    ids = [item.portfolio_id for item in body]
    if len(set(ids)) != len(ids):
        raise _invalid("portfolio_id", "portafoglio duplicato nell'elenco")
    for item in body:
        if item.permission not in _PERMISSIONS:
            raise _invalid("permission", f"il permesso deve essere uno tra {list(_PERMISSIONS)}")
    await _agent(session, agent_id)
    existing = set(await session.scalars(select(Portfolio.id).where(Portfolio.id.in_(ids))))
    missing = [str(i) for i in ids if i not in existing]
    if missing:
        raise SMTError(
            ErrorCode.NOT_FOUND, "portafoglio non trovato",
            {"field": "portfolio_id", "portfolio_ids": missing},
        )
    async with _transaction(session, redis):
        await session.execute(delete(AgentPortfolio).where(AgentPortfolio.agent_id == agent_id))
        session.add_all(
            AgentPortfolio(
                agent_id=agent_id, portfolio_id=item.portfolio_id, permission=item.permission
            )
            for item in body
        )
        await session.flush()
    return await _agent_dto(session, await _agent(session, agent_id))


@router.get("/mcp-audit")
async def mcp_audit(
    session: DbSession,
    agent_id: UUID | None = None,
    method: str | None = None,
    outcome: str | None = None,
    since: str | None = None,
    limit: Annotated[int, Query(ge=1, le=MAX_AUDIT_LIMIT)] = DEFAULT_AUDIT_LIMIT,
) -> list[dict]:
    since_ts: datetime | None = parse_ts(since, "since")
    rows = await agents.list_audit(
        session, agent_id=agent_id, method=method, outcome=outcome, since=since_ts, limit=limit
    )
    fields = ("id", "agent_id", "method", "params", "outcome", "latency_ms", "created_at")
    return [{f: to_jsonable(getattr(r, f)) for f in fields} for r in rows]


