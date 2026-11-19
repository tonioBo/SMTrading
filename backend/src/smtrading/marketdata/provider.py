from collections.abc import AsyncIterator
from datetime import date, datetime
from decimal import Decimal
from typing import ClassVar

import httpx

from smtrading.instruments.models import AssetClass
from smtrading.marketdata.types import (
    Bar,
    CalendarEvent,
    Capability,
    DayChange,
    ExtendedQuote,
    Fundamentals,
    InstrumentHit,
    NewsItem,
    Quote,
    ScreenFilter,
    Timeframe,
)


class ProviderUnsupported(Exception):
    """The provider does not support the capability or the symbol."""


class DataProvider:
    name: ClassVar[str] = ""
    capabilities: ClassVar[frozenset[Capability]] = frozenset()
    asset_classes: ClassVar[frozenset[AssetClass]] = frozenset()
    requires_key: ClassVar[bool] = False
    # config fields the portal shows even when empty and the connector test requires
    # (none declared: `requires_key` stands for one `api_key`)
    config_fields: ClassVar[tuple[str, ...]] = ()
    default_rate_limit_per_min: ClassVar[int] = 60
    # news tagged with bare tickers from any exchange (no listing check on the provider's
    # side): the router keeps an item for a symbol only when its text names the instrument
    news_tags_unverified: ClassVar[bool] = False

    def __init__(self, config: dict, http: httpx.AsyncClient):
        self.config = config
        self.http = http

    def _unsupported(self, what: str) -> ProviderUnsupported:
        return ProviderUnsupported(f"{self.name or type(self).__name__}: {what} not supported")

    def map_symbol(self, symbol: str, asset_class: AssetClass, override: str | None) -> str:
        if override:
            return override
        return self._default_symbol(symbol, asset_class)

    def _default_symbol(self, symbol: str, asset_class: AssetClass) -> str:
        raise self._unsupported(f"symbol {symbol}")

    async def get_quotes(self, items: list[tuple[str, str]]) -> dict[str, Quote]:
        raise self._unsupported("quote")

    # batch day changes (router.get_day_changes): at most `day_change_chunk` items per
    # call, each call costing `day_change_cost` rate-limit tokens (one HTTP request)
    day_change_chunk: ClassVar[int] = 0
    day_change_cost: ClassVar[int] = 1

    async def get_day_changes(self, items: list[tuple[str, str]]) -> dict[str, DayChange]:
        """{canonical: DayChange} for the items it knows (absent ones are left out)."""
        raise self._unsupported("day changes")

    async def get_extended(self, symbol: str, provider_symbol: str) -> ExtendedQuote | None:
        """US equities only: the latest pre-market / after-hours price (information only:
        orders, stops and valuation use the regular session), None when there is none."""
        raise self._unsupported("extended hours")

    async def get_bars(
        self, symbol: str, provider_symbol: str, timeframe: Timeframe, start: datetime, end: datetime
    ) -> list[Bar]:
        raise self._unsupported("bars")

    async def get_news(self, symbols: list[tuple[str, str]], limit: int) -> list[NewsItem]:
        raise self._unsupported("news")

    async def get_fundamentals(self, symbol: str, provider_symbol: str) -> Fundamentals:
        raise self._unsupported("fundamentals")

    async def get_calendar(
        self, symbols: list[tuple[str, str]], start: date, end: date
    ) -> list[CalendarEvent]:
        raise self._unsupported("calendar")

    async def get_fx_rate(self, base: str, quote: str) -> Decimal:
        raise self._unsupported("fx")

    async def get_fx_rate_on(self, base: str, quote: str, day: date) -> Decimal:
        """The reference rate of `day` (a closed day: the last one published up to it)."""
        raise self._unsupported("dated fx")

    async def search(self, query: str, limit: int) -> list[InstrumentHit]:
        raise self._unsupported("search")

    async def screen(self, f: ScreenFilter) -> list[dict]:
        raise self._unsupported("screener")

    async def stream(self, items: list[tuple[str, str]]) -> AsyncIterator[Quote]:
        raise self._unsupported("stream")
        yield  # pragma: no cover - makes this an async generator

    async def health_check(self) -> None:
        raise self._unsupported("health_check")
