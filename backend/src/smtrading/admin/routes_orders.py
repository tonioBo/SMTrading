"""Admin API: manual orders from the portal (plan 9).

    POST /api/portfolios/{id}/orders/preview   the pre-trade check, nothing stored, no TOTP
    POST /api/portfolios/{id}/orders           place (Idempotency-Key: scope "order")

A manual order is a full order: `source="admin"`, no agent, and every check an agent's
order goes through (risk limits, kill switch and «Blocco aperture» — only closes pass —
and on a real-money portfolio the live checks and BrokerSync). On a real-money portfolio
(linked to a LIVE_KINDS link, dry run included) placing needs an admin with TOTP on
(else FORBIDDEN «Attiva prima l'autenticazione a due fattori») and a valid, unused code;
both are checked under the portfolio lock and before anything is written.

A refused order is stored as rejected (like an agent's), committed with its audit row and
returned as the error with its code. Every placement that reached the engine is audited
as `admin:order.place`, outcome "ok" or the rejection code.
"""

from decimal import Decimal
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from smtrading.admin.auth import CurrentAdmin, DbSession, RedisClient, require_access
from smtrading.admin.common import Quotes, _transaction
from smtrading.admin.idempotency import Idempotency, idempotent
from smtrading.admin.routes_portfolios import _order_dto, _portfolio
from smtrading.agents.service import write_audit
from smtrading.brokers.live.service import _require_totp_enabled, _verify_totp
from smtrading.common.errors import SMTError
from smtrading.engine.models import Order, OrderSide, OrderType, TimeInForce
from smtrading.engine.service import PlaceOrderRequest, place_order, preview_order
from smtrading.mcp.dto import parse_enum, to_jsonable
from smtrading.portfolios.locks import lock_live_portfolio
from smtrading.risk.live_checks import live_link_of

router = APIRouter(prefix="/api", tags=["orders"], dependencies=[Depends(require_access)])


class OrderIn(BaseModel):
    symbol: str
    side: str
    type: str
    tif: str = "gtc"
    qty: Decimal | None = None
    notional: Decimal | None = None
    limit_price: Decimal | None = None
    stop_price: Decimal | None = None
    trail_pct: Decimal | None = None
    trail_amount: Decimal | None = None
    # plan 16: an ISO datetime with offset or "close" (checked by the engine)
    valid_until: str | None = None
    totp: str | None = None


def _request(portfolio_id: UUID, body: OrderIn) -> PlaceOrderRequest:
    return PlaceOrderRequest(
        portfolio_id=portfolio_id, symbol=body.symbol.strip(),
        side=parse_enum(OrderSide, body.side, "side"),  # type: ignore[arg-type]
        type=parse_enum(OrderType, body.type, "type"),  # type: ignore[arg-type]
        tif=parse_enum(TimeInForce, body.tif, "tif"),  # type: ignore[arg-type]
        qty=body.qty, notional=body.notional, limit_price=body.limit_price,
        stop_price=body.stop_price, trail_pct=body.trail_pct, trail_amount=body.trail_amount,
        agent_id=None, source="admin", valid_until=body.valid_until,
    )


@router.post("/portfolios/{portfolio_id}/orders/preview")
async def preview(
    portfolio_id: UUID, body: OrderIn, session: DbSession, redis: RedisClient, qs: Quotes,
) -> dict:
    """{ok, code, message, estimated_*, buying_power_after_eur, real, currency}: a refused
    check is ok=false with its code, not an error."""
    portfolio = await _portfolio(session, portfolio_id)
    result = await preview_order(session, qs, _request(portfolio_id, body), redis=redis)
    real = await live_link_of(session, portfolio) is not None
    return {**to_jsonable(result), "real": real, "currency": portfolio.base_currency}


async def _audit(session: AsyncSession, req: PlaceOrderRequest, order: Order, *, real: bool,
                 by: str, outcome: str) -> None:
    await write_audit(
        session, agent_id=None, method="admin:order.place",
        params={"portfolio_id": req.portfolio_id, "symbol": order.symbol,
                "side": req.side, "type": req.type, "qty": req.qty, "notional": req.notional,
                "valid_until": order.valid_until, "real": real, "order_id": order.id, "status": order.status, "by": by},
        outcome=outcome, latency_ms=0,
    )


@router.post("/portfolios/{portfolio_id}/orders")
async def place(
    portfolio_id: UUID, body: OrderIn, admin: CurrentAdmin, session: DbSession,
    redis: RedisClient, qs: Quotes, idem: Annotated[Idempotency, Depends(idempotent("order"))],
):
    """The placed order (the DTO of GET /api/portfolios/{id}/orders)."""
    req = _request(portfolio_id, body)

    async def run() -> dict[str, Any]:
        rejection: SMTError | None = None
        async with _transaction(session, redis):
            portfolio = await lock_live_portfolio(session, portfolio_id)
            real = await live_link_of(session, portfolio) is not None
            if real:  # Review Focus 5: before any write
                _require_totp_enabled(admin)
                await _verify_totp(redis, admin, body.totp)
            try:
                order = await place_order(session, qs, req, redis=redis)
            except SMTError as e:
                if not e.persisted:
                    raise
                rejection = e  # the rejected order stays stored (like run_tool)
                order = await session.get(Order, UUID((e.details or {})["order_id"]))
            await _audit(session, req, order, real=real, by=admin.email,
                         outcome="ok" if rejection is None else str(rejection.code))
            await session.refresh(order)
            out = _order_dto(order)
        if rejection is not None:
            raise rejection
        return out

    return await idem.run(run)
