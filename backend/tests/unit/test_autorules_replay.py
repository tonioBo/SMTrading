"""The one-minute replay of an automatic rule (pure): the price path inside a bar, the
entry after the delay at the ask, the exits (target, stop, time), fees, spread and the
declared limit of one-minute history."""
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest

from smtrading.autorules.replay import MINUTE_NOTE, ReplayResult, price_path, replay
from smtrading.common.errors import SMTError
from smtrading.fees.calculator import FeeRuleData, compute_trade_fee
from smtrading.fees.models import FeeScope, FeeSide
from smtrading.marketdata.types import Bar

D = Decimal
T0 = datetime(2026, 12, 1, 10, 0, tzinfo=UTC)
TICK = D("0.01")


def fee_rule(pct="0") -> FeeRuleData:
    return FeeRuleData(
        id=uuid4(), scope=FeeScope.asset_class, scope_ref="crypto", side=FeeSide.both,
        currency="USD", fixed=D(0), pct=D(pct), min_fee=None, max_fee=None, tiers=None,
        fx_pct=D(0), priority=0, active=True, created_at=T0)


TEN = D(10)
ALPACA_CRYPTO = fee_rule("0.25")  # Alpaca crypto, taker fee of tier 1


def bar(ts, o, h, low, c, v="1") -> Bar:
    return Bar(ts=ts, open=D(o), high=D(h), low=D(low), close=D(c), volume=D(v))


def flat(start: datetime, n: int, p="100") -> list[Bar]:
    return [bar(start + timedelta(minutes=i), p, p, p, p) for i in range(n)]


def at(minute: int, second: int = 0) -> datetime:
    return T0 + timedelta(minutes=minute, seconds=second)


# 20 bars at 100, then a breakout bar 100 → 101.2 (its high is the trigger, at 30 s)
WARM = flat(T0, 20)
BREAKOUT = bar(at(20), "100", "101.2", "100", "101.2")


def run(bars, *, market="CRYPTO", params=None, qty=TEN, notional=None, fee=None,
        spread="0", delay=0, **kw) -> ReplayResult:
    return replay(bars, model="breakout", params={"lookback_min": 20, **(params or {})},
                  market=market, tick_size=TICK, qty=qty, notional=notional,
                  fee_rule=fee or fee_rule(), spread_pct=D(spread), delay_s=delay,
                  day_close_minutes=10, **kw)


def test_price_path_orders():
    up = bar(T0, "100", "102", "99", "101")
    assert price_path(up) == [(T0, D(100)), (T0 + timedelta(seconds=15), D(99)),
                              (T0 + timedelta(seconds=30), D(102)),
                              (T0 + timedelta(seconds=45), D(101))]
    down = bar(T0, "101", "102", "99", "100")
    assert [p for _, p in price_path(down)] == [D(101), D(102), D(99), D(100)]
    unchanged = bar(T0, "100", "101", "99", "100")  # close >= open: rising order
    assert [p for _, p in price_path(unchanged)] == [D(100), D(99), D(101), D(100)]


def test_breakout_trade_with_fees_and_spread():
    after = bar(at(21), "101.3", "101.9", "101.3", "101.8")  # rising: its high hits target
    result = run([*WARM, BREAKOUT, after], fee=ALPACA_CRYPTO, spread="0.1", delay=10)
    [t] = result.trades
    # trigger at 20:30 (ask 101.2 × 1.0005 = 101.2506); after 10 s → the 20:45 point
    assert t.entry_ts == at(20, 45) and t.entry_price == D("101.2506")
    # levels from the ask at the trigger: target ⌈101.2506 × 1.005⌉ = 101.76
    assert t.exit_reason == "target" and t.exit_price == D("101.76")
    assert t.exit_ts == at(21, 30) and t.qty == D(10)
    assert t.fees == compute_trade_fee(ALPACA_CRYPTO, D("1012.506")) + compute_trade_fee(
        ALPACA_CRYPTO, D("1017.60"))
    assert t.fees == D("5.07")  # 2.53 + 2.54
    assert t.net == D("0.024")  # (101.76 - 101.2506) × 10 - 5.07
    assert result.net == D("0.024") and result.fees == D("5.07")
    assert (result.wins, result.losses) == (1, 0) and result.max_drawdown == 0


def test_stop_wins_tie():
    """A falling bar (path O, H, L, C) reaches the target at its high, then the stop at
    its low: inside one minute the order is unknown, the stop is assumed (prudent)."""
    both = bar(at(21), "101.3", "102", "100.5", "100.8")
    result = run([*WARM, BREAKOUT, both])
    [t] = result.trades
    assert t.entry_ts == at(20, 30) and t.entry_price == D("101.2")
    # stop ⌊101.2 × 0.997⌋ = 100.89, target ⌈101.2 × 1.005⌉ = 101.71: both in the bar
    assert t.exit_reason == "stop" and t.exit_ts == at(21, 30)
    assert t.exit_price == D("100.5")  # min(stop, bid) at the low
    assert t.net == D("-7.0")
    assert (result.wins, result.losses) == (0, 1) and result.max_drawdown == D("7.0")


