"""The SMTrading MCP server and its ASGI endpoint (stateless streamable HTTP, JSON)."""

from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError, UnexpectedToolError
from mcp.server.streamable_http_manager import StreamableHTTPASGIApp
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import ValidationError

from smtrading.mcp import (
    resources,
    tools_activity,
    tools_alerts,
    tools_backtest,
    tools_docs,
    tools_forecast,
    tools_goals,
    tools_halt,
    tools_market,
    tools_orders,
    tools_portfolio,
    tools_research,
    tools_telegram,
    tools_watch,
)
from smtrading.mcp.runtime import reject_arguments

SERVER_NAME = "SMTrading"
_TOOL_MODULES = (
    tools_portfolio, tools_orders, tools_market, tools_alerts, tools_watch, tools_backtest,
    tools_research, tools_activity, tools_telegram, tools_halt, tools_goals, tools_docs, tools_forecast,
    resources,
)
# tools whose audit row must not keep the raw arguments (also when the schema rejects them)
_AUDIT_PARAMS = {
    "send_telegram": tools_telegram.audit_params,
    "set_trading_halt": tools_halt.audit_params,
}


class SMTradingServer(MCPServer):
    """MCPServer whose argument-schema failures (missing argument, wrong type) answer like
    every other tool error: INVALID_ORDER JSON with `details.field`, audited."""

    async def call_tool(self, name: str, arguments: dict[str, Any], context=None):
        try:
            return await super().call_tool(name, arguments, context)
        except ToolError as e:
            if isinstance(e, UnexpectedToolError) or not isinstance(e.__cause__, ValidationError):
                raise
            reduce = _AUDIT_PARAMS.get(name)
            params = reduce(arguments) if reduce else arguments
            return await reject_arguments(name, params, e.__cause__)


def build_mcp() -> MCPServer:
    mcp = SMTradingServer(SERVER_NAME)
    for module in _TOOL_MODULES:
        module.register(mcp)
    return mcp


def build_mcp_endpoint(mcp: MCPServer) -> StreamableHTTPASGIApp:
    """The raw ASGI endpoint, mounted by the API at both /mcp and /mcp/ (no redirect).

    Creates `mcp.session_manager`, whose `run()` the app lifespan must enter. Host/Origin
    (DNS-rebinding) checks are off: the server is reached as http://api:8000 inside the
    compose network and every request is bearer-authenticated by McpAuthMiddleware.
    """
    mcp.streamable_http_app(
        streamable_http_path="/",
        stateless_http=True,
        json_response=True,
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )
    return StreamableHTTPASGIApp(mcp.session_manager)
