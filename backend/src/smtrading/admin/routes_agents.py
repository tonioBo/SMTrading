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

from smtrading.admin.auth import DbSession, RedisClient, require_admin
from smtrading.admin.routes_portfolios import _invalid, _transaction
from smtrading.admin.schemas import (
    AgentCreateIn,
    AgentPatchIn,
    AgentPortfolioIn,
)
from smtrading.agents import service as agents
from smtrading.agents.models import Agent, AgentPortfolio
from smtrading.common.errors import ErrorCode, SMTError
from smtrading.mcp.dto import parse_ts, to_jsonable
from smtrading.portfolios.models import Portfolio

router = APIRouter(prefix="/api", tags=["agents"], dependencies=[Depends(require_admin)])

MAX_NAME_LEN = 128
DEFAULT_AUDIT_LIMIT, MAX_AUDIT_LIMIT = 100, 500
_PERMISSIONS = ("read", "trade")
AGENT_FIELDS = ("id", "name", "active", "created_at", "last_seen_at")


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
            select(AgentPortfolio, Portfolio.name)
            .join(Portfolio, Portfolio.id == AgentPortfolio.portfolio_id)
            .where(AgentPortfolio.agent_id.in_([a.id for a in agents_]))
            .order_by(Portfolio.name)
        )
    ).all()
    by_agent: dict[UUID, list[dict[str, Any]]] = {a.id: [] for a in agents_}
    for link, name in links:
        by_agent[link.agent_id].append(
            {
                "portfolio_id": str(link.portfolio_id),
                "portfolio_name": name,
                "permission": link.permission,
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
    try:
        async with _transaction(session, redis):
            agent, token = await agents.create_agent(session, name)
    except IntegrityError:  # a concurrent creation with the same name won the race
        raise _duplicate_name(name) from None
    except SMTError as e:
        if "already exists" in e.message:
            raise _duplicate_name(name) from None
        raise
    return {"agent": await _agent_dto(session, agent), "token": token}


@router.post("/agents/{agent_id}/rotate")
async def rotate_agent_token(agent_id: UUID, session: DbSession, redis: RedisClient) -> dict:
    await _agent(session, agent_id)
    async with _transaction(session, redis):
        token = await agents.rotate_token(session, agent_id)
    return {"token": token}


@router.patch("/agents/{agent_id}")
async def patch_agent(
    agent_id: UUID, body: AgentPatchIn, session: DbSession, redis: RedisClient
) -> dict:
    await _agent(session, agent_id)
    async with _transaction(session, redis):
        agent = await agents.set_agent_active(session, agent_id, body.active)
    return await _agent_dto(session, agent)


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


