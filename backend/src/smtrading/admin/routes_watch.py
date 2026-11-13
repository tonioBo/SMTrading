"""Admin API: an agent's watchlists and price alerts (read, remove a symbol, delete a
watchlist, cancel an alert).

Reuses watch.service, so the streaming rules are the agent's own: a symbol is untracked
(post-commit) only when no other watchlist / active alert still references it. Every change
writes an audit row (`admin:watchlist.*`, `admin:alert.cancel`, by the admin's email) and a
notification addressed to the agent (`admin.watch_changed`), which the agent reads with
get_triggered_alerts. A watchlist or alert of another agent is answered NOT_FOUND.
"""

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from smtrading.admin.auth import CurrentAdmin, DbSession, RedisClient, require_admin
from smtrading.admin.common import _transaction
from smtrading.agents.models import Agent
from smtrading.agents.service import write_audit
from smtrading.common.errors import ErrorCode, SMTError
from smtrading.events import notify
from smtrading.instruments.service import canonical_symbol
from smtrading.mcp.dto import alert_dto, to_jsonable
from smtrading.watch import service as watch
from smtrading.watch.models import Alert, Watchlist, WatchlistItem

router = APIRouter(prefix="/api", tags=["watch"], dependencies=[Depends(require_admin)])

NOTIFICATION_KIND = "admin.watch_changed"



async def _agent(session: AsyncSession, agent_id: UUID) -> Agent:
    agent = await session.get(Agent, agent_id)
    if agent is None:
        raise SMTError(ErrorCode.NOT_FOUND, "agente non trovato", {"agent_id": str(agent_id)})
    return agent


async def _watchlist(session: AsyncSession, agent_id: UUID, watchlist_id: UUID) -> Watchlist:
    wl = await session.get(Watchlist, watchlist_id)
    if wl is None or wl.agent_id != agent_id:
        raise SMTError(ErrorCode.NOT_FOUND, "watchlist non trovata",
                       {"watchlist_id": str(watchlist_id)})
    return wl


async def _changed(
    session: AsyncSession, admin_email: str, agent: Agent, method: str, payload: dict[str, Any]
) -> None:
    await write_audit(
        session, agent_id=None, method=method,
        params={"agent_id": str(agent.id), "agent": agent.name, "by": admin_email,
                **{k: str(v) for k, v in payload.items()}},
        outcome="ok", latency_ms=0,
    )
    await notify(session, portfolio_id=None, agent_id=str(agent.id), kind=NOTIFICATION_KIND,
                 payload=payload)


@router.get("/agents/{agent_id}/watchlists")
async def get_watchlists(agent_id: UUID, session: DbSession) -> list[dict]:
    await _agent(session, agent_id)
    return [to_jsonable(w) for w in await watch.list_watchlists(session, agent_id)]


@router.get("/agents/{agent_id}/alerts")
async def get_alerts(agent_id: UUID, session: DbSession) -> list[dict]:
    """Active and triggered alerts (deleted ones are hidden), oldest first."""
    await _agent(session, agent_id)
    return [to_jsonable(alert_dto(a)) for a in await watch.list_alerts(session, agent_id)]


@router.delete("/agents/{agent_id}/watchlists/{watchlist_id}/items/{symbol}")
async def delete_watchlist_item(
    agent_id: UUID, watchlist_id: UUID, symbol: str, admin: CurrentAdmin,
    session: DbSession, redis: RedisClient,
) -> dict:
    agent = await _agent(session, agent_id)
    wl = await _watchlist(session, agent_id, watchlist_id)
    sym = canonical_symbol(symbol)
    present = await session.scalar(
        select(WatchlistItem.symbol)
        .where(WatchlistItem.watchlist_id == wl.id, WatchlistItem.symbol == sym)
    )
    if present is None:
        raise SMTError(ErrorCode.NOT_FOUND, "simbolo non presente nella watchlist",
                       {"symbol": sym})
    async with _transaction(session, redis):
        out = await watch.remove_from_watchlist(session, agent_id, wl.id, [sym])
        await _changed(session, admin.email, agent, "admin:watchlist.remove_symbol",
                       {"action": "remove_symbol", "watchlist": wl.name,
                        "watchlist_id": str(wl.id), "symbol": sym})
    return to_jsonable(out)


@router.delete("/agents/{agent_id}/watchlists/{watchlist_id}")
async def delete_watchlist(
    agent_id: UUID, watchlist_id: UUID, admin: CurrentAdmin, session: DbSession,
    redis: RedisClient,
) -> dict:
    agent = await _agent(session, agent_id)
    wl = await _watchlist(session, agent_id, watchlist_id)
    name = wl.name
    async with _transaction(session, redis):
        await watch.delete_watchlist(session, agent_id, wl.id)
        await _changed(session, admin.email, agent, "admin:watchlist.delete",
                       {"action": "delete_watchlist", "watchlist": name,
                        "watchlist_id": str(watchlist_id)})
    return {"id": str(watchlist_id), "deleted": True}


@router.post("/agents/{agent_id}/alerts/{alert_id}/cancel")
async def cancel_alert(
    agent_id: UUID, alert_id: UUID, admin: CurrentAdmin, session: DbSession,
    redis: RedisClient,
) -> dict:
    agent = await _agent(session, agent_id)
    alert = await session.get(Alert, alert_id)
    if alert is None or alert.agent_id != agent_id or alert.status == watch.DELETED:
        raise SMTError(ErrorCode.NOT_FOUND, "alert non trovato", {"alert_id": str(alert_id)})
    async with _transaction(session, redis):
        alert = await watch.delete_alert(session, agent_id, alert_id)
        await _changed(session, admin.email, agent, "admin:alert.cancel",
                       {"action": "cancel_alert", "alert_id": str(alert_id),
                        "symbol": alert.symbol})
    return to_jsonable(alert_dto(alert))
