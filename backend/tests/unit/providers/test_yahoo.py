import json
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

import smtrading.marketdata.providers  # noqa: F401
from smtrading.instruments.models import AssetClass
from smtrading.marketdata.provider import ProviderUnsupported
from smtrading.marketdata.providers.yahoo import YahooProvider, screen_query
from smtrading.marketdata.registry import PROVIDERS
from smtrading.marketdata.types import Capability, ScreenFilter, Timeframe

FIX = Path(__file__).parents[2] / "fixtures" / "marketdata"


def fx(name: str):
    return json.loads((FIX / f"yahoo_{name}.json").read_text())


class FakeYF:
    """Replaces _YF: serves recorded fixtures and records the calls."""

    def __init__(self):
        self.calls: list[tuple] = []
        self.infos = {"AAPL": fx("info_AAPL"), "EURUSD=X": fx("info_EURUSD")}
        self.fasts = {
            "AAPL": fx("fast_info_AAPL"),
            "ENI.MI": fx("fast_info_ENIMI"),
            "EURUSD=X": fx("fast_info_EURUSD"),
            "SPY": fx("fast_info_AAPL"),
        }

    def fast_info(self, sym):
        self.calls.append(("fast_info", sym))
        return self.fasts[sym]

    def info(self, sym):
        self.calls.append(("info", sym))
        return self.infos.get(sym, {})

    def history(self, sym, interval, start, end):
        self.calls.append(("history", sym, interval))
        return fx("history_60m" if interval == "60m" else "history_1d")

    def news(self, sym):
        return fx("news")

    def calendar(self, sym):
        return fx("calendar")

    def dividends(self, sym):
        return fx("dividends")

    def splits(self, sym):
        return fx("splits")

    def search(self, q):
        return fx("search")

    def screen(self, query):
        self.calls.append(("screen", query))
        return fx("screen")


@pytest.fixture
async def provider():
    async with httpx.AsyncClient() as http:
        p = YahooProvider({}, http)
        p._yf = FakeYF()
        yield p


def test_registered_and_capabilities():
    p = PROVIDERS["yahoo"]
    assert p is YahooProvider
    assert p.capabilities == {
        Capability.quote, Capability.bars, Capability.news, Capability.fundamentals,
        Capability.calendar, Capability.fx, Capability.search, Capability.screener,
    }  # fmt: skip
    assert p.asset_classes == frozenset(AssetClass)
    assert p.requires_key is False and p.default_rate_limit_per_min == 60


def test_map_symbol(provider):
    m = provider.map_symbol
    assert m("AAPL", AssetClass.us_equity, None) == "AAPL"
    assert m("ENI.MI", AssetClass.eu_equity, None) == "ENI.MI"
    assert m("BTC-USD", AssetClass.crypto, None) == "BTC-USD"
    assert m("EURUSD", AssetClass.forex, None) == "EURUSD=X"
    assert m("EURUSD", AssetClass.forex, "EUR=X") == "EUR=X"
    with pytest.raises(ProviderUnsupported):
        m("EUR", AssetClass.forex, None)


async def test_quotes_with_bid_ask(provider):
    q = (await provider.get_quotes([("AAPL", "AAPL")]))["AAPL"]
    assert q.last == Decimal("354.88") and isinstance(q.last, Decimal)
    assert q.bid == Decimal("355.09") and q.ask == Decimal("361.4")
    assert q.ts == datetime.fromtimestamp(1794517201, UTC)
    assert q.provider == "yahoo"


async def test_quotes_missing_bid_ask_and_time(provider):
    # ENI.MI has no info in the fake: no bid/ask/regularMarketTime -> None / now
    before = datetime.now(UTC)
    q = (await provider.get_quotes([("ENI.MI", "ENI.MI")]))["ENI.MI"]
    assert q.bid is None and q.ask is None and q.last is not None
    assert before <= q.ts <= datetime.now(UTC)


async def test_quotes_zero_bid_ask_and_nan_price(provider):
    provider._yf.infos["AAPL"] = {**fx("info_AAPL"), "bid": 0, "ask": 0.0}
    q = (await provider.get_quotes([("AAPL", "AAPL")]))["AAPL"]
    assert q.bid is None and q.ask is None
    provider._yf.fasts["AAPL"] = {"lastPrice": None}
    assert await provider.get_quotes([("AAPL", "AAPL")]) == {}


async def test_forex_quote(provider):
    q = (await provider.get_quotes([("EURUSD", "EURUSD=X")]))["EURUSD"]
    assert q.last > 0 and q.bid is None and q.ask is None
    assert ("info", "EURUSD=X") not in provider._yf.calls


async def test_bars_interval_mapping_and_conversion(provider):
    start, end = datetime(2026, 11, 1, tzinfo=UTC), datetime(2026, 11, 12, tzinfo=UTC)
    bars = await provider.get_bars("AAPL", "AAPL", Timeframe.h1, start, end)
    assert ("history", "AAPL", "60m") in provider._yf.calls
    assert bars and bars == sorted(bars, key=lambda b: b.ts)
    assert bars[0].ts == datetime(2026, 11, 11, 14, 30, tzinfo=UTC)
    assert bars[0].close == Decimal("353.54") and bars[0].volume == Decimal("8019751.0")
    for tf, iv in [("1m", "1m"), ("5m", "5m"), ("15m", "15m"), ("1d", "1d"), ("1wk", "1wk")]:
        await provider.get_bars("AAPL", "AAPL", Timeframe(tf), start, end)
        assert ("history", "AAPL", iv) in provider._yf.calls


