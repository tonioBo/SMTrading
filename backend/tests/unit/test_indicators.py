from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pandas as pd
import pytest

from smtrading.common.errors import ErrorCode, SMTError
from smtrading.marketdata.indicators import atr, bollinger, ema, macd, rsi, sma, vwap
from smtrading.marketdata.indicators_service import IndicatorSpec, compute_indicators
from smtrading.marketdata.types import Bar, Timeframe

_RAW = (
    "44.34 44.09 44.15 43.61 44.33 44.83 45.10 45.42 45.84 46.08 45.89 46.03 45.61 46.28 46.28 "
    "46.00 46.03 46.41 46.22 45.64 46.21 46.25 45.71 46.45 45.78 45.35 44.03 44.18 44.22 44.57 "
    "43.42 42.66 43.13 43.50 44.10 44.60 45.20 45.01 45.55 46.10 46.80 47.20 46.90 47.50 48.00 "
    "47.60 47.10 46.70 47.30 47.90 48.40 48.10 47.80 48.60 49.20 49.00 48.50 48.90 49.40 49.10 "
)
PRICES = [Decimal(x) for x in _RAW.split()]
assert len(PRICES) == 60
TOL = 1e-6


def _bars(n=60) -> list[Bar]:
    t0 = datetime(2026, 1, 1, tzinfo=UTC)
    out = []
    for i, c in enumerate(PRICES[:n]):
        out.append(
            Bar(
                ts=t0 + timedelta(days=i),
                open=c,
                high=c + Decimal("0.5") + Decimal(i % 3) / 10,
                low=c - Decimal("0.4") - Decimal(i % 4) / 10,
                close=c,
                volume=Decimal(100 + i),
            )
        )
    return out


def _f(xs):
    return pd.Series([None if x is None else float(x) for x in xs], dtype="float64")


def _close(actual, expected: pd.Series, start: int):
    assert len(actual) == len(expected)
    for i in range(start, len(actual)):
        assert actual[i] is not None, i
        assert abs(float(actual[i]) - expected[i]) < TOL, (i, actual[i], expected[i])


def _ref_ema(s: pd.Series, n: int) -> pd.Series:
    # pandas ewm(adjust=False) seeds with the first value; we seed with the SMA of the
    # first n values (as the implementation does) by replacing the seed element.
    t = s.iloc[n - 1 :].copy()
    t.iloc[0] = s.iloc[:n].mean()
    out = pd.Series([float("nan")] * len(s))
    out.iloc[n - 1 :] = t.ewm(span=n, adjust=False).mean().values
    return out


def test_sma_matches_rolling():
    _close(sma(PRICES, 10), _f(PRICES).rolling(10).mean(), 9)
    assert sma(PRICES, 10)[8] is None


def test_ema_matches_pandas():
    for n in (5, 12):
        _close(ema(PRICES, n), _ref_ema(_f(PRICES), n), 3 * n)


def test_rsi_matches_wilder_pandas():
    n = 14
    s = _f(PRICES)
    d = s.diff()
    gain, loss = d.clip(lower=0), (-d).clip(lower=0)

    def wilder(x):
        t = x.iloc[n:].copy()
        t.iloc[0] = x.iloc[1 : n + 1].mean()
        return t.ewm(alpha=1 / n, adjust=False).mean().values

    ag, al = wilder(gain), wilder(loss)
    ref = pd.Series([float("nan")] * len(s))
    ref.iloc[n:] = 100 - 100 / (1 + ag / al)
    got = rsi(PRICES, n)
    assert all(v is None for v in got[:n])
    _close(got, ref, 3 * n)


def test_rsi_all_gains_is_100():
    vals = [Decimal(i) for i in range(1, 30)]
    assert rsi(vals, 14)[-1] == Decimal(100)


def test_macd_matches_pandas():
    s = _f(PRICES)
    fast, slow, sig = 12, 26, 9
    m = _ref_ema(s, fast) - _ref_ema(s, slow)
    m_def = m.iloc[slow - 1 :].reset_index(drop=True)
    sg = pd.Series([float("nan")] * len(s))
    sg.iloc[slow - 1 :] = _ref_ema(m_def, sig).values
    got_m, got_s, got_h = macd(PRICES, fast, slow, sig)
    assert got_m[slow - 2] is None and got_m[slow - 1] is not None
    assert got_s[slow + sig - 3] is None and got_s[slow + sig - 2] is not None
    _close(got_m, m, 3 * fast)
    _close(got_s, sg, 3 * slow - 1 if 3 * slow - 1 < 60 else 59)
    assert abs(float(got_h[59]) - (m[59] - sg[59])) < TOL