def test_stop_exit_at_most_the_stop():
    """A stop touched exactly fills at the stop (min(stop, bid)), less the spread."""
    down = bar(at(21), "101", "101", "100.89", "100.95")
    [t] = run([*WARM, BREAKOUT, down]).trades
    assert t.exit_reason == "stop" and t.exit_price == D("100.89")
    # with a 0.2% spread: ask at the trigger 101.3012, stop ⌊× 0.997⌋ = 100.99; the low
    # touches it and the bid below it is the fill
    down = bar(at(21), "101.1", "101.1", "100.99", "101.0")
    [t] = run([*WARM, BREAKOUT, down], spread="0.2").trades
    assert t.exit_reason == "stop" and t.exit_price == D("100.99") * (1 - D("0.2") / 200)


def test_time_exit():
    held = flat(at(21), 3, "101.2")
    [t] = run([*WARM, BREAKOUT, *held], params={"max_hold_s": 120}).trades
    assert t.entry_ts == at(20, 30)
    assert t.exit_reason == "time" and t.exit_ts == at(22, 30)
    assert t.exit_price == D("101.2") and t.net == 0


def test_delay_moves_entry():
    after = bar(at(21), "101.3", "101.4", "101.3", "101.4")
    bars = [*WARM, BREAKOUT, after]
    assert run(bars, delay=0).trades[0].entry_ts == at(20, 30)  # the trigger's point
    assert run(bars, delay=10).trades[0].entry_ts == at(20, 45)  # the next point
    late = run(bars, delay=20, spread="0.2").trades[0]  # 20:50 → the next bar's open
    assert late.entry_ts == at(21) and late.entry_price == D("101.3") * (1 + D("0.2") / 200)


def test_runner_exit_waits_the_delay():
    held = flat(at(21), 3, "101.2")
    [t] = run([*WARM, BREAKOUT, *held], params={"max_hold_s": 120}, delay=20,
              spread="0.2").trades
    # filled at 21:00 (20:30 + 20 s → the next bar's open), time exit due at 23:00, sold
    # at the bid of the first point 20 s after it (23:30)
    assert t.entry_ts == at(21) and t.exit_reason == "time"
    assert t.exit_ts == at(23, 30) and t.exit_price == D("101.2") * (1 - D("0.2") / 200)


def test_no_entry_in_session_windows():
    prev = [bar(datetime(2026, 11, 30, 20, 40, tzinfo=UTC) + timedelta(minutes=i),
                "100", "100", "100", "100") for i in range(20)]
    open_ = datetime(2026, 12, 1, 14, 30, tzinfo=UTC)  # NYSE opens 9:30 New York
    early = bar(open_ + timedelta(minutes=2), "100", "101.2", "100", "101.2")
    assert run([*prev, early], market="NYSE").trades == []  # first 5 minutes
    late = bar(datetime(2026, 12, 1, 20, 46, tzinfo=UTC), "100", "101.2", "100", "101.2")
    assert run([*prev, late], market="NYSE").trades == []  # last 15 minutes
    inside = bar(open_ + timedelta(minutes=30), "100", "101.2", "100", "101.2")
    assert len(run([*prev, inside], market="NYSE").trades) == 1  # closed at the end


def test_notes_declare_minute_limit():
    result = run(WARM)
    assert MINUTE_NOTE == ("al minuto non si vedono i movimenti dentro il minuto; volume "
                           "solo IEX per le azioni con il flusso gratuito")
    assert MINUTE_NOTE in result.notes
    assert any("stop" in n for n in result.notes)  # the prudent rule is declared
    assert result.trades == [] and result.net == 0 and result.max_drawdown == 0


def test_notional_sizes_the_quantity():
    after = bar(at(21), "101.3", "101.9", "101.3", "101.8")
    [t] = run([*WARM, BREAKOUT, after], qty=None, notional=D(1000), lot_size=D(1)).trades
    assert t.qty == D(9)  # ⌊1000 / 101.2⌋


def test_open_trade_at_the_end_closed_and_noted():
    result = run([*WARM, BREAKOUT])
    [t] = result.trades
    assert t.exit_reason == "end" and t.exit_ts == at(20, 45) and t.exit_price == D("101.2")
    assert any("fine" in n for n in result.notes)


def test_bad_sizes_refused():
    with pytest.raises(SMTError):
        run(WARM, qty=None, notional=None)
    with pytest.raises(SMTError):
        run(WARM, qty=D(1), notional=D(100))
    with pytest.raises(SMTError):
        run(WARM, delay=-1)