async def test_bars_skip_incomplete_rows(provider):
    rows = fx("history_1d")
    rows[0]["close"] = None
    provider._yf.history = lambda *a: rows
    bars = await provider.get_bars(
        "AAPL",
        "AAPL",
        Timeframe.d1,
        datetime(2026, 1, 1, tzinfo=UTC),
        datetime(2026, 12, 1, tzinfo=UTC),
    )
    assert len(bars) == len(rows) - 1


async def test_news(provider):
    news = await provider.get_news([("AAPL", "AAPL")], 3)
    assert len(news) == 3
    assert news == sorted(news, key=lambda n: n.ts, reverse=True)
    n = news[0]
    assert n.id and n.headline and n.ts.tzinfo is not None
    assert n.url.startswith("https://") and n.source and n.symbols == ("AAPL",)


async def test_news_merges_symbols_across_requests(provider):
    news = await provider.get_news([("AAPL", "AAPL"), ("MSFT", "MSFT")], 100)
    assert len(news) == len(fx("news"))  # same ids -> deduped
    assert all(n.symbols == ("AAPL", "MSFT") for n in news)


async def test_fundamentals(provider):
    f = await provider.get_fundamentals("AAPL", "AAPL")
    assert (
        f.name == "Apple Inc." and f.sector == "Technology" and f.industry == "Consumer Electronics"
    )
    assert f.market_cap == Decimal(5157180932096) and f.pe == Decimal("39.518932")
    assert f.eps == Decimal("8.98") and f.currency == "USD"
    assert f.high_52w == Decimal("359.3684") and f.low_52w is not None
    assert f.extra["beta"] == "1.089"
    empty = await provider.get_fundamentals("X", "X")
    assert empty.name is None and empty.pe is None and empty.market_cap is None


async def test_calendar_filters_by_range(provider):
    ev = await provider.get_calendar([("AAPL", "AAPL")], date(2026, 1, 1), date(2027, 3, 1))
    kinds = {(e.kind, e.date) for e in ev}
    assert ("earnings", date(2027, 1, 28)) in kinds
    assert ("dividend", date(2026, 2, 9)) in kinds and ("dividend", date(2026, 8, 10)) in kinds
    assert ("dividend", date(2025, 11, 10)) not in kinds
    assert not any(e.kind == "split" for e in ev)  # last split is 2020
    earn = next(e for e in ev if e.kind == "earnings")
    assert earn.data["eps_estimate"] == "2.91057"
    div = next(e for e in ev if e.date == date(2026, 2, 9))
    assert div.data == {"amount": "0.26", "currency": "USD"}
    # the currency lookup happens once per symbol, not once per dividend
    assert provider._yf.calls.count(("fast_info", "AAPL")) == 1
    assert [e.date for e in ev] == sorted(e.date for e in ev)


async def test_calendar_splits(provider):
    ev = await provider.get_calendar([("AAPL", "AAPL")], date(2020, 8, 1), date(2020, 9, 30))
    assert [(e.kind, e.data) for e in ev] == [("split", {"ratio": "4.0"})]


async def test_search(provider):
    hits = await provider.search("apple", 2)
    assert len(hits) == 2
    assert hits[0].symbol == "AAPL" and hits[0].name == "Apple Inc."
    assert hits[0].asset_class == AssetClass.us_equity and hits[0].exchange == "NASDAQ"
    assert hits[0].provider == "yahoo"
    assert hits[1].asset_class is None  # futures are not a tradable class here


def test_hit_class_eu_and_crypto():
    assert (
        YahooProvider._hit_class({"quoteType": "EQUITY", "exchange": "MIL"}) == AssetClass.eu_equity
    )
    assert YahooProvider._hit_class({"quoteType": "CRYPTOCURRENCY"}) == AssetClass.crypto
    assert YahooProvider._hit_class({"quoteType": "CURRENCY"}) == AssetClass.forex


async def test_screen_rows_are_decimal(provider):
    rows = await provider.screen(ScreenFilter(min_market_cap=Decimal(200000000000), limit=5))
    assert len(rows) == 5
    r = rows[0]
    assert set(r) == {
        "symbol",
        "name",
        "price",
        "change_pct",
        "market_cap",
        "volume",
        "pe",
        "sector",
    }
    assert r["symbol"] == "XOM" and r["name"] == "ExxonMobil Holdings Corporation"
    for k in ("price", "change_pct", "market_cap", "volume", "pe"):
        assert isinstance(r[k], Decimal)
    assert r["price"] == Decimal("169.78")
    assert provider._yf.calls[-1][1]["limit"] == 5


