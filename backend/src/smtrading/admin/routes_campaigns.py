"""Admin API «Campagne» (campaigns.service): reads for every user, writes for admins
(router level). Never an MCP tool: agents see a campaign's end through its goals.

- `GET /api/campaigns`: every campaign (open first) with its portfolios and goals;
- `GET /api/campaigns/{campaign_id}`;
- `POST /api/campaigns/templates`: the goal templates of the wizard's third step for
  {start_date, end_date, portfolios: [{key, name, portfolio_id?, risk_profile_id?}]};
- `POST /api/campaigns`: the wizard's confirm, one transaction: {name, start_date,
  end_date, notes?, status? (bozza | attiva), portfolios: [{key, portfolio_id (existing)
  | name, initial_cash, base_currency, risk_profile_id (new), broker_link_id? (paper/demo
  only)}], goals: [{portfolio_key, name, horizon, start_date, end_date, recurring,
  target_pct, kind, benchmark, closes_positions}]}; a dedicated (eToro demo) account is
  checked empty before the transaction, like `PUT /api/portfolios/{id}/broker-link`;
- `PATCH /api/campaigns/{campaign_id}`: name, notes, status bozza → attiva;
- `GET /api/campaigns/{campaign_id}/close-check`: open positions and orders, archive
  blockers, plan-close state;
- `POST /api/campaigns/{campaign_id}/close`: {archive_portfolio_ids, notes?};
- `GET /api/campaigns/{campaign_id}/report.pdf`: the campaign's «Resoconto» (its dates
  and portfolios, archived ones included)."""

import asyncio
from datetime import UTC, date, datetime
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends
from fastapi.responses import Response
from pydantic import BaseModel

from smtrading.admin.auth import CurrentAdmin, DbSession, RedisClient, require_access
from smtrading.admin.common import Quotes, _transaction
from smtrading.admin.routes_brokers import _require_dedicated
from smtrading.admin.routes_reports import pdf_response
from smtrading.campaigns import service
from smtrading.common.config import get_settings
from smtrading.mcp.dto import to_jsonable
from smtrading.reports.pdf import render_pdf
from smtrading.reports.period import period_report

router = APIRouter(prefix="/api", tags=["campaigns"], dependencies=[Depends(require_access)])


class CampaignIn(BaseModel):
    name: str | None = None
    start_date: str | None = None
    end_date: str | None = None
    notes: str | None = None
    status: str | None = None
    portfolios: list[dict[str, Any]] = []
    goals: list[dict[str, Any]] = []


class CampaignPatchIn(BaseModel):
    """Only the fields sent change (`model_fields_set`)."""

    name: str | None = None
    notes: str | None = None
    status: str | None = None


class CloseIn(BaseModel):
    archive_portfolio_ids: list[str] = []
    notes: str | None = None


def _now() -> datetime:
    return datetime.now(UTC)


def _zone() -> ZoneInfo:
    return ZoneInfo(get_settings().tz)


def _today() -> date:
    return _now().astimezone(_zone()).date()


@router.get("/campaigns")
async def list_campaigns(session: DbSession) -> list[dict[str, Any]]:
    return to_jsonable(await service.list_campaigns(session))


@router.post("/campaigns/templates")
async def campaign_templates(body: CampaignIn, session: DbSession) -> list[dict[str, Any]]:
    return to_jsonable(await service.campaign_templates(
        session, body.model_dump(exclude_unset=True), today=_today()))


@router.post("/campaigns")
async def create_campaign(body: CampaignIn, admin: CurrentAdmin, session: DbSession,
                          redis: RedisClient) -> dict[str, Any]:
    raw = body.model_dump(exclude_unset=True)
    for link_id in service.link_ids(raw):  # broker I/O before the transaction (R9)
        await _require_dedicated(session, link_id)
    async with _transaction(session, redis):
        campaign = await service.create_campaign(session, raw, by=admin.email,
                                                 today=_today())
        out = to_jsonable(await service.campaign_dto(session, campaign))
    return out


@router.get("/campaigns/{campaign_id}")
async def get_campaign(campaign_id: UUID, session: DbSession) -> dict[str, Any]:
    c = await service.get_campaign(session, campaign_id)
    return to_jsonable(await service.campaign_dto(session, c))


@router.patch("/campaigns/{campaign_id}")
async def patch_campaign(campaign_id: UUID, body: CampaignPatchIn, admin: CurrentAdmin,
                         session: DbSession, redis: RedisClient) -> dict[str, Any]:
    async with _transaction(session, redis):
        c = await service.get_campaign(session, campaign_id)
        await service.update_campaign(session, c, body.model_dump(exclude_unset=True),
                                      by=admin.email)
        out = to_jsonable(await service.campaign_dto(session, c))
    return out


@router.get("/campaigns/{campaign_id}/close-check")
async def close_check(campaign_id: UUID, session: DbSession) -> dict[str, Any]:
    c = await service.get_campaign(session, campaign_id)
    try:
        return to_jsonable(await service.close_check(session, c))
    finally:
        await session.rollback()  # the archive dry runs leave nothing behind


@router.post("/campaigns/{campaign_id}/close")
async def close_campaign(campaign_id: UUID, body: CloseIn, admin: CurrentAdmin,
                         session: DbSession, redis: RedisClient) -> dict[str, Any]:
    async with _transaction(session, redis):
        c = await service.get_campaign(session, campaign_id)
        await service.close_campaign(session, c, body.model_dump(exclude_unset=True),
                                     by=admin.email, now=_now())
        out = to_jsonable(await service.campaign_dto(session, c))
    return out


@router.get("/campaigns/{campaign_id}/report.pdf")
async def campaign_report(campaign_id: UUID, session: DbSession, qs: Quotes) -> Response:
    c = await service.get_campaign(session, campaign_id)
    dto = await service.campaign_dto(session, c)
    data = await period_report(
        session, qs, start=c.start_date, end=c.end_date,
        portfolio_ids=[p["id"] for p in dto["portfolios"]], now=_now(), zone=_zone(),
        campaign=dto, campaign_goal_ids=await service.goal_ids(session, c.id))
    return pdf_response(data, await asyncio.to_thread(render_pdf, data))
