"""«Fiume dei flussi»: the portal's read-only view of SMTrading's real flows.

`map` is the single source of truth for the nodes, the links and where every MCP tool
goes; `events` translates existing records into path events; `summary` aggregates them.
Admin only, never MCP. Keep it real: any change that adds, removes or reroutes a piece of
SMTrading (MCP tool, broker kind, price source, external service, alert channel) updates
`map` in the same work — the guard tests in `tests/unit/test_flow_map.py` fail otherwise.
"""