def test_screen_query_us_minimal():
    q = screen_query(ScreenFilter())
    assert q == {"query": {"operator": "EQ", "operands": ["region", "us"]}, "limit": 25}


def test_screen_query_full():
    f = ScreenFilter(
        asset_class=AssetClass.us_equity, sector="Technology",
        min_market_cap=Decimal(1000000000), max_market_cap=Decimal(5000000000),
        min_change_pct=Decimal(2), max_change_pct=Decimal("9.5"),
        min_volume=Decimal(100000), max_pe=Decimal(25), limit=10,
    )  # fmt: skip
    q = screen_query(f)
    assert q["limit"] == 10 and q["query"]["operator"] == "AND"
    assert q["query"]["operands"] == [
        {"operator": "EQ", "operands": ["region", "us"]},
        {"operator": "EQ", "operands": ["sector", "Technology"]},
        {"operator": "GT", "operands": ["intradaymarketcap", "1000000000"]},
        {"operator": "LT", "operands": ["intradaymarketcap", "5000000000"]},
        {"operator": "GT", "operands": ["percentchange", "2"]},
        {"operator": "LT", "operands": ["percentchange", "9.5"]},
        {"operator": "GT", "operands": ["dayvolume", "100000"]},
        {"operator": "LT", "operands": ["peratio.lasttwelvemonths", "25"]},
    ]


def test_screen_query_eu_regions():
    q = screen_query(ScreenFilter(asset_class=AssetClass.eu_equity))["query"]
    assert q["operator"] == "OR"
    regions = [o["operands"][1] for o in q["operands"]]
    assert "it" in regions and "de" in regions and "us" not in regions


async def test_health_check(provider):
    await provider.health_check()
    assert ("fast_info", "SPY") in provider._yf.calls
    provider._yf.fasts["SPY"] = {"lastPrice": None}
    with pytest.raises(RuntimeError):
        await provider.health_check()


async def test_info_failure_still_yields_quote(provider):
    def boom(sym):
        raise RuntimeError("429")

    provider._yf.info = boom
    q = (await provider.get_quotes([("AAPL", "AAPL")]))["AAPL"]
    assert q.last is not None and q.bid is None and q.ask is None


async def test_failing_symbol_is_dropped_others_returned(provider):
    orig = provider._yf.fast_info

    def flaky(sym):
        if sym == "ENI.MI":
            raise RuntimeError("boom")
        return orig(sym)

    provider._yf.fast_info = flaky
    quotes = await provider.get_quotes(
        [("AAPL", "AAPL"), ("ENI.MI", "ENI.MI"), ("EURUSD", "EURUSD=X")]
    )
    assert set(quotes) == {"AAPL", "EURUSD"}


async def test_quote_concurrency_is_bounded(provider):
    import threading
    import time

    lock = threading.Lock()
    state = {"now": 0, "max": 0}
    orig = provider._yf.fast_info

    def slow(sym):
        with lock:
            state["now"] += 1
            state["max"] = max(state["max"], state["now"])
        time.sleep(0.05)
        with lock:
            state["now"] -= 1
        return orig("AAPL")

    provider._yf.fast_info = slow
    quotes = await provider.get_quotes([(f"S{i}", f"S{i}") for i in range(12)])
    assert len(quotes) == 12
    assert 1 < state["max"] <= 4


async def test_calendar_dividend_currency_falls_back_to_none(provider):
    def boom(sym):
        raise RuntimeError("429")

    provider._yf.fast_info = boom
    ev = await provider.get_calendar([("AAPL", "AAPL")], date(2026, 1, 1), date(2026, 12, 31))
    divs = [e for e in ev if e.kind == "dividend"]
    assert divs and all(e.data["currency"] is None for e in divs)


async def test_all_quotes_failing_raises(provider):
    def boom(sym):
        raise RuntimeError("yahoo down")

    provider._yf.fast_info = boom
    with pytest.raises(RuntimeError, match="yahoo down"):
        await provider.get_quotes([("AAPL", "AAPL"), ("ENI.MI", "ENI.MI")])


async def test_no_price_without_errors_returns_empty(provider):
    # no exception raised: an empty answer means "no data", not an outage
    provider._yf.fasts["AAPL"] = {"lastPrice": None}
    assert await provider.get_quotes([("AAPL", "AAPL")]) == {}


async def test_fx_rate_from_fast_info(provider):
    rate = await provider.get_fx_rate("EUR", "USD")
    assert rate == Decimal("1.1057055") and isinstance(rate, Decimal)
    assert ("fast_info", "EURUSD=X") in provider._yf.calls


async def test_fx_rate_without_price_raises(provider):
    provider._yf.fasts["EURUSD=X"] = {"lastPrice": None}
    with pytest.raises(RuntimeError):
        await provider.get_fx_rate("EUR", "USD")


def test_float32_noise_is_normalised():
    from smtrading.marketdata.providers.yahoo import _dec

    assert _dec(358.5299987792969) == Decimal("358.53")
    assert _dec(354.8800048828125) == Decimal("354.88")
    assert _dec(104523.45) == Decimal("104523.45")  # a real double is kept as is
    assert _dec(0.1) == Decimal("0.1")
