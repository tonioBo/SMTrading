"""The SMTrading MCP server and its ASGI endpoint (stateless streamable HTTP, JSON)."""

from mcp.server.mcpserver import MCPServer
from mcp.server.streamable_http_manager import StreamableHTTPASGIApp
from mcp.server.transport_security import TransportSecuritySettings

from smtrading.mcp import resources, tools_market, tools_orders, tools_portfolio

SERVER_NAME = "SMTrading"
_TOOL_MODULES = (tools_portfolio, tools_orders, tools_market, resources)


def build_mcp() -> MCPServer:
    mcp = MCPServer(SERVER_NAME)
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
