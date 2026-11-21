"""«Mercati v2» (plan 30), pure parts: screener figures and presets from synthetic daily
bars, the v7 fundamentals parsing, the week's calendar, the overview helpers, the
personal alert texts, and the agents' blindness to all of it (no MCP module uses it)."""

import pathlib
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

from smtrading.marketdata.types import Bar
from smtrading.marketsportal import alerts, calendar, fundamentals, overview, screener
from smtrading.marketsportal.yahoo import series

D = Decimal


def _bars(closes, start=date(2025, 1, 1), volumes=None, highs=None, lows=None):
    out = []
    for n, c in enumerate(closes):
        day = start + timedelta(days=n)
        c = D(str(c))
        out.append(Bar(datetime(day.year, day.month, day.day, tzinfo=UTC), c,
                       D(str(highs[n])) if highs else c, D(str(lows[n])) if lows else c, c,
                       D(str(volumes[n])) if volumes else D(100)))
    return out


def test_metrics_changes_and_52_weeks():
    # 400 daily bars: 100 rising to 499 then the last one
    closes = [100 + n for n in range(400)]
    bars = _bars(closes)  # 2025-01-01 .. 2026-02-04
    m = screener.metrics(bars)
    assert m["as_of"] == date(2026, 2, 4) and m["bars"] == 400 and m["close"] == D(499)
    assert m["chg_1d"] == D("0.20")  # 499 / 498
    assert m["chg_1w"] == D("1.42")  # vs 492 (7 days before)
    assert m["chg_1m"] == D("6.40")  # vs 469 (30 days before)
    # ytd: vs the last close of 2025 (2025-12-31 = bar 364 → 464)
    assert m["chg_ytd"] == D("7.54")
    # 52 weeks: the bars after as_of - 365 days (2025-02-04 excluded → from 135)
    assert m["low_52w"] == D(135) and m["high_52w"] == D(499)
    assert m["sma200"] == D("399.5")
    assert m["rsi14"] == D("100.00")
    assert m["avg_volume20"] == D(100)


def test_metrics_short_history():
    assert screener.metrics(_bars([10])) is None
    m = screener.metrics(_bars([10, 11]))
    assert m["sma200"] is None and m["avg_volume20"] is None and m["chg_1w"] is None
    assert m["chg_1d"] == D("10.00") and m["rsi14"] is None


def _row(**kw):
    base = {"close": D(100), "rsi14": D(50), "sma200": D(90), "high_52w": D(150), "low_52w": D(80),
                "day_high": D(101), "day_low": D(99), "volume": D(100), "avg_volume20": D(100)}
    return SimpleNamespace(**{**base, **kw})


def test_presets():
    p = {k: f for k, (_, f) in screener.PRESETS.items()}
    assert p["rsi_oversold"](_row(rsi14=D(29))) and not p["rsi_oversold"](_row(rsi14=D(30)))
    assert p["rsi_overbought"](_row(rsi14=D(71))) and not p["rsi_overbought"](_row())
    assert p["above_sma200"](_row()) and not p["below_sma200"](_row())
    assert not p["above_sma200"](_row(sma200=None)) and not p["below_sma200"](_row(sma200=None))
    assert p["new_high"](_row(day_high=D(150))) and not p["new_high"](_row())
    assert p["new_low"](_row(day_low=D(80))) and not p["new_low"](_row())
    assert p["volume_spike"](_row(volume=D(201))) and not p["volume_spike"](_row(volume=D(200)))
    assert not p["volume_spike"](_row(avg_volume20=None))
    assert p["near_low"](_row(close=D(84))) and not p["near_low"](_row(close=D("84.01")))


def test_screener_on_synthetic_series():
    """An oversold fall to a new low on a volume spike, below its SMA 200."""
    closes = [200] * 250 + [200 - 5 * n for n in range(1, 21)]
    vols = [100] * 269 + [500]
    lows = [c - 1 for c in closes]
    m = screener.metrics(_bars(closes, volumes=vols, lows=lows))
    row = SimpleNamespace(**m)
    hits = {k for k, (_, f) in screener.PRESETS.items() if f(row)}
    assert hits == {"rsi_oversold", "below_sma200", "new_low", "volume_spike", "near_low"}


