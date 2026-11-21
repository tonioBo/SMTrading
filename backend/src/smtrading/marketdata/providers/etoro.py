"""eToro as a general market-data source (plan 9 Task 14): quotes and bars of US stocks,
the catalog's EU listings (Milan, Xetra, Paris) and USD crypto, for every portfolio, with its own keys (a dedicated Read key, never the
keys of an eToro broker link).

Docs: https://api-portal.etoro.com/llms.txt (consulted 2026-11-15).
- every request: `x-api-key`, `x-user-key`, `x-request-id` (uuid4); market data only
  (`MARKET_DATA_PREFIX`, GET), checked before any network I/O;
- quotes: `instruments/rates?instrumentIds=` (at most 100 ids); a rate with a
  `conversionRate*` other than 1 is not in USD and is skipped (no schema has a currency),
  except for an EU listing, whose price is in the listing's own currency (EUR) and whose
  conversion rate is EUR→USD (checked live 2026-11-21: A2A.MI 2.199, conversion 1.098);
- bars: `instruments/{id}/history/candles/desc/{interval}/{count}` (no date parameters,
  count at most 1000);
- instrument ids: `search`, exact `internalSymbolFull` only, cached 24 h (`"data"` scope,
  never the broker links' caches), misses for 1 h;
- quota: 60 GET a minute per user key. The provider never waits: once exhausted
  (`RateLimit-Remaining: 0` or a 429) it fails at once until the reset (at most 60 s)
  and the router moves on to the next source.
"""

import json
import math
import time
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx

from smtrading.brokers.etoro_core import (
    ETORO_URL,
    MARKET_DATA_PREFIX,
    account_digest,
    as_int,
    etoro_headers,
    field,
    instrument_cache,
    match_instrument,
    path_is_safe,
    rate_headers,
    search_path,
)
from smtrading.brokers.t212_core import _dec
from smtrading.common.logsetup import register_secret
from smtrading.instruments.models import AssetClass
from smtrading.marketdata.provider import DataProvider
from smtrading.marketdata.registry import register
from smtrading.marketdata.types import Bar, Capability, Quote, Timeframe

RATES_BATCH = 100
MAX_CANDLES = 1000
MAX_LOOKUPS_PER_CALL = 10
MISS_TTL_S = 3600
MAX_BLOCK_S = 60.0
INVALID_KEY = "etoro: chiave non valida"
# the catalog's EU listings (all in EUR): eToro's `internalSymbolFull` is the same symbol
EU_SUFFIXES = (".MI", ".DE", ".PA")
RATE_LIMITED = "etoro: rate limited"

INTERVALS: dict[Timeframe, tuple[str, timedelta]] = {
    Timeframe.m1: ("OneMinute", timedelta(minutes=1)),
    Timeframe.m5: ("FiveMinutes", timedelta(minutes=5)),
    Timeframe.m15: ("FifteenMinutes", timedelta(minutes=15)),
    Timeframe.h1: ("OneHour", timedelta(hours=1)),
    Timeframe.d1: ("OneDay", timedelta(days=1)),
    Timeframe.w1: ("OneWeek", timedelta(weeks=1)),
}

# user key digest -> monotonic time until which the market-data quota is exhausted
_blocked_until: dict[str, float] = {}
# eToro symbol not found (or delisted) -> monotonic time of the search (negative cache)
_missing: dict[str, float] = {}
# injectable for tests
_clock = time.monotonic


def _now() -> datetime:
    return datetime.now(UTC)


def _ts(value: object) -> datetime | None:
    """An ISO 8601 time (UTC when it carries no offset), or None: never now()."""
    if not isinstance(value, str) or not value:
        return None
    try:
        ts = datetime.fromisoformat(value)  # 3.11+: reads "Z"
    except ValueError:
        return None
    return ts if ts.tzinfo is not None else ts.replace(tzinfo=UTC)


def _positive(value: object) -> Decimal | None:
    d = _dec(value)
    return d if d is not None and d > 0 else None


