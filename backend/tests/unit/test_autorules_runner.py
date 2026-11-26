from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from smtrading.autorules.runner import Entry, Exit, RuleRunner
from smtrading.marketdata.types import Bar

D = Decimal
SEB = timedelta(minutes=12)  # day_close_minutes 10 + 2


def z(y, mo, d, h, mi=0, s=0):
    return datetime(y, mo, d, h, mi, s, tzinfo=UTC)


def bars_until(end: datetime, n: int, high="100") -> list[Bar]:
    """`n` closed one-minute bars, the last one starting at `end` - 1 min."""
    return [
        Bar(ts=end - timedelta(minutes=n - i), open=D(99), high=D(high), low=D(99),
            close=D(100), volume=D(100))
        for i in range(n)
    ]


def runner(market="NYSE", lookback=5, seb=SEB, **params) -> RuleRunner:
    return RuleRunner("breakout", {"lookback_min": lookback, **params}, market=market,
                      tick_size=D("0.01"), session_exit_before=seb)


def warmed(at: datetime, market="NYSE", **params) -> RuleRunner:
    r = runner(market, **params)
    for b in bars_until(at, 5):
        r.on_bar(b)
    return r


def price(r: RuleRunner, ts: datetime, p="101"):
    return r.on_price(ts, D(p), D(p) - D("0.01"), D(p) + D("0.01"))


def test_no_entry_before_warmup():
    at = z(2026, 12, 1, 15)
    r = runner()
    for b in bars_until(at, 4):
        r.on_bar(b)
    assert price(r, at) is None
    r.on_bar(Bar(ts=at - timedelta(seconds=30), open=D(99), high=D(100), low=D(99),
                 close=D(100), volume=D(100)))
    got = price(r, at)
    assert isinstance(got, Entry)
    assert got.ts == at and got.price == D(101) and got.values["high"] == D(100)


PREV_CLOSE = z(2026, 11, 30, 21)  # bars of the previous regular session warm the series


def test_no_entry_in_first_minutes():
    r = warmed(PREV_CLOSE, no_entry_first_min=5)
    assert price(r, z(2026, 12, 1, 14, 33)) is None
    assert isinstance(price(r, z(2026, 12, 1, 14, 35)), Entry)


def test_no_entry_outside_the_session():
    r = warmed(PREV_CLOSE)
    assert price(r, z(2026, 12, 1, 14, 0)) is None  # pre-market
    assert price(r, z(2026, 11, 26, 16, 0)) is None  # Thanksgiving


def test_no_entry_in_last_window():
    r = warmed(z(2026, 12, 1, 20, 40), no_entry_last_min=15)
    assert isinstance(price(r, z(2026, 12, 1, 20, 44, 59)), Entry)
    assert price(r, z(2026, 12, 1, 20, 45)) is None
    # session_exit_before wider than no_entry_last_min wins
    r = warmed(z(2026, 12, 1, 20, 40), no_entry_last_min=0)
    assert isinstance(price(r, z(2026, 12, 1, 20, 47, 59)), Entry)
    assert price(r, z(2026, 12, 1, 20, 48)) is None


def test_cooldown_after_exit():
    t = z(2026, 12, 1, 16)
    r = warmed(t, cooldown_s=60)
    r.sent(t)
    r.entered(t, D(101))
    r.exited(t + timedelta(seconds=10))
    t_exit = t + timedelta(seconds=10)
    assert price(r, t_exit) is None
    assert price(r, t_exit + timedelta(seconds=59)) is None
    assert isinstance(price(r, t_exit + timedelta(seconds=60)), Entry)


def test_pending_blocks_second_entry():
    t = z(2026, 12, 1, 16)
    r = warmed(t)
    assert isinstance(price(r, t), Entry)
    r.sent(t)
    assert r.state == "pending"
    assert price(r, t + timedelta(seconds=1)) is None
    assert price(r, t + timedelta(hours=1)) is None


def test_time_exit():
    t = z(2026, 12, 1, 16)
    r = warmed(t, max_hold_s=300)
    r.sent(t)
    r.entered(t + timedelta(seconds=1), D(101))
    filled = t + timedelta(seconds=1)
    assert r.state == "long"
    assert price(r, filled + timedelta(seconds=299)) is None
    assert price(r, filled + timedelta(seconds=300)) == Exit(filled + timedelta(seconds=300), "time")
    r.exited(filled + timedelta(seconds=301))
    assert r.state == "flat"


def test_session_exit_before_worker_net():
    t = z(2026, 12, 1, 20, 40)
    r = warmed(t, max_hold_s=3600)
    r.sent(t)
    r.entered(t, D(101))
    assert price(r, z(2026, 12, 1, 20, 47, 59)) is None
    assert price(r, z(2026, 12, 1, 20, 48)) == Exit(z(2026, 12, 1, 20, 48), "session")
    # the half day after Thanksgiving closes at 13:00 ET = 18:00Z
    t = z(2026, 11, 27, 17, 30)
    r = warmed(t, max_hold_s=3600, no_entry_last_min=0)
    r.sent(t)
    r.entered(t, D(101))
    assert price(r, z(2026, 11, 27, 17, 47, 59)) is None
    assert price(r, z(2026, 11, 27, 17, 48)) == Exit(z(2026, 11, 27, 17, 48), "session")


