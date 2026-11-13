"""Watchlist and price alert tools: list_watchlists, create_watchlist, add_to_watchlist,
remove_from_watchlist, delete_watchlist, create_alert, list_alerts, delete_alert.

They act on the calling agent's own watchlists and alerts (another agent's are FORBIDDEN)
and need no portfolio permission. Limits: 20 watchlists, 200 symbols per watchlist and
100 active alerts per agent (RISK_LIMIT_EXCEEDED). A triggered alert is delivered as a
`price_alert` notification by get_triggered_alerts.
"""

from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.types import CallToolResult

from smtrading.common.errors import ErrorCode, SMTError
from smtrading.mcp.dto import parse_decimal, parse_enum, parse_uuid
from smtrading.mcp.runtime import ToolCtx, run_tool
from smtrading.watch import service as watch
from smtrading.watch.models import AlertCondition

Num = str | int | float | None


def _invalid(field: str, message: str) -> SMTError:
    return SMTError(ErrorCode.INVALID_ORDER, message, {"field": field})


def _required_uuid(v: Any, field: str):
    if v is None:
        raise _invalid(field, f"{field} is required")
    return parse_uuid(v, field)


def _symbols(v: Any) -> list[str]:
    if not isinstance(v, list) or not v:
        raise _invalid("symbols", "symbols must be a non-empty list of symbols")
    if len(v) > watch.MAX_WATCHLIST_SYMBOLS:
        raise _invalid("symbols", f"at most {watch.MAX_WATCHLIST_SYMBOLS} symbols per call")
    return v


def _note(v: Any) -> str | None:
    if v is None:
        return None
    if not isinstance(v, str):
        raise _invalid("note", "note must be a string")
    return v.strip() or None


def register(mcp: MCPServer) -> None:
    @mcp.tool(structured_output=False)
    async def list_watchlists() -> CallToolResult:
        """The agent's watchlists: [{id, name, symbols, created_at}]."""
        return await run_tool(
            "list_watchlists", {}, lambda ctx: watch.list_watchlists(ctx.session, ctx.agent_id)
        )

    @mcp.tool(structured_output=False)
    async def create_watchlist(name: str) -> CallToolResult:
        """Create an empty watchlist (unique name per agent, at most 20 per agent):
        {id, name, symbols, created_at}."""
        return await run_tool(
            "create_watchlist", {"name": name},
            lambda ctx: watch.create_watchlist(ctx.session, ctx.agent_id, name),
        )

    @mcp.tool(structured_output=False)
    async def add_to_watchlist(watchlist_id: str, symbols: list[str]) -> CallToolResult:
        """Add symbols (catalog instruments) to a watchlist, at most 200 per watchlist;
        their quotes start streaming. Returns the watchlist."""
        params = {"watchlist_id": watchlist_id, "symbols": symbols}

        async def fn(ctx: ToolCtx) -> Any:
            wid = _required_uuid(watchlist_id, "watchlist_id")
            return await watch.add_to_watchlist(ctx.session, ctx.agent_id, wid, _symbols(symbols))

        return await run_tool("add_to_watchlist", params, fn)

    @mcp.tool(structured_output=False)
    async def remove_from_watchlist(
        watchlist_id: str, symbols: list[str]
    ) -> CallToolResult:
        """Remove symbols from a watchlist (absent ones are ignored). Returns the
        watchlist."""
        params = {"watchlist_id": watchlist_id, "symbols": symbols}

        async def fn(ctx: ToolCtx) -> Any:
            wid = _required_uuid(watchlist_id, "watchlist_id")
            return await watch.remove_from_watchlist(
                ctx.session, ctx.agent_id, wid, _symbols(symbols)
            )

        return await run_tool("remove_from_watchlist", params, fn)

    @mcp.tool(structured_output=False)
    async def delete_watchlist(watchlist_id: str) -> CallToolResult:
        """Delete a watchlist and its symbols: {id, deleted: true}."""

        async def fn(ctx: ToolCtx) -> Any:
            wid = _required_uuid(watchlist_id, "watchlist_id")
            return await watch.delete_watchlist(ctx.session, ctx.agent_id, wid)

        return await run_tool("delete_watchlist", {"watchlist_id": watchlist_id}, fn)

    @mcp.tool(structured_output=False)
    async def create_alert(
        symbol: str, condition: str, value: Num, note: str | None = None
    ) -> CallToolResult:
        """Create a price alert (at most 100 active per agent). condition: price_above |
        price_below (value = price) | change_pct_above | change_pct_below (value = percent
        change from the price at creation, e.g. -5). The price is last, else the bid/ask
        mid. It fires once and appears in get_triggered_alerts as kind "price_alert".
        Returns {id, symbol, condition, value, ref_price, status, note, created_at,
        triggered_at, triggered_price}."""
        params = {"symbol": symbol, "condition": condition, "value": value, "note": note}

        async def fn(ctx: ToolCtx) -> Any:
            if not isinstance(symbol, str) or not symbol.strip():
                raise _invalid("symbol", "symbol must be a non-empty string")
            cond = parse_enum(AlertCondition, condition, "condition")
            if cond is None:
                raise _invalid("condition", "condition is required")
            v = parse_decimal(value, "value")
            if v is None:
                raise _invalid("value", "value is required")
            return await watch.create_alert(
                ctx.session, ctx.qs, ctx.agent_id, symbol, cond, v, _note(note)
            )

        return await run_tool("create_alert", params, fn)

    @mcp.tool(structured_output=False)
    async def list_alerts(status: str | None = None) -> CallToolResult:
        """The agent's alerts, oldest first; status: active | triggered (default both)."""

        async def fn(ctx: ToolCtx) -> Any:
            if status is not None and not isinstance(status, str):
                raise _invalid("status", "status must be a string")
            return await watch.list_alerts(ctx.session, ctx.agent_id, status)

        return await run_tool("list_alerts", {"status": status}, fn)

    @mcp.tool(structured_output=False)
    async def delete_alert(alert_id: str) -> CallToolResult:
        """Delete one of the agent's alerts; returns it with status "deleted"."""

        async def fn(ctx: ToolCtx) -> Any:
            aid = _required_uuid(alert_id, "alert_id")
            return await watch.delete_alert(ctx.session, ctx.agent_id, aid)

        return await run_tool("delete_alert", {"alert_id": alert_id}, fn)