def _is_eu_listing(symbol: str) -> bool:
    return symbol.endswith(EU_SUFFIXES) and len(symbol) > 3


def _in_usd(rate: dict) -> bool:
    """No conversion rate, or 1 (rule 2 of the plan): the price is already in USD."""
    for key in ("conversionRateBid", "conversionRateAsk"):
        raw = rate.get(key)
        if raw is not None and _dec(raw) != 1:
            return False
    return True


@register
class EtoroProvider(DataProvider):
    name = "etoro"
    capabilities = frozenset({Capability.quote, Capability.bars})
    asset_classes = frozenset({AssetClass.us_equity, AssetClass.eu_equity, AssetClass.crypto})
    requires_key = True
    config_fields = ("api_key", "user_key")
    default_rate_limit_per_min = 60

    def __init__(self, config: dict, http: httpx.AsyncClient):
        super().__init__(config, http)
        self._api_key = str(config.get("api_key") or "")
        self._user_key = str(config.get("user_key") or "")
        for secret in (self._api_key, self._user_key):
            register_secret(secret)

    def _default_symbol(self, symbol: str, asset_class: AssetClass) -> str:
        if asset_class == AssetClass.us_equity:
            return symbol
        if asset_class == AssetClass.eu_equity and _is_eu_listing(symbol):
            return symbol
        if asset_class == AssetClass.crypto and symbol.endswith("-USD") and len(symbol) > 4:
            return symbol[: -len("-USD")]
        raise self._unsupported(f"symbol {symbol}")

    # --- the only network access ---------------------------------------------------

    async def _get(self, path: str, params: dict | None = None) -> object:
        if not path_is_safe(path, (MARKET_DATA_PREFIX,)):
            raise RuntimeError("etoro: percorso non ammesso")
        if not self._api_key or not self._user_key:
            raise self._unsupported("request without API key")
        account = account_digest(self._user_key)
        until = _blocked_until.get(account)
        if until is not None:
            if until > _clock():
                raise RuntimeError(RATE_LIMITED)
            _blocked_until.pop(account, None)
        try:
            resp = await self.http.request(
                "GET", ETORO_URL + path, params=params,
                headers=etoro_headers(self._api_key, self._user_key), follow_redirects=False,
            )
        except httpx.RequestError as exc:
            raise RuntimeError(f"etoro: request failed: {type(exc).__name__}") from None
        remaining, reset = rate_headers(resp)
        if remaining is not None and remaining <= 0:
            wait = reset if reset is not None and reset > 0 else MAX_BLOCK_S
            _blocked_until[account] = _clock() + min(wait, MAX_BLOCK_S)
        if resp.status_code == 429:
            raise RuntimeError(RATE_LIMITED)
        if resp.status_code in (401, 403):
            raise RuntimeError(INVALID_KEY)
        if 300 <= resp.status_code < 400:  # never follow to another host
            raise RuntimeError(f"etoro: unexpected redirect HTTP {resp.status_code}")
        if resp.status_code >= 400:  # never eToro's text: it may name its environment
            raise RuntimeError(f"etoro: HTTP {resp.status_code}")
        try:
            return json.loads(resp.content, parse_float=Decimal)
        except ValueError:
            raise RuntimeError(f"etoro: HTTP {resp.status_code}: response is not JSON") from None

    # --- instruments ---------------------------------------------------------------

    async def _search(self, symbol: str) -> dict | None:
        return match_instrument(await self._get(search_path(symbol)), symbol)

    async def _instrument_ids(self, symbols: list[str]) -> dict[str, int]:
        """eToro symbol -> instrument id. At most MAX_LOOKUPS_PER_CALL new searches: the
        other symbols stay without an id this round."""
        cache = instrument_cache("data")
        out: dict[str, int] = {}
        lookups = 0
        for symbol in dict.fromkeys(symbols):
            entry = cache.fresh(symbol)
            if entry is not None and entry["instrument_id"] is not None:
                out[symbol] = entry["instrument_id"]
                continue
            missed = _missing.get(symbol)
            if missed is not None and _clock() - missed < MISS_TTL_S:
                continue
            if lookups >= MAX_LOOKUPS_PER_CALL:
                continue
            lookups += 1
            item = await self._search(symbol)
            iid = as_int(field(item, "instrumentId")) if item is not None else None
            if iid is None or item.get("isDelisted") is True:
                _missing[symbol] = _clock()
                continue
            _missing.pop(symbol, None)
            out[symbol] = cache.put(symbol, iid)["instrument_id"]
        return out

    # --- capabilities --------------------------------------------------------------

    async def get_quotes(self, items: list[tuple[str, str]]) -> dict[str, Quote]:
        ids = await self._instrument_ids([ps for _, ps in items])
        wanted: dict[int, list[str]] = {}
        for canonical, ps in items:
            if ps in ids:
                wanted.setdefault(ids[ps], []).append(canonical)
        out: dict[str, Quote] = {}
        id_list = list(wanted)
        for i in range(0, len(id_list), RATES_BATCH):
            chunk = id_list[i:i + RATES_BATCH]
            data = await self._get(f"{MARKET_DATA_PREFIX}instruments/rates",
                                   {"instrumentIds": ",".join(map(str, chunk))})
            rates = data.get("rates") if isinstance(data, dict) else None
            for rate in rates if isinstance(rates, list) else []:
                if not isinstance(rate, dict):
                    continue
                iid = as_int(field(rate, "instrumentID"))
                canonicals = wanted.get(iid) if iid is not None else None
                last, ts = _positive(rate.get("lastExecution")), _ts(rate.get("date"))
                bid, ask = _positive(rate.get("bid")), _positive(rate.get("ask"))
                if last is None and bid is not None and ask is not None:
                    # eToro sends lastExecution 0.0 outside trading hours: use the mid
                    last = (bid + ask) / 2
                if not canonicals or last is None or ts is None:
                    continue
                for canonical in canonicals:
                    if not _is_eu_listing(canonical) and not _in_usd(rate):
                        continue
                    out[canonical] = Quote(symbol=canonical, bid=bid, ask=ask, last=last,
                                           ts=ts, provider=self.name)
        return out

    async def get_bars(
        self, symbol: str, provider_symbol: str, timeframe: Timeframe, start: datetime,
        end: datetime,
    ) -> list[Bar]:
        if timeframe not in INTERVALS:
            raise self._unsupported(f"timeframe {timeframe}")
        interval, step = INTERVALS[timeframe]
        count = max(1, math.ceil((_now() - start) / step) + 1)
        if count > MAX_CANDLES:
            raise self._unsupported("range")
        iid = (await self._instrument_ids([provider_symbol])).get(provider_symbol)
        if iid is None:
            raise self._unsupported(f"symbol {symbol}")
        data = await self._get(
            f"{MARKET_DATA_PREFIX}instruments/{iid}/history/candles/desc/{interval}/{count}")
        groups = data.get("candles") if isinstance(data, dict) else None
        bars: list[Bar] = []
        for group in groups if isinstance(groups, list) else []:
            if not isinstance(group, dict) or as_int(field(group, "instrumentId")) != iid:
                continue
            for c in group.get("candles") or []:
                if not isinstance(c, dict):
                    continue
                ts = _ts(c.get("fromDate"))
                ohlc = [_dec(c.get(k)) for k in ("open", "high", "low", "close")]
                if ts is None or any(v is None for v in ohlc) or not start <= ts < end:
                    continue
                volume = _dec(c.get("volume"))
                bars.append(Bar(ts, *ohlc, volume if volume is not None else Decimal(0)))
        return sorted(bars, key=lambda b: b.ts)

    async def health_check(self) -> None:
        if await self._search("AAPL") is None:
            raise RuntimeError("etoro: strumento di riferimento non trovato")