def test_crypto_has_no_windows_or_session_exit():
    t = z(2026, 12, 1, 3)
    r = warmed(t, market="CRYPTO", max_hold_s=3600)
    got = price(r, t)
    assert isinstance(got, Entry)
    r.sent(t)
    r.entered(t, D(101))
    for ts in (z(2026, 12, 1, 3, 59, 59), z(2026, 12, 1, 3, 30)):
        assert price(r, ts) is None
    assert price(r, z(2026, 12, 1, 4)) == Exit(z(2026, 12, 1, 4), "time")


def test_levels_rounded_to_tick():
    r = RuleRunner("breakout", {"stop_pct": D("0.3"), "target_pct": D("0.5")}, market="NYSE",
                   tick_size=D("0.01"), session_exit_before=SEB)
    assert r.levels(D("100.013")) == (D("99.71"), D("100.52"))


def test_canceled_entry_returns_flat_without_cooldown():
    t = z(2026, 12, 1, 16)
    r = warmed(t, cooldown_s=600)
    r.sent(t)
    r.canceled()
    assert r.state == "flat"
    assert isinstance(price(r, t + timedelta(seconds=1)), Entry)


def test_vwap_resets_at_each_session():
    r = RuleRunner("vwap_pullback", {"min_above_min": 1, "no_entry_first_min": 0},
                   market="NYSE", tick_size=D("0.01"), session_exit_before=SEB)
    r.on_bar(Bar(ts=z(2026, 12, 1, 20, 0), open=D(50), high=D(51), low=D(49), close=D(50),
                 volume=D(1000)))
    # next session: the old VWAP (50) is gone; one bar with typical price 100, close above
    r.on_bar(Bar(ts=z(2026, 12, 2, 14, 30), open=D(100), high=D(101), low=D("98.9"),
                 close=D("100.1"), volume=D(10)))
    assert r.vwap == D(100)
    assert isinstance(r.on_price(z(2026, 12, 2, 14, 31), D("100.05"), D(100), D("100.1")), Entry)


def test_crypto_vwap_session_is_the_utc_day():
    r = RuleRunner("vwap_pullback", {"min_above_min": 1}, market="CRYPTO",
                   tick_size=D("0.01"), session_exit_before=SEB)
    r.on_bar(Bar(ts=z(2026, 12, 1, 23, 59), open=D(50), high=D(51), low=D(49), close=D(50),
                 volume=D(1000)))
    assert r.vwap == D(50)
    r.on_bar(Bar(ts=z(2026, 12, 2, 0, 0), open=D(100), high=D(101), low=D("98.9"),
                 close=D("100.1"), volume=D(10)))
    assert r.vwap == D(100)


def vbar(ts, close="100.1", volume=10):
    """Typical price 100 (101 + 98.9 + 100.1 over 3), close above it by default."""
    c = D(close)
    return Bar(ts=ts, open=D(100), high=D(101), low=D(300) - D(101) - c, close=c,
               volume=D(volume))


def test_equity_bars_outside_the_regular_session_are_ignored():
    r = runner()
    r.on_bar(vbar(z(2026, 12, 1, 14, 29)))  # pre-market
    r.on_bar(vbar(z(2026, 12, 1, 21, 0)))  # after-hours
    r.on_bar(vbar(z(2026, 11, 26, 16, 0)))  # holiday
    assert r.series.count() == 0 and r.vwap is None
    r.on_bar(vbar(z(2026, 12, 1, 14, 30)))
    r.on_bar(vbar(z(2026, 12, 1, 20, 59)))
    assert r.series.count() == 2
    c = runner(market="CRYPTO")
    c.on_bar(vbar(z(2026, 12, 1, 3, 0)))
    assert c.series.count() == 1


def test_replaced_bar_keeps_vwap_and_series_in_step():
    r = RuleRunner("vwap_pullback", {}, market="NYSE", tick_size=D("0.01"),
                   session_exit_before=SEB)
    t = z(2026, 12, 1, 15)
    r.on_bar(Bar(ts=t, open=D(50), high=D(51), low=D(49), close=D(50), volume=D(1000)))
    r.on_bar(vbar(t))  # the provider re-sends the corrected minute
    assert r.series.count() == 1 and r.vwap == D(100)


def test_vwap_pullback_needs_session_bars_beyond_first_minutes():
    r = RuleRunner("vwap_pullback", {"min_above_min": 5, "no_entry_first_min": 0},
                   market="NYSE", tick_size=D("0.01"), session_exit_before=SEB)
    for i in range(5):  # previous session, all above its VWAP
        r.on_bar(vbar(z(2026, 11, 30, 20, 55 + i)))
    for i in range(2):
        r.on_bar(vbar(z(2026, 12, 1, 14, 30 + i)))
    assert r.on_price(z(2026, 12, 1, 14, 32), D("100.05"), D(100), D("100.1")) is None
    for i in range(2, 5):
        r.on_bar(vbar(z(2026, 12, 1, 14, 30 + i)))
    got = r.on_price(z(2026, 12, 1, 14, 35), D("100.05"), D(100), D("100.1"))
    assert isinstance(got, Entry) and got.values["vwap"] == D(100)


def test_naive_datetimes_are_rejected():
    r = warmed(z(2026, 12, 1, 16))
    naive = datetime(2026, 12, 1, 16)
    with pytest.raises(ValueError):
        r.on_price(naive, D(101), D(101), D(101))
    for call in (lambda: r.sent(naive), lambda: r.entered(naive, D(101)),
                 lambda: r.exited(naive)):
        with pytest.raises(ValueError):
            call()
