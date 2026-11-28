"""«Fiume dei flussi»: the portal's read-only view of SMTrading's real flows.

`map` is the single source of truth for the nodes, the links and where every MCP tool
goes, the lens families and the price-source names; `events` translates existing records
into path events (live and replay); `summary` aggregates them (and the lens tools);
`story` tells one order or MCP call; `agents` reads the Paperclip runs and costs (cached);
`health` gives every node a state; `sources` the price sources behind «Prezzi».
Admin only, never MCP. Keep it real: any change that adds, removes or reroutes a piece of
SMTrading (MCP tool, broker kind, price source, external service, alert channel) updates
`map` in the same work — the guard tests in `tests/unit/test_flow_map.py` fail otherwise.
"""
