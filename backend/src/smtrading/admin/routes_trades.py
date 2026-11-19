"""Admin API «Diario operazioni» (performance.journal). Read-only (a viewer can read it);
never exposed to the agents.

`GET /api/trades?period=week|1m|3m|all&portfolio_id=&strategy=STR-n|none&symbol=&outcome=
win|loss|open&page=1&page_size=50` → {as_of, period, total, page, page_size, summary:
{trades, open, closed, wins, win_rate_pct, pnl_eur}, filters: {portfolios: [{id, name}],
strategies: [{ref, name}], symbols: [...]}, items: [TradeRow]} (numbers as strings, null
when unknown). The unfiltered rows are cached CACHE_TTL_S in redis; filters and pages
apply to them. `filters` lists the values of every trade (the dropdowns).

`GET /api/trades/{trade_key}` → the trade's detail and timeline (`journal.trade_detail`),
404 when the key names no trade.
"""

import json
import logging
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Depends, Query
from redis.exceptions import RedisError

from smtrading.admin.auth import DbSession, RedisClient, require_access
from smtrading.admin.common import Quotes, _invalid
from smtrading.common.config import get_settings
from smtrading.common.errors import ErrorCode, SMTError
from smtrading.mcp.dto import to_jsonable
from smtrading.performance.agent_results import PERIODS, period_bounds
from smtrading.performance.journal import NO_STRATEGY, OUTCOMES, trade_detail, trade_rows

log = logging.getLogger("smtrading.admin.trades")
router = APIRouter(prefix="/api", tags=["trades"], dependencies=[Depends(require_access)])

CACHE_KEY = "trades:journal:v1"
CACHE_TTL_S = 60
MAX_PAGE_SIZE = 200


async def _rows(session, redis, qs) -> dict[str, Any]:
    try:
        cached = await redis.get(CACHE_KEY)
    except RedisError:
        log.warning("trades cache read failed", exc_info=True)
        cached = None
    if cached is not None:
        return json.loads(cached)
    body = to_jsonable({"as_of": datetime.now(UTC),
                        "rows": await trade_rows(session, qs, redis=redis)})
    try:
        await redis.set(CACHE_KEY, json.dumps(body), ex=CACHE_TTL_S)
    except RedisError:
        log.warning("trades cache write failed", exc_info=True)
    return body


def _summary(rows: list[dict]) -> dict[str, Any]:
    closed = [r for r in rows if r["outcome"] != "open"]
    wins = sum(1 for r in closed if r["outcome"] == "win")
    pnl: Decimal | None = Decimal(0)
    for r in rows:
        if r["pnl_eur"] is None:
            pnl = None
            break
        pnl += Decimal(r["pnl_eur"])
    return {
        "trades": len(rows), "open": len(rows) - len(closed), "closed": len(closed),
        "wins": wins,
        "win_rate_pct": str((Decimal(wins) * 100 / len(closed)).quantize(Decimal("0.1")))
        if closed else None,
        "pnl_eur": None if pnl is None else str(pnl.quantize(Decimal("0.01"))),
    }


def _filters(rows: list[dict]) -> dict[str, Any]:
    portfolios = {r["portfolio_id"]: r["portfolio_name"] for r in rows}
    strategies = {r["strategy_ref"]: r["strategy_name"] for r in rows if r["strategy_ref"]}
    return {
        "portfolios": [{"id": k, "name": v} for k, v in
                       sorted(portfolios.items(), key=lambda kv: kv[1].casefold())],
        "strategies": [{"ref": k, "name": v} for k, v in
                       sorted(strategies.items(), key=lambda kv: int(kv[0][4:])
                              if kv[0][4:].isdigit() else 0)],
        "no_strategy": any(r["strategy_ref"] is None for r in rows),
        "symbols": sorted({r["symbol"] for r in rows}),
    }


@router.get("/trades")
async def list_trades(
    session: DbSession, redis: RedisClient, qs: Quotes,
    period: str = Query("all"), portfolio_id: str | None = Query(None),
    strategy: str | None = Query(None), symbol: str | None = Query(None),
    outcome: str | None = Query(None), page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=MAX_PAGE_SIZE),
) -> dict[str, Any]:
    if period not in PERIODS:
        raise _invalid("period", f"period deve essere uno tra {list(PERIODS)}")
    if outcome is not None and outcome not in OUTCOMES:
        raise _invalid("outcome", f"outcome deve essere uno tra {list(OUTCOMES)}")
    if portfolio_id is not None:
        try:
            portfolio_id = str(uuid.UUID(portfolio_id))
        except ValueError:
            raise _invalid("portfolio_id", "portfolio_id non valido") from None
    body = await _rows(session, redis, qs)
    rows: list[dict] = body["rows"]
    _, since = period_bounds(period, datetime.now(UTC), get_settings().tz)
    picked = []
    for r in rows:
        if since is not None and r["closed_at"] is not None \
                and datetime.fromisoformat(r["closed_at"]) < since:
            continue
        if portfolio_id is not None and r["portfolio_id"] != portfolio_id:
            continue
        if strategy is not None and (
                (r["strategy_ref"] is not None) if strategy == NO_STRATEGY
                else r["strategy_ref"] != strategy):
            continue
        if symbol and r["symbol"].casefold() != symbol.strip().casefold():
            continue
        if outcome is not None and r["outcome"] != outcome:
            continue
        picked.append(r)
    start = (page - 1) * page_size
    return {
        "as_of": body["as_of"], "period": period, "total": len(picked), "page": page,
        "page_size": page_size, "summary": _summary(picked), "filters": _filters(rows),
        "items": picked[start:start + page_size],
    }


@router.get("/trades/{trade_key}")
async def get_trade(trade_key: str, session: DbSession, redis: RedisClient,
                    qs: Quotes) -> dict[str, Any]:
    detail = await trade_detail(session, qs, trade_key, redis=redis)
    if detail is None:
        raise SMTError(ErrorCode.NOT_FOUND, "operazione non trovata",
                       {"trade_key": trade_key})
    return to_jsonable({"as_of": datetime.now(UTC), **detail})
