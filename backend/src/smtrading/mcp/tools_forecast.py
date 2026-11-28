"""get_forecast: the risk range of a symbol from Neurograph's «serie» compartment.

The bars come from SMTrading's own router (the same as get_bars); Neurograph only sees the
closes and answers with the GARCH(1,1)-t range (the model that won Neurograph's T0 test on
209 symbols: the time-series foundation models lost to it). It is a risk measure, not a
direction: the range is symmetric and no order ever follows from it.

`SMT_NEUROGRAPH_URL` (e.g. http://192.168.4.4:18088/api/serie, over the WireGuard VPN) must
be set; unset or unreachable answers PROVIDER_UNAVAILABLE. The call goes from SMTrading to
Neurograph, so SMTrading's own MCP stays closed to the network.
"""

from typing import Any

import httpx
from mcp.server.mcpserver import MCPServer
from mcp.types import CallToolResult

from smtrading.common.config import get_settings
from smtrading.common.errors import ErrorCode, SMTError
from smtrading.marketdata.indicators_service import fetch_bars
from smtrading.mcp.instruments import ensure_instrument
from smtrading.mcp.runtime import ToolCtx, run_tool
from smtrading.mcp.tools_market import _int, _router, _str, _timeframe

MAX_HORIZON = 20
N_BARS = 600  # GARCH is fitted on the last closes (Neurograph uses at most 2000)
MIN_BARS = 101
TIMEOUT_S = 20.0


def _pct(x: float) -> str:
    return f"{100 * x:.2f}"


async def _get_forecast(ctx: ToolCtx, symbol: Any, timeframe: Any, horizon: Any) -> dict:
    sym = _str(symbol, "symbol")
    tf = _timeframe(timeframe)
    h = _int(horizon, "horizon", 5, MAX_HORIZON)
    base = get_settings().neurograph_url
    if not base:
        raise SMTError(ErrorCode.PROVIDER_UNAVAILABLE, "Neurograph is not configured (SMT_NEUROGRAPH_URL)")
    await ensure_instrument(ctx, sym)
    bars = await fetch_bars(_router(ctx), sym, tf, N_BARS)
    if len(bars) < MIN_BARS:
        raise SMTError(ErrorCode.PROVIDER_UNAVAILABLE,
                       f"not enough bars for a forecast ({len(bars)} < {MIN_BARS})",
                       {"symbol": sym, "bars": len(bars)})
    closes = [float(b.close) for b in bars]
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT_S) as client:
            r = await client.post(f"{base.rstrip('/')}/rischio", json={"chiusure": closes, "h": h})
        r.raise_for_status()
        out = r.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise SMTError(ErrorCode.PROVIDER_UNAVAILABLE, f"Neurograph did not answer: {type(exc).__name__}") from exc
    q = out["quantili"]
    last = out["ultimo"]
    steps = [
        {"step": i + 1, "price": {f"q{round(100 * lv)}": f"{p:.6g}" for lv, p in zip(q, row, strict=True)}}
        for i, row in enumerate(out["prezzi"])
    ]
    lo, hi = out["forchetta80"]
    return {
        "symbol": sym, "timeframe": tf.value, "horizon": h, "model": out["modello"],
        "as_of": bars[-1].ts, "last_close": f"{last:.6g}", "bars_used": out["barre_usate"],
        "sigma_pct": _pct(out["sigma_h"]),
        "range80_pct": [_pct(lo), _pct(hi)],
        "range80_price": [steps[-1]["price"]["q10"], steps[-1]["price"]["q90"]],
        "steps": steps,
        "note": ("Risk range, not a direction: 8 times out of 10 the price at `horizon` bars should "
                 "fall inside range80. Use it for sizing, stops and alert levels; it never "
                 "justifies an order by itself."),
    }


def register(mcp: MCPServer) -> None:
    @mcp.tool(structured_output=False)
    async def get_forecast(symbol: str, timeframe: str = "1d", horizon: int | str | None = 5) -> CallToolResult:
        """Risk range of a symbol `horizon` bars ahead (1-20, default 5), from Neurograph's
        GARCH(1,1)-t on the symbol's closed bars (timeframe 1d or 1h recommended). Result:
        {symbol, timeframe, horizon, model, as_of, last_close, sigma_pct, range80_pct: [low%,
        high%], range80_price, steps: [{step, price: {q10..q90}}], note}. A risk measure for
        sizing, stops and alert levels: symmetric, no direction, never an order signal.
        PROVIDER_UNAVAILABLE when Neurograph is not configured or does not answer."""
        params = {"symbol": symbol, "timeframe": timeframe, "horizon": horizon}
        return await run_tool("get_forecast", params,
                              lambda ctx: _get_forecast(ctx, symbol, timeframe, horizon))
