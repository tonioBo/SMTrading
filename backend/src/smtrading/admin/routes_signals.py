"""Admin API «Segnali»: how close each approved strategy is to an entry (signals.service).
Read-only (a viewer can read it).

`GET /api/signals` → {as_of, portfolios: [{portfolio_id, portfolio_name, strategies:
[{ref, name, type, params, timeframe, status, symbols: [{symbol, last, indicator,
value_closed, value_now, threshold, direction, distance, triggered_closed, triggered_now,
as_of, position: {qty, avg_price}|null, open_entry, exit: {rule, value, threshold,
distance, triggered}|null (only in position), filters: [{rule, value, threshold,
passed}] (the entry filters; empty without)}]}]}]}: numbers as strings, value_now null
without a forming bar (market closed,
stale quote...). Portfolios by name (a strategy without portfolio last, portfolio_id
null). The answer is cached CACHE_TTL_S in redis: open tabs refreshing every minute
cost one computation per minute; `as_of` is when it was computed.
"""

import json
import logging
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, Request
from redis.exceptions import RedisError

from smtrading.admin.auth import DbSession, RedisClient, require_access
from smtrading.common.errors import ErrorCode, SMTError
from smtrading.mcp.dto import to_jsonable
from smtrading.signals.service import StrategySignal, strategy_signals

log = logging.getLogger("smtrading.admin.signals")
router = APIRouter(prefix="/api", tags=["signals"], dependencies=[Depends(require_access)])

CACHE_KEY = "signals:v3"  # v2: `exit`; v3: `filters`
CACHE_TTL_S = 60


def _payload(items: list[StrategySignal], as_of: datetime) -> dict[str, Any]:
    groups: dict[Any, dict[str, Any]] = {}
    for s in items:
        g = groups.setdefault(s.portfolio_id, {
            "portfolio_id": s.portfolio_id, "portfolio_name": s.portfolio_name,
            "strategies": [],
        })
        g["strategies"].append({
            "ref": s.ref, "name": s.name, "type": s.type, "params": s.params,
            "timeframe": s.timeframe, "status": s.status, "symbols": s.symbols,
        })
    return to_jsonable({"as_of": as_of, "portfolios": list(groups.values())})


@router.get("/signals")
async def signals(request: Request, session: DbSession, redis: RedisClient) -> dict:
    return await signals_body(request, session, redis)


async def cached_signals(redis) -> dict | None:
    """The cached answer of GET /api/signals, None when there is none (never computes)."""
    try:
        cached = await redis.get(CACHE_KEY)
    except RedisError:
        log.warning("signals cache read failed", exc_info=True)
        return None
    return json.loads(cached) if cached is not None else None


async def signals_body(request: Request, session, redis) -> dict:
    """The GET /api/signals answer: from the cache, else computed and cached."""
    try:
        cached = await redis.get(CACHE_KEY)
    except RedisError:
        log.warning("signals cache read failed", exc_info=True)
        cached = None
    if cached is not None:
        return json.loads(cached)
    router_ = request.app.state.mcp_runtime.router
    if router_ is None:
        raise SMTError(ErrorCode.PROVIDER_UNAVAILABLE, "dati di mercato non disponibili")
    now = datetime.now(UTC)
    body = _payload(await strategy_signals(session, router_, now), now)
    try:
        await redis.set(CACHE_KEY, json.dumps(body), ex=CACHE_TTL_S)
    except RedisError:
        log.warning("signals cache write failed", exc_info=True)
    return body
