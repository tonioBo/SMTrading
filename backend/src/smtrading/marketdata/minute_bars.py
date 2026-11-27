"""1-minute bars from the existing price stream, published to a Redis stream per symbol.

The stream manager asks the provider for minute bars only for the symbols of active
automatic rules (intraday mode B) already on its socket; each bar goes to the Redis stream
`bars1m:{symbol}` (trimmed to about BARS_MAXLEN entries), read by the scalper for volume
and the session VWAP. Bars skip the quote quality guard and never touch `quote:{symbol}`.
"""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from smtrading.marketdata.types import Bar

BARS_KEY = "bars1m:{symbol}"
BARS_MAXLEN = 1000


@dataclass(frozen=True)
class StreamBar:
    symbol: str  # canonical
    bar: Bar


# add unless the last entry carries the same bar time (a resent minute).
# KEYS = [stream]; ARGV = [maxlen, ts, o, h, l, c, v]
_PUBLISH_LUA = """
local last = redis.call('XREVRANGE', KEYS[1], '+', '-', 'COUNT', 1)
if #last > 0 then
  local fields = last[1][2]
  for i = 1, #fields, 2 do
    if fields[i] == 'ts' and fields[i + 1] == ARGV[2] then
      return 0
    end
  end
end
redis.call('XADD', KEYS[1], 'MAXLEN', '~', ARGV[1], '*',
           'ts', ARGV[2], 'o', ARGV[3], 'h', ARGV[4], 'l', ARGV[5], 'c', ARGV[6], 'v', ARGV[7])
return 1
"""


def _s(v) -> str:
    return v.decode() if isinstance(v, bytes) else v


async def publish_minute_bar(redis, sb: StreamBar) -> None:
    b = sb.bar
    await redis.eval(
        _PUBLISH_LUA, 1, BARS_KEY.format(symbol=sb.symbol), str(BARS_MAXLEN),
        b.ts.isoformat(), str(b.open), str(b.high), str(b.low), str(b.close), str(b.volume),
    )


async def read_minute_bars(
    redis, symbol: str, *, since_id: str = "0-0", count: int = 1000
) -> list[tuple[str, Bar]]:
    """The bars after entry `since_id` (exclusive), oldest first, at most `count`."""
    found = await redis.xread({BARS_KEY.format(symbol=symbol): since_id}, count=count)
    out: list[tuple[str, Bar]] = []
    for _key, entries in found or []:
        for entry_id, raw in entries:
            f = {_s(k): _s(v) for k, v in raw.items()}
            out.append((_s(entry_id), Bar(
                ts=datetime.fromisoformat(f["ts"]), open=Decimal(f["o"]), high=Decimal(f["h"]),
                low=Decimal(f["l"]), close=Decimal(f["c"]), volume=Decimal(f["v"]),
            )))
    return out