def test_fundamentals_parse():
    today = date(2026, 11, 21)
    ts = lambda d: int(datetime(d.year, d.month, d.day, 14, tzinfo=UTC).timestamp())
    row = {"marketCap": 4978144444416, "trailingPE": 38.14, "forwardPE": 32.4,
           "dividendYield": 0.31, "fiftyTwoWeekLow": 243.42, "fiftyTwoWeekHigh": 360.83,
           "beta": 1.089, "earningsTimestamp": ts(date(2026, 10, 30)),
           "earningsTimestampStart": ts(date(2027, 1, 28)), "isEarningsDateEstimate": True,
           "exDividendDate": ts(date(2026, 11, 24)), "currency": "USD"}
    f = fundamentals.parse(row, today)
    assert f["market_cap"] == "4978144444416" and f["pe_trailing"] == "38.14"
    assert f["pe_forward"] == "32.4" and f["dividend_yield"] == "0.31" and f["beta"] == "1.089"
    assert f["next_earnings"] == "2027-01-28" and f["earnings_estimate"] is True
    assert f["ex_dividend"] == "2026-11-24" and f["dividend_date"] is None
    empty = fundamentals.parse({"trailingPE": "n/a", "beta": True}, today)
    assert all(empty[k] is None for k in fundamentals.EMPTY_KEYS)


def test_calendar_assemble():
    today = date(2026, 12, 2)  # a Wednesday: window Mon 30/11 .. Sun 13/12
    i = lambda s, sector="Tecnologia": SimpleNamespace(symbol=s, name=s + " Inc", sector=sector)
    figures = {
        "AAA": {"next_earnings": "2026-12-03", "earnings_estimate": True,
                "ex_dividend": "2026-11-29", "source": "yahoo"},  # ex-dividend before
        "BBB": {"next_earnings": "2026-12-03", "ex_dividend": "2026-12-10", "source": "yahoo"},
        "CCC": {"next_earnings": "2026-12-20", "source": "router"},  # after the window
        "DDD": {"next_earnings": None, "source": None},  # an empty entry: not covered
    }
    out = calendar.assemble([i("AAA"), i("BBB"), i("CCC"), i("DDD")], figures,
                            {"BBB": ["Portafoglio eToro"]}, today)
    assert (out["from"], out["to"]) == ("2026-11-30", "2026-12-13")
    got = [(e["date"], e["kind"], e["symbol"] or e["name"], e["held"]) for e in out["events"]]
    assert got == [
        ("2026-12-03", "earnings", "BBB", True),  # held first
        ("2026-12-03", "earnings", "AAA", False),
        ("2026-12-09", "macro", "Fed", False),
        ("2026-12-10", "ex_dividend", "BBB", True),
    ]
    assert out["events"][1]["estimate"] is True and out["covered"] == 3 and out["total"] == 4
    assert out["macro"]["as_of"] == calendar.MACRO_AS_OF


def test_overview_helpers():
    assert len(overview.ITEMS) == 15 and len(overview.BY_CODE) == 15
    assert {i.symbol for i in overview.ITEMS} >= {"^GSPC", "^NDX", "FTSEMIB.MI", "^TNX", "GC=F"}
    pts = [(n, float(n)) for n in range(200)]
    thin = overview._thin(pts, 60)
    assert len(thin) == 60 and thin[0] == [0, 0.0] and thin[-1] == [199, 199.0]
    assert overview.any_open({"CRYPTO": {"is_open": True}, "NYSE": {"is_open": False}}) is False
    assert overview.any_open({"NYSE": {"is_open": True}}) is True
    resp = {"timestamp": [1, 2, 3], "indicators": {"quote": [{"close": [1.5, None, 2.5]}]}}
    assert series(resp) == [(1, 1.5), (3, 2.5)]


def test_alert_text_and_hit():
    a = SimpleNamespace(symbol="SPX", source="overview", direction="above",
                        threshold=D("7000"), note="massimi")
    assert alerts.hit("above", D(7000), D(7000)) and not alerts.hit("above", D(7000), D(6999))
    assert alerts.hit("below", D(10), D(9)) and not alerts.hit("below", D(10), D(11))
    assert alerts.text(a, D("7012.5")) == (
        "📈 Avviso di prezzo: S&P 500 sopra 7.000 (ora 7.012,5)\nNota: massimi")


def test_agents_never_see_markets_v2():
    """No MCP module imports the portal's market tools or names their tables."""
    mcp = pathlib.Path(screener.__file__).resolve().parents[1] / "mcp"
    for f in mcp.rglob("*.py"):
        src = f.read_text()
        for word in ("marketsportal", "portal_favorites", "portal_price_alerts",
                     "market_screen_rows", "routes_markets_v2"):
            assert word not in src, f"{f.name} mentions {word}"