def test_bollinger_matches_pandas_population_std():
    s = _f(PRICES)
    mid, up, lo = bollinger(PRICES, 20, 2)
    _close(mid, s.rolling(20).mean(), 19)
    std = s.rolling(20).std(ddof=0)
    _close(up, s.rolling(20).mean() + 2 * std, 19)
    _close(lo, s.rolling(20).mean() - 2 * std, 19)
    assert mid[18] is None


def test_atr_matches_wilder_pandas():
    n = 14
    bars = _bars()
    h, l_, c = (_f([getattr(b, k) for b in bars]) for k in ("high", "low", "close"))
    pc = c.shift(1)
    tr = pd.concat([h - l_, (h - pc).abs(), (l_ - pc).abs()], axis=1).max(axis=1)
    tr.iloc[0] = h.iloc[0] - l_.iloc[0]
    t = tr.iloc[n - 1 :].copy()
    t.iloc[0] = tr.iloc[:n].mean()
    ref = pd.Series([float("nan")] * len(tr))
    ref.iloc[n - 1 :] = t.ewm(alpha=1 / n, adjust=False).mean().values
    got = atr(bars, n)
    assert got[n - 2] is None
    _close(got, ref, 3 * n)


def test_short_series_all_none():
    short = PRICES[:5]
    assert sma(short, 10) == [None] * 5
    assert ema(short, 10) == [None] * 5
    assert rsi(short, 14) == [None] * 5
    assert atr(_bars(5), 14) == [None] * 5
    for part in (*macd(short), *bollinger(short)):
        assert part == [None] * 5


def test_empty_series():
    assert sma([], 3) == [] and ema([], 3) == [] and rsi([], 3) == []
    assert atr([], 3) == [] and vwap([]) == []
    assert macd([]) == ([], [], []) and bollinger([]) == ([], [], [])


def test_vwap_three_bars_by_hand():
    t = datetime(2026, 1, 1, tzinfo=UTC)

    def b(h, l_, c, v):
        return Bar(
            ts=t, open=c, high=Decimal(h), low=Decimal(l_), close=Decimal(c), volume=Decimal(v)
        )

    bars = [b(12, 6, 9, 10), b(15, 9, 12, 20), b(18, 12, 15, 30)]
    # typical: 9, 12, 15 -> cum pv: 90, 330, 780 ; cum v: 10, 30, 60
    assert vwap(bars) == [Decimal(9), Decimal(11), Decimal(13)]


def test_results_quantized_to_8_decimals():
    out = sma([Decimal(1), Decimal(1), Decimal(2)], 3)
    assert out[2] == Decimal("1.33333333")


class FakeRouter:
    def __init__(self, bars):
        self.bars, self.calls = bars, []

    async def get_bars(self, symbol, timeframe, start, end):
        self.calls.append((symbol, timeframe, start, end))
        return self.bars


async def test_compute_indicators_shapes_and_lookback():
    router = FakeRouter(_bars())
    specs = [
        IndicatorSpec("sma", {"period": 5}),
        IndicatorSpec("macd", {}),
        IndicatorSpec("vwap", {}),
    ]
    out = await compute_indicators(router, "AAPL", Timeframe.d1, specs, lookback=10)
    assert out["symbol"] == "AAPL" and out["timeframe"] == "1d"
    assert len(out["ts"]) == 10 and len(out["sma"]) == 10 and len(out["vwap"]) == 10
    assert set(out["macd"]) == {"macd", "signal", "hist"} and len(out["macd"]["hist"]) == 10
    assert out["sma"][-1] == sma(PRICES, 5)[-1]
    _sym, tf, start, end = router.calls[0]
    assert tf == Timeframe.d1 and end - start >= timedelta(days=(10 + 35 * 3))


async def test_compute_indicators_invalid_spec():
    for spec in (
        IndicatorSpec("nope", {}),
        IndicatorSpec("sma", {"bogus": 1}),
        IndicatorSpec("sma", {"period": 0}),
    ):
        with pytest.raises(SMTError) as e:
            await compute_indicators(FakeRouter(_bars()), "AAPL", Timeframe.d1, [spec])
        assert e.value.code == ErrorCode.INVALID_ORDER
