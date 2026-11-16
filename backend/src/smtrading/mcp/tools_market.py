"""Market data tools: search_instruments, get_instrument, get_quote, get_bars,
get_market_status, get_indicators, get_fx_rate, get_news, get_fundamentals,
get_earnings_calendar, get_dividends, screen.

Every tool needs a valid agent (run_tool) but no portfolio permission. Data comes from
`ctx.router` (the MarketDataRouter); a runtime without one answers PROVIDER_UNAVAILABLE.
"""

import asyncio
from datetime import UTC, date, datetime, timedelta
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.types import CallToolResult
from sqlalchemy import select

from smtrading.agents.models import AgentPortfolio
from smtrading.common.errors import ErrorCode, SMTError
from smtrading.instruments.discovery import Unsupported, auto_add_enabled, classify_hit
from smtrading.instruments.market_hours import (
    MARKETS,
    STATUS_MARKETS,
    all_market_status,
    market_status,
)
from smtrading.instruments.models import AssetClass, Instrument
from smtrading.instruments.service import canonical_symbol
from smtrading.marketdata.forming import forming_bar
from smtrading.marketdata.indicators_service import (
    IndicatorSpec,
    indicator_bars,
    indicators_from_bars,
)
from smtrading.marketdata.types import ScreenFilter, Timeframe
from smtrading.mcp.dto import instrument_dto, parse_decimal, parse_enum, parse_int, parse_ts
from smtrading.mcp.instruments import ensure_instrument
from smtrading.mcp.runtime import ToolCtx, run_tool
from smtrading.portfolios.models import live_portfolio_ids
from smtrading.positions.models import Position
from smtrading.risk.settings import get_stale_threshold

MAX_QUOTE_SYMBOLS = 50
MAX_BARS = 1000
MAX_LOOKBACK = 1000
MAX_PERIOD = 1000
_PERIOD_PARAMS = frozenset({"period", "fast", "slow", "signal"})
MAX_SEARCH = 50
MAX_LIST = 200
DEFAULT_BAR_COUNT = 200

_TF_DELTA = {
    Timeframe.m1: timedelta(minutes=1),
    Timeframe.m5: timedelta(minutes=5),
    Timeframe.m15: timedelta(minutes=15),
    Timeframe.h1: timedelta(hours=1),
    Timeframe.d1: timedelta(days=1),
    Timeframe.w1: timedelta(weeks=1),
}
# 200 daily bars are ~140 trading days; ask for 300 calendar days (weekends, holidays)
_DEFAULT_SPAN = {**{tf: d * DEFAULT_BAR_COUNT for tf, d in _TF_DELTA.items()},
                 Timeframe.d1: timedelta(days=300)}

Num = str | float | None


def _invalid(field: str, message: str) -> SMTError:
    return SMTError(ErrorCode.INVALID_ORDER, message, {"field": field})


def _str(v: Any, field: str) -> str:
    if not isinstance(v, str) or not v.strip():
        raise _invalid(field, f"{field} must be a non-empty string")
    return v.strip()


def _int(v: Any, field: str, default: int, hi: int) -> int:
    n = parse_int(v, field, 1, hi)
    return default if n is None else n


def _date(v: Any, field: str, *, required: bool = False) -> date | None:
    if v is None:
        if required:
            raise _invalid(field, f"{field} is required")
        return None
    try:
        return date.fromisoformat(str(v)[:10])
    except ValueError:
        raise _invalid(field, f"{field} must be an ISO date (YYYY-MM-DD)") from None


def _timeframe(v: Any) -> Timeframe:
    tf = parse_enum(Timeframe, v, "timeframe")
    if tf is None:
        raise _invalid("timeframe", "timeframe is required")
    return tf


def _symbols(v: Any, field: str = "symbols", *, maximum: int = MAX_QUOTE_SYMBOLS) -> list[str]:
    if not isinstance(v, list) or not v:
        raise _invalid(field, f"{field} must be a non-empty list of symbols")
    if len(v) > maximum:
        raise _invalid(field, f"at most {maximum} symbols per call")
    return list(dict.fromkeys(_str(s, field) for s in v))


def _router(ctx: ToolCtx):
    if ctx.router is None:
        raise SMTError(ErrorCode.PROVIDER_UNAVAILABLE, "market data is not available")
    return ctx.router


def _escape_like(s: str) -> str:
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _local_hit(i: Instrument) -> dict:
    return {
        "symbol": i.symbol, "name": i.name, "asset_class": i.asset_class, "market": i.market,
        "exchange": i.market, "currency": i.currency, "tradable": bool(i.active),
        "in_catalogo": True, "aggiungibile": False, "provider": "local",
    }


