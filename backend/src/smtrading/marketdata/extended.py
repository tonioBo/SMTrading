"""US pre-market / after-hours prices, for information only.

Agents and the portal see the latest extended-hours price of a US equity while its
regular session is closed (e.g. to judge the gap risk of an open position at the next
open). Nothing else uses it: order matching, stops, fills, signals, indicators,
include_forming, valuation and P/L keep using the regular session only.

`extended_hours` answers {symbol: dto | None} for the US equities among the given
instruments (other asset classes are left out): None while the regular session is open,
when no provider has a price or when the lookup fails (best-effort, never an error).
dto = {session: "pre"|"post", price, change_pct_vs_close, ts}; change_pct_vs_close is
the change against the close of the latest regular session, in percent.
"""

import asyncio
from collections.abc import Iterable
from datetime import UTC, datetime
from decimal import Decimal, localcontext
from typing import Any, Protocol

from smtrading.common.errors import SMTError
from smtrading.instruments.market_hours import market_status
from smtrading.instruments.models import AssetClass
from smtrading.marketdata.types import ExtendedQuote

MAX_CONCURRENT = 4
_PCT = Decimal("0.01")


class _Instrument(Protocol):
    symbol: str
    asset_class: Any
    market: str


def _now() -> datetime:
    return datetime.now(UTC)


def extended_dto(e: ExtendedQuote) -> dict[str, Any]:
    with localcontext() as ctx:
        ctx.prec = 60
        pct = (e.price - e.regular_close) / e.regular_close * 100
    return {"session": e.session, "price": e.price,
            "change_pct_vs_close": pct.quantize(_PCT), "ts": e.ts}


def _regular_open(market: str, now: datetime) -> bool:
    try:
        return market_status(market, now).is_open
    except SMTError:
        return False


async def extended_hours(
    router: Any, instruments: Iterable[_Instrument], now: datetime | None = None
) -> dict[str, dict[str, Any] | None]:
    now = now or _now()
    us = {i.symbol: i for i in instruments if i.asset_class == AssetClass.us_equity}
    out: dict[str, dict[str, Any] | None] = dict.fromkeys(us)
    lookup = getattr(router, "get_extended_quote", None)
    todo = [s for s, i in us.items() if not _regular_open(i.market, now)]
    if lookup is None or not todo:
        return out
    sem = asyncio.Semaphore(MAX_CONCURRENT)

    async def one(sym: str) -> dict[str, Any] | None:
        async with sem:
            try:
                e = await lookup(sym)
            except SMTError:
                return None
        return extended_dto(e) if e is not None and e.regular_close > 0 else None

    for sym, dto in zip(todo, await asyncio.gather(*(one(s) for s in todo)), strict=True):
        out[sym] = dto
    return out
