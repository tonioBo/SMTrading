import json
from datetime import UTC, date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from smtrading.instruments.models import AssetClass
from smtrading.marketdata.provider import DataProvider, ProviderUnsupported
from smtrading.marketdata.registry import PROVIDERS, register
from smtrading.marketdata.serialization import dumps, loads, loads_bars, loads_quote
from smtrading.marketdata.types import (
    Bar,
    CalendarEvent,
    Capability,
    Fundamentals,
    InstrumentHit,
    NewsItem,
    Quote,
    ScreenFilter,
    Timeframe,
)

T0 = datetime(2026, 1, 1, 12, 30, tzinfo=UTC)


def test_quote_roundtrip():
    q = Quote("BTC-USD", Decimal("1.10"), None, Decimal("1.2"), T0, "kraken")
    s = dumps(q)
    assert json.loads(s)["bid"] == "1.10"
    assert loads_quote(s) == q


def test_bars_roundtrip():
    bars = [Bar(T0 + timedelta(minutes=i), Decimal("1.5"), Decimal(2), Decimal(1), Decimal("1.75"), Decimal("1000.123456789012345")) for i in range(3)]
    out = loads_bars(dumps(bars))
    assert out == bars
    assert out[0].ts.tzinfo is not None


def test_news_roundtrip():
    n = NewsItem("1", T0, "h", None, "http://x", "src", ("AAPL", "MSFT"))
    out = loads(dumps(n), NewsItem)
    assert out == n and isinstance(out.symbols, tuple)


def test_fundamentals_roundtrip():
    f = Fundamentals("AAPL", "Apple", None, "Tech", Decimal("3E+12"), Decimal("30.5"), None, None, None, Decimal(1), "USD", {"a": 1})
    assert loads(dumps(f), Fundamentals) == f


def test_calendar_roundtrip():
    c = CalendarEvent("AAPL", "earnings", date(2026, 2, 1), {"eps": "1.2"})
    s = dumps(c)
    assert json.loads(s)["date"] == "2026-02-01"
    assert loads(s, CalendarEvent) == c


def test_hit_roundtrip():
    h = InstrumentHit("BTC-USD", "Bitcoin", AssetClass.crypto, None, "USD", "coingecko")
    s = dumps(h)
    assert json.loads(s)["asset_class"] == "crypto"
    assert loads(s, InstrumentHit) == h
    h2 = InstrumentHit("X", "X", None, None, None, "p")
    assert loads(dumps(h2), InstrumentHit) == h2


def test_screen_filter_roundtrip():
    f = ScreenFilter(asset_class=AssetClass.us_equity, min_volume=Decimal(5), limit=10)
    assert loads(dumps(f), ScreenFilter) == f


def test_datetime_emitted_iso_utc():
    q = Quote("X", None, None, Decimal(1), T0, "p")
    assert json.loads(dumps(q))["ts"] == "2026-01-01T12:30:00+00:00"


def test_naive_and_non_utc_datetime_rejected():
    with pytest.raises(ValueError):
        Quote("X", None, None, None, datetime(2026, 1, 1), "p")
    b = Bar(datetime(2026, 1, 1, 12, tzinfo=timezone(timedelta(hours=2))), *[Decimal(1)] * 5)
    assert b.ts == datetime(2026, 1, 1, 10, tzinfo=UTC)
    assert b.ts.tzinfo is UTC


def test_timeframe_values():
    assert [t.value for t in Timeframe] == ["1m", "5m", "15m", "1h", "1d", "1wk"]


async def test_provider_defaults_unsupported():
    class P(DataProvider):
        name = "p"

    p = P({}, None)  # type: ignore[arg-type]
    with pytest.raises(ProviderUnsupported):
        await p.get_quotes([("A", "A")])
    with pytest.raises(ProviderUnsupported):
        await p.get_fx_rate("EUR", "USD")
    with pytest.raises(ProviderUnsupported):
        async for _ in p.stream([("A", "A")]):
            pass
    with pytest.raises(ProviderUnsupported):
        p.map_symbol("A", AssetClass.crypto, None)
    assert p.map_symbol("A", AssetClass.crypto, "ZZ") == "ZZ"
    assert Capability.quote == "quote"


def test_register_decorator():
    before = set(PROVIDERS)

    @register
    class Q(DataProvider):
        name = "q-test"

    try:
        assert set(PROVIDERS) - before == {"q-test"}
        assert PROVIDERS["q-test"] is Q
    finally:
        PROVIDERS.pop("q-test")