async def _search_instruments(
    ctx: ToolCtx, query: Any, asset_class: str | None, market: Any, include_provider: Any,
    limit: Any,
) -> list[dict]:
    q = _str(query, "query")
    ac = parse_enum(AssetClass, asset_class, "asset_class")
    mk = _str(market, "market").upper() if market is not None else None
    if mk is not None and mk not in MARKETS:
        raise _invalid("market", f"market must be one of {list(MARKETS)}")
    if not isinstance(include_provider, bool):
        raise _invalid("include_provider", "include_provider must be a boolean")
    n = _int(limit, "limit", 20, MAX_SEARCH)

    pattern = f"%{_escape_like(q)}%"
    stmt = (
        select(Instrument)
        .where(Instrument.symbol.ilike(pattern, escape="\\")
               | Instrument.name.ilike(pattern, escape="\\"))
        .order_by(Instrument.symbol)
        .limit(n)
    )
    if ac is not None:
        stmt = stmt.where(Instrument.asset_class == ac)
    if mk is not None:
        stmt = stmt.where(Instrument.market == mk)
    out: dict[str, dict] = {i.symbol: _local_hit(i) for i in await ctx.session.scalars(stmt)}
    if not include_provider or len(out) >= n:
        return list(out.values())
    try:
        hits = await _router(ctx).search(q, n)
    except SMTError:
        if not out:
            raise
        hits = []
    candidates: dict[str, tuple] = {}
    for h in hits:
        try:
            c = classify_hit(h)  # only what get_instrument(symbol) would add
        except Unsupported:
            continue
        if (ac is None or c.asset_class == ac) and (mk is None or c.market == mk):
            candidates.setdefault(c.symbol, (c, h))
    stored = set(await ctx.session.scalars(
        select(Instrument.symbol).where(Instrument.symbol.in_(list(candidates)))
    )) if candidates else set()
    addable = await auto_add_enabled(ctx.session)  # with the toggle off nothing is added
    for sym, (c, h) in candidates.items():
        if sym in out or sym in stored:
            continue
        out[sym] = {
            "symbol": sym, "name": c.name, "asset_class": c.asset_class, "market": c.market,
            "exchange": h.exchange, "currency": c.currency, "tradable": False,
            "in_catalogo": False, "aggiungibile": addable, "provider": h.provider,
        }
    return list(out.values())[:n]


async def _get_instrument(ctx: ToolCtx, symbol: Any) -> dict:
    sym = canonical_symbol(_str(symbol, "symbol"))
    await ensure_instrument(ctx, sym)
    instr = await ctx.session.get(Instrument, sym)
    if instr is None:
        raise SMTError(ErrorCode.SYMBOL_NOT_FOUND, f"unknown symbol {sym}", {"symbol": sym})
    out = instrument_dto(instr)
    try:
        out["market_status"] = market_status(instr.market, datetime.now(UTC))
    except SMTError:
        out["market_status"] = None
    return out


async def _get_quote(ctx: ToolCtx, symbols: Any) -> dict:
    syms = _symbols(symbols)
    router = _router(ctx)
    refused: dict[str, dict] = {}
    for sym in syms:  # unknown symbols are added on demand, one refusal per symbol
        try:
            await ensure_instrument(ctx, sym)
        except SMTError as e:
            refused[sym] = {"error": str(e.code), "message": e.message}
    todo = [s for s in syms if s not in refused]
    found: dict[str, Any] = {}
    if todo:
        try:
            found = dict(await router.get_quotes(todo))
        except SMTError:  # all-or-error batch: isolate the failing symbols
            async def one(sym: str):
                try:
                    return (await router.get_quotes([sym]))[sym]
                except SMTError as e:
                    return {"error": str(e.code)}

            found = dict(zip(todo, await asyncio.gather(*(one(s) for s in todo)), strict=True))
    return {s: refused[s] if s in refused else found[s] for s in syms}


async def _get_bars(
    ctx: ToolCtx, symbol: Any, timeframe: Any, from_ts: Any, to_ts: Any, limit: Any
) -> list:
    sym = _str(symbol, "symbol")
    tf = _timeframe(timeframe)
    n = _int(limit, "limit", 500, MAX_BARS)
    await ensure_instrument(ctx, sym)
    start, end = parse_ts(from_ts, "from_ts"), parse_ts(to_ts, "to_ts")
    end = end or datetime.now(UTC)
    start = start or end - _DEFAULT_SPAN[tf]
    if start >= end:
        raise _invalid("from_ts", "from_ts must be before to_ts")
    bars = await _router(ctx).get_bars(sym, tf, start, end)
    return list(bars)[-n:]


