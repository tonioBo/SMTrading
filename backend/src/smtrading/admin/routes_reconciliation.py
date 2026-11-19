"""Admin API «Riconciliazione» (plan 21): every non-archived portfolio with the last check
of its broker account (smtrading.reconciliation), one portfolio's history, and «Riconcilia
ora» (admins only, by require_access; read-only broker calls, own sessions, no
transaction open across the broker I/O). A portfolio without a broker link is listed as
not linked; a real-money link is read through its live gate."""

import asyncio
from collections import Counter
from typing import Any
from uuid import UUID

import httpx
from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from smtrading.admin.auth import DbSession, require_access
from smtrading.admin.common import Quotes
from smtrading.admin.routes_live import live_http_factory
from smtrading.brokers.live import LIVE_KINDS
from smtrading.brokers.models import BrokerLink
from smtrading.common.errors import ErrorCode, SMTError
from smtrading.mcp.dto import parse_int, to_jsonable
from smtrading.portfolios.models import Portfolio, PortfolioStatus
from smtrading.reconciliation.compare import UNVERIFIABLE
from smtrading.reconciliation.models import ReconciliationCheck
from smtrading.reconciliation.service import check_all, check_link

router = APIRouter(prefix="/api", tags=["reconciliation"],
                   dependencies=[Depends(require_access)])

RUN_TIMEOUT_S = 90.0
DEFAULT_HISTORY = 20
MAX_HISTORY = 200


class RunIn(BaseModel):
    portfolio_id: UUID | None = None  # None: every linked portfolio


def _counts(row: ReconciliationCheck) -> dict[str, int]:
    found = Counter(str(i.get("status")) for i in row.items or [])
    return {s: found.get(s, 0) for s in ("ok", "warning", "mismatch", "unverifiable")}


def check_dto(row: ReconciliationCheck | None, *, items: bool = True) -> dict | None:
    if row is None:
        return None
    out: dict[str, Any] = {
        "id": row.id, "ts": row.ts, "status": row.status, "trigger": row.trigger,
        "error": row.error, "meta": row.meta or {}, "counts": _counts(row),
    }
    if items:
        out["items"] = row.items or []
    return out


async def _latest(session: AsyncSession, pid: UUID, *, verified: bool = False
                  ) -> ReconciliationCheck | None:
    q = select(ReconciliationCheck).where(ReconciliationCheck.portfolio_id == pid)
    if verified:
        q = q.where(ReconciliationCheck.status != UNVERIFIABLE)
    return await session.scalar(
        q.order_by(ReconciliationCheck.ts.desc(), ReconciliationCheck.id.desc()).limit(1))


async def _portfolio_dto(session: AsyncSession, p: Portfolio,
                         links: dict[UUID, BrokerLink]) -> dict:
    link = links.get(p.broker_link_id) if p.broker_link_id else None
    latest = await _latest(session, p.id)
    verified = latest if latest is None or latest.status != UNVERIFIABLE else \
        await _latest(session, p.id, verified=True)
    return {
        "portfolio_id": p.id, "portfolio_name": p.name, "linked": link is not None,
        "link": None if link is None else {
            "id": link.id, "name": link.name, "kind": link.kind,
            "live": link.kind in LIVE_KINDS, "enabled": link.enabled},
        "latest": check_dto(latest),
        "last_verified_at": None if verified is None else verified.ts,
    }


async def _links(session: AsyncSession) -> dict[UUID, BrokerLink]:
    return {link.id: link for link in await session.scalars(select(BrokerLink))}


async def _portfolio(session: AsyncSession, pid: UUID) -> Portfolio:
    p = await session.get(Portfolio, pid)
    if p is None or p.status == PortfolioStatus.archived:
        raise SMTError(ErrorCode.NOT_FOUND, "portafoglio non trovato", {"portfolio_id": str(pid)})
    return p


@router.get("/reconciliation")
async def list_reconciliation(session: DbSession) -> dict:
    """Every non-archived portfolio, linked first, with its last check."""
    links = await _links(session)
    portfolios = list(await session.scalars(
        select(Portfolio).where(Portfolio.status != PortfolioStatus.archived)
        .order_by(Portfolio.name)))
    out = [await _portfolio_dto(session, p, links) for p in portfolios]
    out.sort(key=lambda d: not d["linked"])
    return to_jsonable({"portfolios": out})


@router.get("/reconciliation/{portfolio_id}")
async def get_reconciliation(portfolio_id: UUID, session: DbSession,
                             limit: str | None = None) -> dict:
    """The portfolio with its last check (items) and its history, newest first."""
    n = DEFAULT_HISTORY if limit is None else parse_int(limit, "limit", 1, MAX_HISTORY)
    p = await _portfolio(session, portfolio_id)
    rows = await session.scalars(
        select(ReconciliationCheck).where(ReconciliationCheck.portfolio_id == p.id)
        .order_by(ReconciliationCheck.ts.desc(), ReconciliationCheck.id.desc()).limit(n))
    out = await _portfolio_dto(session, p, await _links(session))
    out["history"] = [check_dto(r, items=False) for r in rows]
    return to_jsonable(out)


@router.post("/reconciliation/run")
async def run_reconciliation(body: RunIn, request: Request, session: DbSession,
                             qs: Quotes) -> dict:
    """«Riconcilia ora»: one portfolio's link (every portfolio on it) or every enabled
    link. Read-only broker calls; a broker failure is a «non verificabile» check, not an
    error. Answers the list of GET /reconciliation."""
    link_id: UUID | None = None
    if body.portfolio_id is not None:
        p = await _portfolio(session, body.portfolio_id)
        if p.broker_link_id is None:
            raise SMTError(ErrorCode.INVALID_ORDER, "portafoglio non collegato a un broker",
                           {"field": "portfolio_id"})
        link = await session.get(BrokerLink, p.broker_link_id)
        if link is None or not link.enabled:
            raise SMTError(ErrorCode.INVALID_ORDER, "collegamento broker disattivato",
                           {"field": "portfolio_id"})
        link_id = link.id
    await session.commit()  # the checks use their own sessions (no tx across the I/O)
    sm = request.app.state.sessionmaker
    live = live_http_factory(request)
    async with httpx.AsyncClient() as http:
        if link_id is not None:
            work = check_link(sm, link_id, qs, http=http, live_http_factory=live,
                              trigger="manual")
        else:
            work = check_all(sm, qs, http=http, live_http_factory=live, trigger="manual")
        try:
            await asyncio.wait_for(work, RUN_TIMEOUT_S)
        except TimeoutError:
            raise SMTError(ErrorCode.PROVIDER_UNAVAILABLE,
                           "il broker non ha risposto in tempo") from None
    return await list_reconciliation(session)
