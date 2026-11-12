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


def _require_utc(ts: datetime) -> None:
    if ts.tzinfo is None or ts.utcoffset() != UTC.utcoffset(None):
        raise ValueError("datetime must be timezone-aware UTC")


@dataclass(frozen=True)
class Quote:
    symbol: str
    bid: Decimal | None
    ask: Decimal | None
    last: Decimal | None
    ts: datetime
    provider: str

    def __post_init__(self) -> None:
        _require_utc(self.ts)


@dataclass(frozen=True)
class Bar:
    ts: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal

    def __post_init__(self) -> None:
        _require_utc(self.ts)


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
        _require_utc(self.ts)


@dataclass(frozen=True)
class Fundamentals:
    symbol: str
    name: str | None
    sector: str | None
    industry: str | None
    market_cap: Decimal | None
    pe: Decimal | None
    eps: Decimal | None
    dividend_yield: Decimal | None
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