def _market_status(market: Any) -> Any:
    now = datetime.now(UTC)
    if market is None:
        return all_market_status(now)
    mk = _str(market, "market").upper()
    if mk not in STATUS_MARKETS:  # LSE: hours only, not a tradable market
        raise SMTError(ErrorCode.NOT_FOUND, f"unknown market {mk!r}", {"market": mk})
    return market_status(mk, now)


def _specs(indicators: Any) -> list[IndicatorSpec]:
    if not isinstance(indicators, list) or not indicators:
        raise _invalid("indicators", "indicators must be a non-empty list of {name, params}")
    specs = []
    for item in indicators:
        if not isinstance(item, dict) or set(item) - {"name", "params"}:
            raise _invalid("indicators", "each indicator must be {name, params?}")
        params = item.get("params") or {}
        if not isinstance(params, dict):
            raise _invalid("indicators", "indicator params must be an object")
        for key in _PERIOD_PARAMS & set(params):
            v = params[key]
            if isinstance(v, bool) or not isinstance(v, int) or not 1 <= v <= MAX_PERIOD:
                raise _invalid(
                    "indicators.params", f"{key} must be an integer between 1 and {MAX_PERIOD}"
                )
        specs.append(IndicatorSpec(_str(item.get("name"), "indicators.name"), params))  # type: ignore[arg-type]
    return specs


async def _get_indicators(
    ctx: ToolCtx, symbol: Any, timeframe: Any, indicators: Any, lookback: Any,
    include_forming: Any = False,
) -> dict:
    sym = _str(symbol, "symbol")
    tf = _timeframe(timeframe)
    specs = _specs(indicators)
    n = _int(lookback, "lookback", 100, MAX_LOOKBACK)
    if not isinstance(include_forming, bool):
        raise _invalid("include_forming", "include_forming must be a boolean")
    await ensure_instrument(ctx, sym)
    router = _router(ctx)
    bars = await indicator_bars(router, sym, tf, specs, n)
    out = indicators_from_bars(bars, sym, tf, specs, n)
    if not include_forming:
        return out
    instr = await ctx.session.get(Instrument, canonical_symbol(sym))
    if instr is None:
        raise SMTError(ErrorCode.SYMBOL_NOT_FOUND, f"unknown symbol {sym}", {"symbol": sym})
    stale_s = await get_stale_threshold(ctx.session, instr.asset_class)
    forming = await forming_bar(router, instr, tf, bars, stale_s)
    if forming.bar is None:
        return {**out, "provisional": False, "provisional_reason": forming.skipped}
    closed = {k: out[k] for k in ("ts", *(s["key"] for s in out["specs"]))}
    now = indicators_from_bars([*bars, forming.bar], sym, tf, specs, n)
    return {**now, "provisional": True, "as_of": forming.quote_ts, "closed": closed}


async def _held_symbols(ctx: ToolCtx) -> list[str]:
    rows = await ctx.session.scalars(
        select(Position.symbol)
        .join(AgentPortfolio, AgentPortfolio.portfolio_id == Position.portfolio_id)
        .where(
            AgentPortfolio.agent_id == ctx.agent_id, Position.qty != 0,
            Position.portfolio_id.in_(live_portfolio_ids()),
        )
        .distinct()
    )
    return sorted(rows)


async def _get_news(ctx: ToolCtx, symbols: Any, limit: Any) -> list:
    n = _int(limit, "limit", 20, MAX_LIST)
    syms = _symbols(symbols) if symbols is not None else await _held_symbols(ctx)
    return await _router(ctx).get_news(syms, n)


async def _calendar(
    ctx: ToolCtx, symbols: Any, from_date: Any, to_date: Any, kind: str
) -> list:
    start, end = _date(from_date, "from_date", required=True), _date(
        to_date, "to_date", required=True)
    if start > end:  # type: ignore[operator]
        raise _invalid("from_date", "from_date must not be after to_date")
    syms = _symbols(symbols) if symbols is not None else await _held_symbols(ctx)
    events = await _router(ctx).get_calendar(syms, start, end)
    return [e for e in events if e.kind == kind]


async def _get_dividends(ctx: ToolCtx, symbol: Any, from_date: Any, to_date: Any) -> list:
    sym = _str(symbol, "symbol")
    today = datetime.now(UTC).date()
    start = _date(from_date, "from_date") or today - timedelta(days=365)
    end = _date(to_date, "to_date") or today + timedelta(days=90)
    return await _calendar(ctx, [sym], start.isoformat(), end.isoformat(), "dividend")


