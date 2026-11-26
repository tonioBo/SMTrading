from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from smtrading.autorules.indicators import MinuteSeries, SessionVwap
from smtrading.autorules.models import (
    COMMON_PARAMS,
    MODELS,
    PORTFOLIO_BRAKE_DEFAULTS,
    RULE_BRAKE_DEFAULTS,
    MarketState,
    model_catalog,
    validate_brakes,
    validate_params,
)
from smtrading.common.errors import ErrorCode, SMTError
from smtrading.marketdata.types import Bar

D = Decimal
T0 = datetime(2026, 12, 1, 14, 30, tzinfo=UTC)


def bar(i, high, low, close, volume=100, open_=None):
    return Bar(
        ts=T0 + timedelta(minutes=i), open=D(open_ if open_ is not None else close),
        high=D(high), low=D(low), close=D(close), volume=D(volume),
    )


def series_of(bars, maxlen=500):
    s = MinuteSeries(maxlen)
    for b in bars:
        s.add(b)
    return s


def fire(model, state, price, **params):
    p = validate_params(model, params)
    return MODELS[model].entry(state, D(price), p)


# --- indicators ---------------------------------------------------------------------

def test_minute_series_replaces_same_ts_and_ignores_older():
    s = MinuteSeries(3)
    s.add(bar(0, 10, 9, 9.5))
    s.add(bar(1, 11, 9, 10))
    s.add(bar(1, 12, 9, 11))  # same ts: replaces
    s.add(bar(0, 99, 9, 50))  # older: ignored
    assert s.count() == 2
    assert [b.close for b in s.closed(5)] == [D("9.5"), D(11)]
    assert s.highest(2) == D(12)
    assert s.highest(3) is None
    for i in range(2, 6):
        s.add(bar(i, 10, 9, 9))
    assert s.count() == 3  # maxlen


def test_session_vwap_typical_price():
    v = SessionVwap()
    v.reset(T0)
    assert v.value is None
    v.add(bar(-1, 500, 400, 450, 1000))  # before the session start: ignored
    v.add(bar(0, 102, 98, 100, 10))
    v.add(bar(1, 104, 100, 102, 30))
    assert v.value == D("101.5")


def test_session_vwap_zero_volume_is_none():
    v = SessionVwap()
    v.reset(T0)
    v.add(bar(0, 102, 98, 100, 0))
    assert v.value is None


# --- models --------------------------------------------------------------------------

def test_breakout_fires_above_high():
    bars = [bar(i, 100 + (i % 3) * D("0.5"), 99, 100) for i in range(19)] + [bar(19, 101, 99, 100)]
    state = MarketState(series=series_of(bars), vwap=None)
    got = fire("breakout", state, "101.01")
    assert got is not None and got["high"] == D(101)
    assert fire("breakout", state, "101") is None


def test_breakout_volume_filter():
    # avg_volume(20) includes the last bar: 19 × a + v over 20
    low = [bar(i, 100, 99, 100, 37) for i in range(19)] + [bar(19, 100, 99, 100, 57)]  # 57 = 1.5 × 38
    state = MarketState(series=series_of(low), vwap=None)
    assert fire("breakout", state, "100.5", volume_mult=D(2)) is None
    assert fire("breakout", state, "100.5") is not None  # no filter
    high = [bar(i, 100, 99, 100, 9) for i in range(19)] + [bar(19, 100, 99, 100, 19)]  # 19 = 2 × 9.5
    state = MarketState(series=series_of(high), vwap=None)
    got = fire("breakout", state, "100.5", volume_mult=D(2))
    assert got == {"high": D(100), "volume": D(19), "avg_volume": D("9.5")}


def test_mean_reversion_fires_below_band():
    bars = [bar(i, 102, 98, 99 if i % 2 else 101) for i in range(30)]  # mean 100, std 1
    state = MarketState(series=series_of(bars), vwap=None)
    got = fire("mean_reversion", state, "97.99", z=D(2))
    assert got is not None
    assert got["mean"] == D(100) and got["std"] == D(1) and got["z_now"] == D("-2.01")
    assert fire("mean_reversion", state, "98.01", z=D(2)) is None
    flat = MarketState(series=series_of([bar(i, 101, 99, 100) for i in range(30)]), vwap=None)
    assert fire("mean_reversion", flat, "50", z=D(2)) is None  # std 0


