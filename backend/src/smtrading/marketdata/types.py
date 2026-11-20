from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Literal

from smtrading.instruments.models import AssetClass


class Capability(StrEnum):
    quote = "quote"
    bars = "bars"
    stream = "stream"
    news = "news"
    fundamentals = "fundamentals"
    calendar = "calendar"
    fx = "fx"
    search = "search"
    screener = "screener"


class Timeframe(StrEnum):
    m1 = "1m"
    m5 = "5m"
    m15 = "15m"
    h1 = "1h"
    d1 = "1d"
    w1 = "1wk"


def _normalize_utc(obj: object, name: str) -> None:
    """Reject naive datetimes; convert aware ones to UTC (frozen dataclass)."""
    ts = getattr(obj, name)
    if ts.tzinfo is None:
        raise ValueError("datetime must be timezone-aware")
    object.__setattr__(obj, name, ts.astimezone(UTC))


@dataclass(frozen=True)
class Quote:
    symbol: str
    bid: Decimal | None
    ask: Decimal | None
    last: Decimal | None
    ts: datetime
    provider: str
    # set by the router's quality guard (marketdata.quality) when the price could not be
    # confirmed (a jump, or providers that disagree): money logic must not act on it
    suspect: str | None = None

    def __post_init__(self) -> None:
        _normalize_utc(self, "ts")


@dataclass(frozen=True)
class Bar:
    ts: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal

    def __post_init__(self) -> None:
        _normalize_utc(self, "ts")


@dataclass(frozen=True)
class NewsItem:
    id: str
    ts: datetime
    headline: str
    summary: str | None
    url: str | None
    source: str
    symbols: tuple[str, ...]

    def __post_init__(self) -> None:
        _normalize_utc(self, "ts")


@dataclass(frozen=True)
class Fundamentals:
    symbol: str
    name: str | None
    sector: str | None
    industry: str | None
    market_cap: Decimal | None
    pe: Decimal | None
    eps: Decimal | None
    dividend_yield: Decimal | None  # percent units for all providers: 0.3 means 0.3%
    high_52w: Decimal | None
    low_52w: Decimal | None
    currency: str | None
    extra: dict = field(default_factory=dict)


@dataclass(frozen=True)
class CalendarEvent:
    symbol: str
    kind: Literal["earnings", "dividend", "split"]
    date: date
    data: dict = field(default_factory=dict)


@dataclass(frozen=True)
class InstrumentHit:
    symbol: str
    name: str
    asset_class: AssetClass | None
    exchange: str | None
    currency: str | None
    provider: str


@dataclass(frozen=True)
class ScreenFilter:
    asset_class: AssetClass | None = None
    sector: str | None = None
    min_market_cap: Decimal | None = None
    max_market_cap: Decimal | None = None
    min_change_pct: Decimal | None = None
    max_change_pct: Decimal | None = None
    min_volume: Decimal | None = None
    max_pe: Decimal | None = None
    limit: int = 25


@dataclass(frozen=True)
class ExtendedQuote:
    """The latest US pre-market / after-hours trade, for information only: orders, stops,
    fills, signals and valuation use the regular session. `regular_close` is the close of
    the latest regular session (what `price` is compared to)."""

    symbol: str
    session: Literal["pre", "post"]
    price: Decimal
    regular_close: Decimal
    ts: datetime
    provider: str

    def __post_init__(self) -> None:
        _normalize_utc(self, "ts")


@dataclass(frozen=True)
class DayChange:
    """The latest price against the previous session's close (the «Mappa dei settori» and
    «Titoli più mossi» of the Markets page; information only, never used for trading).
    `ts` is the time of `last`."""

    symbol: str
    last: Decimal
    prev_close: Decimal
    ts: datetime
    provider: str

    def __post_init__(self) -> None:
        _normalize_utc(self, "ts")

    @property
    def change_pct(self) -> Decimal:
        return (self.last / self.prev_close - 1) * 100