async def _screen(ctx: ToolCtx, a: dict[str, Any]) -> list[dict]:
    f = ScreenFilter(
        asset_class=parse_enum(AssetClass, a["asset_class"], "asset_class"),
        sector=_str(a["sector"], "sector") if a["sector"] is not None else None,
        min_market_cap=parse_decimal(a["min_market_cap"], "min_market_cap"),
        max_market_cap=parse_decimal(a["max_market_cap"], "max_market_cap"),
        min_change_pct=parse_decimal(a["min_change_pct"], "min_change_pct"),
        max_change_pct=parse_decimal(a["max_change_pct"], "max_change_pct"),
        min_volume=parse_decimal(a["min_volume"], "min_volume"),
        max_pe=parse_decimal(a["max_pe"], "max_pe"),
        limit=_int(a["limit"], "limit", 25, MAX_LIST),
    )
    return await _router(ctx).screen(f)


def register(mcp: MCPServer) -> None:
    @mcp.tool(structured_output=False)
    async def search_instruments(
        query: str, asset_class: str | None = None, market: str | None = None,
        include_provider: bool = True, limit: int | str | None = 20,
    ) -> CallToolResult:
        """Search instruments by symbol or name (at most 50 results). First the catalog
        (in_catalogo: true; tradable false when deactivated), then, with include_provider,
        market data provider results not in the catalog that can be added on demand
        (in_catalogo: false, aggiungibile: true): call get_instrument(symbol) (or quote/
        trade it) to add one. Nothing is created by a search. asset_class: us_equity |
        eu_equity | crypto | forex; market: NASDAQ | NYSE | MTA | XETRA | EURONEXT_PARIS |
        CRYPTO | FX."""
        params = {"query": query, "asset_class": asset_class, "market": market,
                  "include_provider": include_provider, "limit": limit}
        return await run_tool(
            "search_instruments", params,
            lambda ctx: _search_instruments(
                ctx, query, asset_class, market, include_provider, limit))

    @mcp.tool(structured_output=False)
    async def get_instrument(symbol: str) -> CallToolResult:
        """Instrument master data (market, currency, lot/tick size, shortable) plus the
        current status of its market. A symbol not in the catalog is added on demand when
        a provider quotes it on a supported market (NASDAQ/NYSE in USD, .MI/.DE/.PA in
        EUR, crypto XXX-USD, forex XXXYYY), at most 25 per agent per day; otherwise
        INVALID_ORDER "Strumento non supportato: ..." or SYMBOL_NOT_FOUND."""
        return await run_tool("get_instrument", {"symbol": symbol},
                              lambda ctx: _get_instrument(ctx, symbol))

    @mcp.tool(structured_output=False)
    async def get_quote(symbols: list[str]) -> CallToolResult:
        """Latest quotes for up to 50 symbols: {symbol: {bid, ask, last, ts, provider}} or
        {symbol: {"error": code}} for a symbol that failed. Prices are returned as strings
        for exact precision."""
        return await run_tool("get_quote", {"symbols": symbols},
                              lambda ctx: _get_quote(ctx, symbols))

    @mcp.tool(structured_output=False)
    async def get_bars(
        symbol: str, timeframe: str, from_ts: str | None = None, to_ts: str | None = None,
        limit: int | str | None = 500,
    ) -> CallToolResult:
        """OHLCV bars, oldest first (the latest `limit`, max 1000). timeframe: 1m | 5m |
        15m | 1h | 1d | 1wk. Defaults: to_ts=now, from_ts=200 bars back (1d: 300 days).
        Timestamps are ISO 8601; numbers are returned as strings for exact precision."""
        params = {"symbol": symbol, "timeframe": timeframe, "from_ts": from_ts,
                  "to_ts": to_ts, "limit": limit}
        return await run_tool(
            "get_bars", params,
            lambda ctx: _get_bars(ctx, symbol, timeframe, from_ts, to_ts, limit))

    @mcp.tool(structured_output=False)
    async def get_market_status(market: str | None = None) -> CallToolResult:
        """Open/closed status and next open/close of a market (NASDAQ, NYSE, MTA, XETRA,
        EURONEXT_PARIS, CRYPTO, FX), or of all markets when omitted. LSE (London) is listed
        for its hours only: its instruments are not tradable here."""
        return await run_tool("get_market_status", {"market": market},
                              lambda ctx: _async(_market_status, market))

    @mcp.tool(structured_output=False)
    async def get_indicators(
        symbol: str, timeframe: str, indicators: list[dict], lookback: int | str | None = 100,
        include_forming: bool = False,
    ) -> CallToolResult:
        """Technical indicators over the last `lookback` bars. indicators is a list of
        {name, params}: sma/ema/rsi/atr/bollinger take {period} (bollinger also k), macd
        {fast, slow, signal}, vwap {}; periods are integers 1-1000. Result: {symbol,
        timeframe, ts, specs: [{key, name, params}], <key>: values}; the key is the name,
        or name_<param values> (e.g. sma_50) when a name is asked more than once. Values
        are returned as strings for exact precision.
        include_forming (1d and 1wk only; default false = closed bars only): "as if the
        bar closed now". While the symbol's market is open (crypto always) and its quote
        is fresh, today's (1wk: this week's) bar is added from the current quote (open =
        previous close, close = last price or mid) before computing: the result then has
        provisional: true, as_of (the quote's time: EU quotes may be some minutes late)
        and closed: {ts, <key>: values} with the values on the closed bars only. Otherwise
        provisional: false and provisional_reason (market closed, stale quote, no quote,
        bar already closed, timeframe not supported) with the closed-bar values."""
        params = {"symbol": symbol, "timeframe": timeframe, "indicators": indicators,
                  "lookback": lookback, "include_forming": include_forming}
        return await run_tool(
            "get_indicators", params,
            lambda ctx: _get_indicators(
                ctx, symbol, timeframe, indicators, lookback, include_forming))

    @mcp.tool(structured_output=False)
    async def get_fx_rate(base: str, quote: str) -> CallToolResult:
        """Units of `quote` per one unit of `base`, as a string for exact precision."""
        return await run_tool(
            "get_fx_rate", {"base": base, "quote": quote},
            lambda ctx: _fx(ctx, base, quote))

    @mcp.tool(structured_output=False)
    async def get_news(
        symbols: list[str] | None = None, limit: int | str | None = 20
    ) -> CallToolResult:
        """Latest news, newest first, for the given symbols (default: the symbols held in
        your portfolios)."""
        return await run_tool("get_news", {"symbols": symbols, "limit": limit},
                              lambda ctx: _get_news(ctx, symbols, limit))

    @mcp.tool(structured_output=False)
    async def get_fundamentals(symbol: str) -> CallToolResult:
        """Fundamentals: sector, market cap, P/E, EPS, dividend yield (percent), 52-week
        range. Numbers are returned as strings for exact precision."""
        return await run_tool("get_fundamentals", {"symbol": symbol},
                              lambda ctx: _fundamentals(ctx, symbol))

    @mcp.tool(structured_output=False)
    async def get_earnings_calendar(
        from_date: str, to_date: str, symbols: list[str] | None = None
    ) -> CallToolResult:
        """Earnings events between two ISO dates (default symbols: those held in your
        portfolios)."""
        params = {"from_date": from_date, "to_date": to_date, "symbols": symbols}
        return await run_tool(
            "get_earnings_calendar", params,
            lambda ctx: _calendar(ctx, symbols, from_date, to_date, "earnings"))

    @mcp.tool(structured_output=False)
    async def get_dividends(
        symbol: str, from_date: str | None = None, to_date: str | None = None
    ) -> CallToolResult:
        """Dividend events of a symbol (default: one year back to 90 days ahead)."""
        params = {"symbol": symbol, "from_date": from_date, "to_date": to_date}
        return await run_tool("get_dividends", params,
                              lambda ctx: _get_dividends(ctx, symbol, from_date, to_date))

    @mcp.tool(structured_output=False)
    async def screen(
        asset_class: str | None = None, sector: str | None = None,
        min_market_cap: Num = None, max_market_cap: Num = None,
        min_change_pct: Num = None, max_change_pct: Num = None, min_volume: Num = None,
        max_pe: Num = None, limit: int | str | None = 25,
    ) -> CallToolResult:
        """Screen instruments by filters. Pass numbers as strings for exact precision."""
        params = {
            "asset_class": asset_class, "sector": sector, "min_market_cap": min_market_cap,
            "max_market_cap": max_market_cap, "min_change_pct": min_change_pct,
            "max_change_pct": max_change_pct, "min_volume": min_volume, "max_pe": max_pe,
            "limit": limit,
        }
        return await run_tool("screen", params, lambda ctx: _screen(ctx, params))


async def _async(fn, *args):
    return fn(*args)


async def _fx(ctx: ToolCtx, base: Any, quote: Any) -> dict:
    b, q = _str(base, "base").upper(), _str(quote, "quote").upper()
    return {"base": b, "quote": q, "rate": await _router(ctx).get_fx_rate(b, q)}


async def _fundamentals(ctx: ToolCtx, symbol: Any):
    return await _router(ctx).get_fundamentals(_str(symbol, "symbol"))
