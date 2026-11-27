"""The quote source of the `scalper`: the Redis cache written by the price stream, never
the providers (the stream manager of the worker stays the only owner of the data
connections)."""
from datetime import date
from decimal import Decimal

from smtrading.marketdata.cache import read_quote_meta
from smtrading.marketdata.types import CalendarEvent, Quote


class CacheQuoteSource:
    """`get_quotes` reads only `quote:{symbol}` (`read_quote_meta`): a symbol without a
    cached quote is missing from the result (the caller's guards decide). The age is
    judged by the caller from the quote's `ts`, so `max_age_s` is ignored here. Exchange
    rates and the calendar go to the router (cached REST, no broker)."""

    def __init__(self, redis, router) -> None:
        self._redis = redis
        self._router = router

    async def get_quotes(self, symbols: list[str], *, max_age_s: int = 5) -> dict[str, Quote]:
        out: dict[str, Quote] = {}
        for symbol in dict.fromkeys(symbols):
            meta = await read_quote_meta(self._redis, symbol)
            if meta is not None:
                out[symbol] = meta[0]
        return out

    async def get_fx_rate(self, base: str, quote: str) -> Decimal:
        return await self._router.get_fx_rate(base, quote)

    async def get_calendar(
        self, symbols: list[str], start: date, end: date
    ) -> list[CalendarEvent]:
        return await self._router.get_calendar(symbols, start, end)
