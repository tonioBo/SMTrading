"""The Markets page's catalog queries (plan 22): instant search with ranking, the list
filters (sector, index, quick filters), universes for the sector map and the movers, and
the aggregation of day changes into sector tiles and top gainers / losers.

Everything here reads the database only; day changes come from the caller
(`MarketDataRouter.get_day_changes`).
"""

import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import ROUND_HALF_EVEN, Decimal
from typing import Any

from sqlalchemy import ColumnElement, case, func, literal, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from smtrading.instruments.models import AssetClass, Instrument, InstrumentSource
from smtrading.instruments.taxonomy import INDICES, index_codes_matching
from smtrading.portfolios.models import live_portfolio_ids
from smtrading.positions.models import Position
from smtrading.watch.models import WatchlistItem

MAX_SEARCH = 50
RECENT_DAYS = 7
MOVERS = 10
UNSECTORED = "Senza settore"
# universes of the sector map / movers: the index codes, all crypto, and «mine»
# (the open positions plus the agents' watchlists)
UNIVERSES: dict[str, str] = {**INDICES, "CRYPTO": "Crypto", "MINE": "Watchlist e posizioni"}
QUICK_FILTERS = ("portfolio", "watchlist", "strategy", "signal", "recent")
SORTS = {
    "symbol": Instrument.symbol, "name": Instrument.name, "sector": Instrument.sector,
    "market": Instrument.market, "asset_class": Instrument.asset_class,
    "created_at": Instrument.created_at,
}


def escape_like(s: str) -> str:
    return re.sub(r"([\\%_])", r"\\\1", s)


def _compact(col: Any) -> Any:
    """The symbol without separators: "brkb" finds BRK-B, "eurusd" EURUSD."""
    return func.replace(func.replace(func.replace(col, "-", ""), ".", ""), "/", "")


# ------------------------------------------------------------------ instant search


def _score(q: str) -> tuple[ColumnElement, ColumnElement]:
    """(score, where) for a search: exact symbol 100, symbol without separators 95,
    symbol prefix 90, name prefix 80, a name word prefix 70, symbol contains 50, name
    contains 40, index 30, sector 25, industry 20."""
    e = escape_like(q)
    compact = re.sub(r"[-./\s]", "", q)
    codes = index_codes_matching(q)
    sym, name = Instrument.symbol, Instrument.name
    whens: list[tuple[ColumnElement, int]] = [
        (func.upper(sym) == q.upper(), 100),
    ]
    if compact:
        whens.append((func.upper(_compact(sym)) == compact.upper(), 95))
    whens += [
        (sym.ilike(f"{e}%", escape="\\"), 90),
        (name.ilike(f"{e}%", escape="\\"), 80),
        (name.ilike(f"% {e}%", escape="\\"), 70),
        (sym.ilike(f"%{e}%", escape="\\"), 50),
        (name.ilike(f"%{e}%", escape="\\"), 40),
    ]
    if codes:
        whens.append((Instrument.indices.overlap(codes), 30))
    whens += [
        (Instrument.sector.ilike(f"%{e}%", escape="\\"), 25),
        (Instrument.industry.ilike(f"%{e}%", escape="\\"), 20),
    ]
    score = case(*whens, else_=literal(0))
    return score, score > 0


def match_field(i: Instrument, q: str) -> str:
    """Which field a hit matched (the UI highlights it)."""
    ql = q.lower()
    compact = re.sub(r"[-./\s]", "", ql)
    sym = i.symbol.lower()
    if ql in sym or (compact and compact == re.sub(r"[-./]", "", sym)):
        return "symbol"
    if ql in i.name.lower():
        return "name"
    if set(index_codes_matching(q)) & set(i.indices or []):
        return "index"
    if i.sector and ql in i.sector.lower():
        return "sector"
    if i.industry and ql in i.industry.lower():
        return "industry"
    return "symbol"


async def search(session: AsyncSession, q: str, limit: int) -> list[tuple[Instrument, int]]:
    """Catalog hits for `q`, best first (active before inactive, shorter symbols first)."""
    q = q.strip()
    if not q:
        return []
    score, where = _score(q)
    rows = await session.execute(
        select(Instrument, score.label("score"))
        .where(where)
        .order_by(score.desc(), Instrument.active.desc(), func.length(Instrument.symbol),
                  Instrument.symbol)
        .limit(min(max(limit, 1), MAX_SEARCH))
    )
    return [(i, int(s)) for i, s in rows.all()]


# ------------------------------------------------------------------ quick filters


