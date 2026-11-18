"""Admin API «Risultati agenti» (performance.agent_results): per agent, per strategy and
for the team, in a period. Read-only (a viewer can read it); never exposed to the agents.

`GET /api/agent-results?period=week|1m|3m|all` (default `week`) → {as_of, period, from,
to, last_read_at, team: {pnl_eur, costs_eur, orders_executed, proposals, approvals,
vetoes, approval_rate_pct, lessons, lessons_repeated, rules_active, cost_usd, billed_usd,
unmatched_cost_usd, pnl_per_usd, cost_per_order_usd}, agents: [{agent_id, name, active,
decisions: {kind: n}, decisions_total, proposals, approved, vetoed, pending,
approval_rate_pct, lessons_written, lessons_about, lessons_repeated, mcp_calls,
mcp_errors, mcp_error_pct, orders_executed, paperclip_names, runs, cost_usd, billed_usd,
cost_per_order_usd}], strategies: [{ref, name, status, portfolios, trades_opened,
trades_closed, wins, win_rate_pct, realized_pnl_eur, costs_eur, net_pnl_eur,
avg_holding_s, open_positions}]}. Numbers as strings (counts as integers), null when
unknown. `from` is null for `all`. Cached CACHE_TTL_S in redis per period.
"""

import json
import logging
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, Query
from redis.exceptions import RedisError

from smtrading.admin.auth import DbSession, RedisClient, require_access
from smtrading.admin.common import Quotes, _invalid
from smtrading.mcp.dto import to_jsonable
from smtrading.performance.agent_results import PERIODS, agent_results

log = logging.getLogger("smtrading.admin.agent_results")
router = APIRouter(prefix="/api", tags=["agent-results"], dependencies=[Depends(require_access)])

CACHE_KEY = "agent-results:v1:{period}"
CACHE_TTL_S = 60


@router.get("/agent-results")
async def get_agent_results(session: DbSession, redis: RedisClient, qs: Quotes,
                            period: str = Query("week")) -> dict[str, Any]:
    if period not in PERIODS:
        raise _invalid("period", f"period deve essere uno tra {list(PERIODS)}")
    key = CACHE_KEY.format(period=period)
    try:
        cached = await redis.get(key)
    except RedisError:
        log.warning("agent results cache read failed", exc_info=True)
        cached = None
    if cached is not None:
        return json.loads(cached)
    r = await agent_results(session, qs, period)
    body = to_jsonable({
        "as_of": datetime.now(UTC), "period": r.period, "from": r.from_, "to": r.to,
        "last_read_at": r.last_read_at, "team": r.team, "agents": r.agents,
        "strategies": r.strategies,
    })
    try:
        await redis.set(key, json.dumps(body), ex=CACHE_TTL_S)
    except RedisError:
        log.warning("agent results cache write failed", exc_info=True)
    return body