def test_vwap_pullback():
    above = [bar(i, 101, "98.9", "100.1", 10) for i in range(5)]  # typical price 100
    v = SessionVwap()
    v.reset(T0)
    for b in above:
        v.add(b)
    assert v.value == D(100)
    state = MarketState(series=series_of(above), vwap=v.value)
    got = fire("vwap_pullback", state, "100.05", pullback_pct=D("0.1"))
    assert got == {"vwap": D(100), "distance_pct": D("0.05")}
    assert fire("vwap_pullback", state, "99.99", pullback_pct=D("0.1")) is None
    assert fire("vwap_pullback", state, "100.2", pullback_pct=D("0.1")) is None
    one_below = above[:4] + [bar(4, "101.2", 99, "99.8", 10)]  # typical price 100, close below
    state = MarketState(series=series_of(one_below), vwap=D(100))
    assert fire("vwap_pullback", state, "100.05", pullback_pct=D("0.1")) is None
    assert fire("vwap_pullback", MarketState(series_of(above), None), "100.05") is None


def test_warmup_per_model():
    assert MODELS["breakout"].warmup(validate_params("breakout", {"lookback_min": 25})) == 25
    assert MODELS["mean_reversion"].warmup(validate_params("mean_reversion", {})) == 30
    assert MODELS["vwap_pullback"].warmup(validate_params("vwap_pullback", {})) == 5


# --- validation ---------------------------------------------------------------------

def test_validate_params_defaults_and_bounds():
    p = validate_params("breakout", {})
    assert p["stop_pct"] == D("0.3") and p["target_pct"] == D("0.5")
    assert p["max_hold_s"] == 300 and p["no_entry_first_min"] == 5
    assert p["no_entry_last_min"] == 15 and p["cooldown_s"] == 60
    assert p["entry"] == "market" and p["limit_offset_pct"] == D("0.05")
    assert p["entry_timeout_s"] == 5 and p["max_age_s"] == D(2)
    assert p["max_spread_pct"] == D("0.10")
    assert p["lookback_min"] == 20 and p["volume_mult"] is None
    assert set(p) == set(COMMON_PARAMS) | {"lookback_min", "volume_mult"}
    # JSON-stored decimals come back as strings
    assert validate_params("breakout", {"stop_pct": "0.4"})["stop_pct"] == D("0.4")
    with pytest.raises(SMTError) as e:
        validate_params("breakout", {"stop_pct": D("0.01")})
    assert e.value.code == ErrorCode.INVALID_ORDER
    assert "stop_pct fuori dai limiti 0,05–5" in e.value.message
    with pytest.raises(SMTError) as e:
        validate_params("breakout", {"foo": 1})
    assert e.value.code == ErrorCode.INVALID_ORDER and "foo" in e.value.message
    with pytest.raises(SMTError) as e:
        validate_params("breakout", {"lookback_min": 4})
    assert "lookback_min fuori dai limiti 5–240" in e.value.message
    for bad in ({"max_hold_s": "300"}, {"max_hold_s": True}, {"stop_pct": "abc"},
                {"entry": "stop"}, {"lookback_min": D("20.5")}):
        with pytest.raises(SMTError) as e:
            validate_params("breakout", bad)
        assert e.value.code == ErrorCode.INVALID_ORDER
    with pytest.raises(SMTError) as e:
        validate_params("martingale", {})
    assert e.value.code == ErrorCode.INVALID_ORDER and "modello sconosciuto" in e.value.message


def test_validate_brakes_levels():
    assert validate_brakes({}, level="rule") == RULE_BRAKE_DEFAULTS
    assert validate_brakes({}, level="portfolio") == PORTFOLIO_BRAKE_DEFAULTS
    assert validate_brakes({"max_open_positions": 3}, level="rule").max_open_positions == 1
    b = validate_brakes({"max_open_positions": 3, "max_loss_day": "50"}, level="portfolio")
    assert b.max_open_positions == 3 and b.max_loss_day == D(50)
    assert validate_brakes({"max_trades_hour": None}, level="rule").max_trades_hour is None
    with pytest.raises(SMTError) as e:
        validate_brakes({"max_loss_day_pct": 60}, level="portfolio")
    assert e.value.code == ErrorCode.INVALID_ORDER and "max_loss_day_pct" in e.value.message
    for bad in ({"max_loss_day": 0}, {"max_loss_day_pct": 0}, {"max_trades_hour": 121},
                {"max_trades_day": 0}, {"max_consecutive_losses": 21}, {"foo": 1}):
        with pytest.raises(SMTError):
            validate_brakes(bad, level="portfolio")
    with pytest.raises(SMTError):
        validate_brakes({"max_open_positions": 31}, level="portfolio")


def test_model_catalog_lists_three_models():
    cat = model_catalog()
    assert [m["name"] for m in cat] == ["breakout", "mean_reversion", "vwap_pullback"]
    br = cat[0]
    names = [p["name"] for p in br["params"]]
    assert "lookback_min" in names and "stop_pct" in names
    lb = next(p for p in br["params"] if p["name"] == "lookback_min")
    assert lb == {"name": "lookback_min", "kind": "int", "lo": 5, "hi": 240, "default": 20,
                  "choices": [], "common": False}
    entry = next(p for p in br["params"] if p["name"] == "entry")
    assert entry["choices"] == ["market", "limit"] and entry["common"] is True