async def held_symbols(session: AsyncSession) -> set[str]:
    """Symbols with an open position in a non-archived portfolio."""
    return set(await session.scalars(
        select(Position.symbol).where(Position.qty != 0,
                                      Position.portfolio_id.in_(live_portfolio_ids()))
        .distinct()))


async def watchlist_symbols(session: AsyncSession) -> set[str]:
    return set(await session.scalars(select(WatchlistItem.symbol).distinct()))


async def strategy_symbols(session: AsyncSession) -> set[str]:
    from smtrading.signals.service import approved_strategies

    return {s for a in await approved_strategies(session) for s in a.symbols}


def signal_symbols(signals: dict | None) -> set[str]:
    """Symbols whose entry rule fires now in the «Segnali» answer (flat and without a
    resting entry: a new signal, as the Signals page counts it)."""
    out: set[str] = set()
    for p in (signals or {}).get("portfolios") or []:
        for s in p.get("strategies") or []:
            for row in s.get("symbols") or []:
                if row.get("triggered_now") and not row.get("position") \
                        and not row.get("open_entry"):
                    out.add(row["symbol"])
    return out


def recent_condition(now: datetime | None = None) -> ColumnElement:
    since = (now or datetime.now(UTC)) - timedelta(days=RECENT_DAYS)
    return or_(Instrument.created_at >= since, Instrument.source == InstrumentSource.auto)


# ------------------------------------------------------------------ universes


async def universe_instruments(session: AsyncSession, universe: str) -> list[Instrument]:
    """The active instruments of a universe (UNIVERSES), by symbol."""
    stmt = select(Instrument).where(Instrument.active.is_(True)).order_by(Instrument.symbol)
    if universe in INDICES:
        stmt = stmt.where(Instrument.indices.contains([universe]))
    elif universe == "CRYPTO":
        stmt = stmt.where(Instrument.asset_class == AssetClass.crypto)
    elif universe == "MINE":
        syms = await held_symbols(session) | await watchlist_symbols(session)
        if not syms:
            return []
        stmt = stmt.where(Instrument.symbol.in_(syms))
    else:
        raise ValueError(universe)
    return list(await session.scalars(stmt))


# ------------------------------------------------------------------ aggregation


@dataclass(frozen=True)
class Member:
    symbol: str
    name: str
    sector: str
    currency: str
    last: Decimal
    change_pct: Decimal


def _pct(d: Decimal) -> Decimal:
    return d.quantize(Decimal("0.01"), rounding=ROUND_HALF_EVEN)


def members(instruments: Iterable[Instrument], changes: dict[str, Any]) -> list[Member]:
    """The instruments with a day change (`changes`: symbol -> DayChange)."""
    out = []
    for i in instruments:
        dc = changes.get(i.symbol)
        if dc is None:
            continue
        out.append(Member(i.symbol, i.name, i.sector or UNSECTORED, i.currency, dc.last,
                          _pct(dc.change_pct)))
    return out


def sector_tiles(rows: list[Member]) -> list[dict]:
    """One tile per sector: the simple average day change of its members, how many rose
    and fell, the members by change (best first). Tiles by member count, then name."""
    groups: dict[str, list[Member]] = {}
    for m in rows:
        groups.setdefault(m.sector, []).append(m)
    tiles = []
    for sector, ms in groups.items():
        avg = sum((m.change_pct for m in ms), Decimal(0)) / len(ms)
        tiles.append({
            "sector": sector, "count": len(ms), "avg_change_pct": _pct(avg),
            "up": sum(m.change_pct > 0 for m in ms), "down": sum(m.change_pct < 0 for m in ms),
            "members": [_member_dto(m) for m in sorted(ms, key=lambda m: -m.change_pct)],
        })
    tiles.sort(key=lambda t: (-t["count"], t["sector"]))
    return tiles


def movers(rows: list[Member], n: int = MOVERS) -> tuple[list[dict], list[dict]]:
    """(top gainers, top losers): the n best rising and n worst falling, ties by symbol."""
    up = sorted((m for m in rows if m.change_pct > 0), key=lambda m: (-m.change_pct, m.symbol))
    down = sorted((m for m in rows if m.change_pct < 0), key=lambda m: (m.change_pct, m.symbol))
    return [_member_dto(m) for m in up[:n]], [_member_dto(m) for m in down[:n]]


def _member_dto(m: Member) -> dict:
    return {"symbol": m.symbol, "name": m.name, "sector": m.sector, "currency": m.currency,
            "last": m.last, "change_pct": m.change_pct}
