"""Order tools: preview_order, place_order, place_bracket_order, place_oco_order,
modify_order, cancel_order, cancel_all_orders, get_order, list_orders, close_position,
close_all_positions.

Mutating tools need `trade` on the portfolio, get_order/list_orders/preview_order need
`read`; an order_id tool checks the permission on the order's own portfolio (a missing
order is NOT_FOUND). Orders are placed with source="agent" and the calling agent's id.
The placing tools commit on error too, so rejected orders are stored (run_tool).
"""

from typing import Any, Literal
from uuid import UUID

from mcp.server.mcpserver import MCPServer
from mcp.types import CallToolResult
from sqlalchemy import select

from smtrading.agents.service import require_permission
from smtrading.common.errors import ErrorCode, SMTError
from smtrading.engine import service as engine
from smtrading.engine.models import Fill, Order, OrderSide, OrderStatus, OrderType, TimeInForce
from smtrading.engine.service import PlaceOrderRequest
from smtrading.mcp.dto import (
    fill_dto,
    order_dto,
    parse_decimal,
    parse_enum,
    parse_int,
    parse_uuid,
)
from smtrading.mcp.instruments import ensure_instrument
from smtrading.mcp.runtime import ToolCtx, run_tool

MAX_ORDERS = 500
SOURCE = "agent"
_OCO_LEG_KEYS = frozenset({"type", "limit_price", "stop_price"})

Num = str | float | None


def _invalid(field: str, message: str) -> SMTError:
    return SMTError(ErrorCode.INVALID_ORDER, message, {"field": field})


def _required[T](value: T | None, field: str) -> T:
    if value is None:
        raise _invalid(field, f"{field} is required")
    return value


def _str(v: Any, field: str, *, required: bool = False) -> str | None:
    if v is None:
        return _required(None, field) if required else None
    if not isinstance(v, str) or not v.strip():
        raise _invalid(field, f"{field} must be a non-empty string")
    return v.strip()


async def _portfolio(ctx: ToolCtx, portfolio_id: Any, needed: Literal["read", "trade"]) -> UUID:
    pid = _required(parse_uuid(portfolio_id, "portfolio_id"), "portfolio_id")
    await require_permission(ctx.session, ctx.agent_id, pid, needed)
    return pid


async def _order(ctx: ToolCtx, order_id: Any, needed: Literal["read", "trade"]) -> Order:
    oid = _required(parse_uuid(order_id, "order_id"), "order_id")
    order = await engine.get_order(ctx.session, oid)
    await require_permission(ctx.session, ctx.agent_id, order.portfolio_id, needed)
    return order


def _request(
    ctx: ToolCtx, pid: UUID, symbol: Any, side: Any, type: Any, *, qty: Num = None,
    notional: Num = None, limit_price: Num = None, stop_price: Num = None,
    trail_pct: Num = None, trail_amount: Num = None, tif: Any = "gtc",
    client_order_id: Any = None, valid_until: Any = None,
) -> PlaceOrderRequest:
    return PlaceOrderRequest(
        portfolio_id=pid,
        symbol=_str(symbol, "symbol", required=True),  # type: ignore[arg-type]
        side=_required(parse_enum(OrderSide, side, "side"), "side"),
        type=_required(parse_enum(OrderType, type, "type"), "type"),
        tif=_required(parse_enum(TimeInForce, tif, "tif"), "tif"),
        qty=parse_decimal(qty, "qty"),
        notional=parse_decimal(notional, "notional"),
        limit_price=parse_decimal(limit_price, "limit_price"),
        stop_price=parse_decimal(stop_price, "stop_price"),
        trail_pct=parse_decimal(trail_pct, "trail_pct"),
        trail_amount=parse_decimal(trail_amount, "trail_amount"),
        client_order_id=_str(client_order_id, "client_order_id"),
        agent_id=str(ctx.agent_id),
        source=SOURCE,
        valid_until=valid_until,  # checked by the engine (INVALID_ORDER, field valid_until)
    )


def _oco_leg(
    ctx: ToolCtx, pid: UUID, field: str, leg: Any, symbol: Any, side: Any, qty: Num, tif: Any
) -> PlaceOrderRequest:
    if not isinstance(leg, dict):
        raise _invalid(field, f"{field} must be an object {{type, limit_price?, stop_price?}}")
    unknown = set(leg) - _OCO_LEG_KEYS
    if unknown:
        raise _invalid(field, f"{field} has unknown keys: {sorted(unknown)}")
    return _request(
        ctx, pid, symbol, side, leg.get("type"), qty=_required(qty, "qty"),
        limit_price=leg.get("limit_price"), stop_price=leg.get("stop_price"), tif=tif,
    )


def _parse_statuses(v: str | list[str] | None) -> list[OrderStatus] | None:
    if v is None:
        return None
    values = [v] if isinstance(v, str) else v
    if not isinstance(values, list) or not values:
        raise _invalid("status", "status must be an order status or a non-empty list of them")
    return [parse_enum(OrderStatus, s, "status") for s in values]  # type: ignore[misc]


async def _loaded[T: Order | list[Order]](ctx: ToolCtx, result: T) -> T:
    """Reload the orders so server-side values (created_at, onupdate updated_at) expired
    by a flush are present: the result is serialised without lazy IO."""
    for order in result if isinstance(result, list) else [result]:
        await ctx.session.refresh(order)
    return result


async def _fills(ctx: ToolCtx, order_id: UUID) -> list[dict]:
    rows = await ctx.session.scalars(
        select(Fill).where(Fill.order_id == order_id).order_by(Fill.created_at, Fill.id)
    )
    return [fill_dto(f) for f in rows]


def register(mcp: MCPServer) -> None:
    @mcp.tool(structured_output=False)
    async def preview_order(
        portfolio_id: str, symbol: str, side: str, type: str, qty: Num = None,
        notional: Num = None, limit_price: Num = None, stop_price: Num = None,
        trail_pct: Num = None, trail_amount: Num = None,
    ) -> CallToolResult:
        """Run the pre-trade checks without placing anything: {ok, code, message,
        estimated_price, estimated_notional_eur, estimated_fee, estimated_fx_fee_eur,
        buying_power_after_eur}. A refused check is ok=false with its code, not an error.
        side: buy|sell; type: market|limit|stop|stop_limit|trailing_stop; give qty or
        notional (market only). Needs read."""
        params = {
            "portfolio_id": portfolio_id, "symbol": symbol, "side": side, "type": type,
            "qty": qty, "notional": notional, "limit_price": limit_price,
            "stop_price": stop_price, "trail_pct": trail_pct, "trail_amount": trail_amount,
        }

        async def fn(ctx: ToolCtx) -> Any:
            pid = await _portfolio(ctx, portfolio_id, "read")
            req = _request(
                ctx, pid, symbol, side, type, qty=qty, notional=notional,
                limit_price=limit_price, stop_price=stop_price, trail_pct=trail_pct,
                trail_amount=trail_amount,
            )
            await ensure_instrument(ctx, req.symbol)
            return await engine.preview_order(ctx.session, ctx.qs, req, redis=ctx.redis)

        return await run_tool("preview_order", params, fn)

    @mcp.tool(structured_output=False)
    async def place_order(
        portfolio_id: str, symbol: str, side: str, type: str, qty: Num = None,
        notional: Num = None, limit_price: Num = None, stop_price: Num = None,
        trail_pct: Num = None, trail_amount: Num = None, tif: str = "gtc",
        client_order_id: str | None = None, valid_until: str | None = None,
    ) -> CallToolResult:
        """Place an order; returns the order. side: buy|sell; type: market|limit|stop|
        stop_limit|trailing_stop; tif: gtc|day. Give qty, or notional for a market order;
        limit_price for limit/stop_limit, stop_price for stop/stop_limit, trail_pct or
        trail_amount for trailing_stop. A refused order is stored as rejected and returned
        as an error with its code (e.g. INSUFFICIENT_BUYING_POWER). Re-sending a
        client_order_id returns the same order (or the same error). Needs trade.
        valid_until (optional): an ISO 8601 datetime with an offset (e.g.
        2026-11-18T21:55:00+01:00) or "close" (today's regular session close of the
        market; not for crypto or FX). Still unfilled then, the order is canceled and becomes
        expired; a part already filled stands."""
        params = {
            "portfolio_id": portfolio_id, "symbol": symbol, "side": side, "type": type,
            "qty": qty, "notional": notional, "limit_price": limit_price,
            "stop_price": stop_price, "trail_pct": trail_pct, "trail_amount": trail_amount,
            "tif": tif, "client_order_id": client_order_id, "valid_until": valid_until,
        }

        async def fn(ctx: ToolCtx) -> Any:
            pid = await _portfolio(ctx, portfolio_id, "trade")
            req = _request(
                ctx, pid, symbol, side, type, qty=qty, notional=notional,
                limit_price=limit_price, stop_price=stop_price, trail_pct=trail_pct,
                trail_amount=trail_amount, tif=tif, client_order_id=client_order_id,
                valid_until=valid_until,
            )
            await ensure_instrument(ctx, req.symbol)
            order = await engine.place_order(ctx.session, ctx.qs, req, redis=ctx.redis)
            return await _loaded(ctx, order)

        return await run_tool("place_order", params, fn, commit_on_error=True)

    @mcp.tool(structured_output=False)
    async def place_bracket_order(
        portfolio_id: str, symbol: str, side: str, qty: Num = None, take_profit: Num = None,
        stop_loss: Num = None, type: str = "market", limit_price: Num = None, tif: str = "gtc",
        client_order_id: str | None = None, valid_until: str | None = None,
    ) -> CallToolResult:
        """Entry (market or limit) plus a take_profit limit and a stop_loss stop exit, in one
        OCO group, both reduce_only and held until the entry fills. Returns
        [entry, take_profit, stop_loss]. For a buy: take_profit > entry > stop_loss.
        Needs trade. valid_until (optional, the entry only): an ISO 8601 datetime with an
        offset or "close" (today's regular session close of the market; not for crypto or FX).
        An entry still unfilled then is canceled and becomes expired, with its exits; if
        part of it filled, the exits keep protecting that part with their own tif."""
        params = {
            "portfolio_id": portfolio_id, "symbol": symbol, "side": side, "qty": qty,
            "take_profit": take_profit, "stop_loss": stop_loss, "type": type,
            "limit_price": limit_price, "tif": tif, "client_order_id": client_order_id,
            "valid_until": valid_until,
        }

        async def fn(ctx: ToolCtx) -> Any:
            pid = await _portfolio(ctx, portfolio_id, "trade")
            req = _request(
                ctx, pid, symbol, side, type, qty=_required(qty, "qty"),
                limit_price=limit_price, tif=tif, client_order_id=client_order_id,
                valid_until=valid_until,
            )
            tp = _required(parse_decimal(take_profit, "take_profit"), "take_profit")
            sl = _required(parse_decimal(stop_loss, "stop_loss"), "stop_loss")
            await ensure_instrument(ctx, req.symbol)
            orders = await engine.place_bracket_order(
                ctx.session, ctx.qs, req, take_profit=tp, stop_loss=sl, redis=ctx.redis
            )
            return await _loaded(ctx, orders)

        return await run_tool("place_bracket_order", params, fn, commit_on_error=True)

    @mcp.tool(structured_output=False)
    async def place_oco_order(
        portfolio_id: str, symbol: str, side: str, qty: Num = None, a: dict | None = None,
        b: dict | None = None, tif: str = "gtc",
    ) -> CallToolResult:
        """Two resting orders (same symbol, side, qty) in one OCO group: when one fills the
        other is canceled. a and b: {type: limit|stop|stop_limit, limit_price?,
        stop_price?}; market legs are not allowed. If either leg is refused both are
        stored as rejected. Returns [a, b]. Needs trade."""
        params = {
            "portfolio_id": portfolio_id, "symbol": symbol, "side": side, "qty": qty,
            "a": a, "b": b, "tif": tif,
        }

        async def fn(ctx: ToolCtx) -> Any:
            pid = await _portfolio(ctx, portfolio_id, "trade")
            leg_a = _oco_leg(ctx, pid, "a", a, symbol, side, qty, tif)
            leg_b = _oco_leg(ctx, pid, "b", b, symbol, side, qty, tif)
            await ensure_instrument(ctx, leg_a.symbol)
            orders = await engine.place_oco_order(
                ctx.session, ctx.qs, leg_a, leg_b, redis=ctx.redis
            )
            return await _loaded(ctx, orders)

        return await run_tool("place_oco_order", params, fn, commit_on_error=True)

    @mcp.tool(structured_output=False)
    async def modify_order(
        order_id: str, qty: Num = None, limit_price: Num = None, stop_price: Num = None,
        trail_pct: Num = None, trail_amount: Num = None,
    ) -> CallToolResult:
        """Change an open (pending/queued/held) order's qty or prices; the type cannot
        change. The risk checks run again; a refused change is an error and leaves the
        order as it was. Returns the order. Needs trade."""
        params = {
            "order_id": order_id, "qty": qty, "limit_price": limit_price,
            "stop_price": stop_price, "trail_pct": trail_pct, "trail_amount": trail_amount,
        }

        async def fn(ctx: ToolCtx) -> Any:
            changes = {
                "qty": parse_decimal(qty, "qty"),
                "limit_price": parse_decimal(limit_price, "limit_price"),
                "stop_price": parse_decimal(stop_price, "stop_price"),
                "trail_pct": parse_decimal(trail_pct, "trail_pct"),
                "trail_amount": parse_decimal(trail_amount, "trail_amount"),
            }
            order = await _order(ctx, order_id, "trade")
            order = await engine.modify_order(
                ctx.session, ctx.qs, order.id, **changes, redis=ctx.redis
            )
            return await _loaded(ctx, order)

        return await run_tool("modify_order", params, fn)

    @mcp.tool(structured_output=False)
    async def cancel_order(order_id: str) -> CallToolResult:
        """Cancel an open order (a bracket entry takes its exits with it). Returns the
        order. Needs trade."""

        async def fn(ctx: ToolCtx) -> Any:
            order = await _order(ctx, order_id, "trade")
            order = await engine.cancel_order(ctx.session, order.id, by=SOURCE)
            return await _loaded(ctx, order)

        return await run_tool("cancel_order", {"order_id": order_id}, fn)

    @mcp.tool(structured_output=False)
    async def cancel_all_orders(portfolio_id: str, symbol: str | None = None) -> CallToolResult:
        """Cancel every open order of the portfolio (only `symbol`'s, if given):
        {"canceled": n}. Needs trade."""

        async def fn(ctx: ToolCtx) -> Any:
            pid = await _portfolio(ctx, portfolio_id, "trade")
            n = await engine.cancel_all_orders(
                ctx.session, pid, _str(symbol, "symbol"), by=SOURCE
            )
            return {"canceled": n}

        params = {"portfolio_id": portfolio_id, "symbol": symbol}
        return await run_tool("cancel_all_orders", params, fn)

    @mcp.tool(structured_output=False)
    async def get_order(order_id: str) -> CallToolResult:
        """One order with its fills: {...order, fills: [{id, qty, price, fee_amount,
        fee_currency, quote_*, created_at, ...}]}. Needs read."""

        async def fn(ctx: ToolCtx) -> Any:
            order = await _order(ctx, order_id, "read")
            return {**order_dto(order), "fills": await _fills(ctx, order.id)}

        return await run_tool("get_order", {"order_id": order_id}, fn)

    @mcp.tool(structured_output=False)
    async def list_orders(
        portfolio_id: str, status: str | list[str] | None = None, limit: int | str | None = 100,
    ) -> CallToolResult:
        """The portfolio's orders, newest first. status: one of new, held, queued, pending,
        triggered, filled, canceled, rejected, expired, or a list of them. limit: 1..500
        (default 100). Needs read."""
        params = {"portfolio_id": portfolio_id, "status": status, "limit": limit}

        async def fn(ctx: ToolCtx) -> Any:
            statuses = _parse_statuses(status)
            n = parse_int(limit, "limit", 1, MAX_ORDERS) or 100
            pid = await _portfolio(ctx, portfolio_id, "read")
            return await engine.list_orders(ctx.session, pid, status=statuses, limit=n)

        return await run_tool("list_orders", params, fn)

    @mcp.tool(structured_output=False)
    async def close_position(
        portfolio_id: str, symbol: str, qty: Num = None
    ) -> CallToolResult:
        """Close a position (or `qty` of it) with a reduce_only market order on the
        opposite side; the symbol's open bracket exits are canceled. Returns the order.
        Needs trade."""
        params = {"portfolio_id": portfolio_id, "symbol": symbol, "qty": qty}

        async def fn(ctx: ToolCtx) -> Any:
            pid = await _portfolio(ctx, portfolio_id, "trade")
            sym = _str(symbol, "symbol", required=True)
            order = await engine.close_position(
                ctx.session, ctx.qs, pid, sym, parse_decimal(qty, "qty"),  # type: ignore[arg-type]
                source=SOURCE, agent_id=str(ctx.agent_id), redis=ctx.redis,
            )
            return await _loaded(ctx, order)

        return await run_tool("close_position", params, fn, commit_on_error=True)

    @mcp.tool(structured_output=False)
    async def close_all_positions(portfolio_id: str) -> CallToolResult:
        """Close every open position with reduce_only market orders; returns the orders
        (a refused close is returned with status rejected, not raised). Needs trade."""

        async def fn(ctx: ToolCtx) -> Any:
            pid = await _portfolio(ctx, portfolio_id, "trade")
            orders = await engine.close_all_positions(
                ctx.session, ctx.qs, pid, source=SOURCE, agent_id=str(ctx.agent_id),
                redis=ctx.redis,
            )
            return await _loaded(ctx, orders)

        params = {"portfolio_id": portfolio_id}
        return await run_tool("close_all_positions", params, fn, commit_on_error=True)
