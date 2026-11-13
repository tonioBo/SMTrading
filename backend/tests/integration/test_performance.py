from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from smtrading.common.errors import ErrorCode, SMTError
from smtrading.performance.service import get_performance
from smtrading.risk.models import EquitySnapshot, SnapshotKind

D = Decimal
NOW = datetime(2026, 11, 13, 12, 0, tzinfo=UTC)


async def _snap(session, portfolio_id, ts, equity, *, day_open=False):
    session.add(EquitySnapshot(
        portfolio_id=portfolio_id, ts=ts,
        kind=SnapshotKind.day_open if day_open else SnapshotKind.periodic,
        local_date=ts.date() if day_open else None,
        equity_eur=D(equity), cash_eur=D(equity), exposure_eur=D(0), margin_used_eur=D(0),
    ))
    await session.flush()


@pytest.fixture
async def series(db_session, portfolio):
    pid = portfolio.id
    await _snap(db_session, pid, NOW - timedelta(days=40), "50", day_open=True)  # outside 1m
    await _snap(db_session, pid, NOW - timedelta(days=3), "100", day_open=True)
    await _snap(db_session, pid, NOW - timedelta(days=3) + timedelta(hours=4), "105")
    await _snap(db_session, pid, NOW - timedelta(days=2), "102", day_open=True)
    await _snap(db_session, pid, NOW - timedelta(days=2) + timedelta(hours=4), "99.75")
    await _snap(db_session, pid, NOW - timedelta(days=1), "106.08", day_open=True)
    await _snap(db_session, pid, NOW - timedelta(hours=2), "110")
    return pid


async def test_return_drawdown_and_sharpe(db_session, series):
    perf = await get_performance(db_session, series, "1m", now=NOW)
    # 1m and longer: the series has the day_open points only ...
    assert [p["equity_eur"] for p in perf["series"]] == [D("100.00"), D("102.00"), D("106.08")]
    assert perf["series"][0]["ts"] == NOW - timedelta(days=3)
    # ... but the statistics use every snapshot of the period
    assert perf["return_pct"] == D("10.0000")
    assert perf["max_drawdown_pct"] == D("-5.0000")  # 105 -> 99.75
    # day_open returns +2%, +4%: mean 3%, sample stdev 1.41421%, x sqrt(252) = 3*sqrt(126)
    assert perf["sharpe"] == D("33.6749")


async def test_periods_filter_the_series(db_session, series):
    everything = await get_performance(db_session, series, "all", now=NOW)
    assert len(everything["series"]) == 4  # day_open points
    assert everything["return_pct"] == D("120.0000")  # 50 -> 110
    assert everything["max_drawdown_pct"] == D("-5.0000")

    day = await get_performance(db_session, series, "1d", now=NOW)
    assert [p["equity_eur"] for p in day["series"]] == [D("106.08"), D("110.00")]
    assert day["return_pct"] == D("3.6953")  # 110/106.08 - 1
    assert day["max_drawdown_pct"] == D("0.0000")
    assert day["sharpe"] is None  # one day_open point


async def test_empty_and_single_point(db_session, portfolio):
    perf = await get_performance(db_session, portfolio.id, "1w", now=NOW)
    assert perf == {"series": [], "return_pct": None, "max_drawdown_pct": None, "sharpe": None}

    await _snap(db_session, portfolio.id, NOW - timedelta(hours=1), "100")
    perf = await get_performance(db_session, portfolio.id, "1w", now=NOW)
    assert perf["return_pct"] == D("0.0000")
    assert perf["max_drawdown_pct"] == D("0.0000")
    assert perf["sharpe"] is None


async def test_flat_day_opens_have_no_sharpe(db_session, portfolio):
    for i in range(3):
        await _snap(db_session, portfolio.id, NOW - timedelta(days=3 - i), "100", day_open=True)
    perf = await get_performance(db_session, portfolio.id, "1w", now=NOW)
    assert perf["sharpe"] is None  # zero standard deviation


async def test_invalid_period(db_session, portfolio):
    with pytest.raises(SMTError) as exc:
        await get_performance(db_session, portfolio.id, "2y", now=NOW)  # type: ignore[arg-type]
    assert exc.value.code == ErrorCode.INVALID_ORDER
    assert exc.value.details["field"] == "period"


def test_downsample_keeps_last_of_each_bucket():
    from smtrading.performance.service import _downsample

    assert _downsample([1, 2, 3], 5) == [1, 2, 3]
    assert _downsample(list(range(10)), 5) == [1, 3, 5, 7, 9]
    assert _downsample(list(range(11)), 5) == [2, 5, 8, 10]
    out = _downsample(list(range(1201)), 500)
    assert len(out) <= 500 and out[-1] == 1200


async def test_intraday_series_is_capped(db_session, portfolio):
    from smtrading.performance.service import MAX_SERIES_POINTS

    start = NOW - timedelta(hours=20)
    for i in range(1200):  # one periodic snapshot a minute
        db_session.add(EquitySnapshot(
            portfolio_id=portfolio.id, ts=start + timedelta(minutes=i),
            kind=SnapshotKind.periodic, equity_eur=D(100 + i % 7), cash_eur=D(0),
            exposure_eur=D(0), margin_used_eur=D(0),
        ))
    await db_session.flush()
    perf = await get_performance(db_session, portfolio.id, "1d", now=NOW)
    assert len(perf["series"]) <= MAX_SERIES_POINTS
    assert perf["series"][-1]["ts"] == start + timedelta(minutes=1199)
    assert perf["max_drawdown_pct"] == D("-5.6604")  # 106 -> 100, on every point


async def test_long_period_without_day_open_keeps_periodic_points(db_session, portfolio):
    await _snap(db_session, portfolio.id, NOW - timedelta(days=2), "100")
    await _snap(db_session, portfolio.id, NOW - timedelta(days=1), "90")
    perf = await get_performance(db_session, portfolio.id, "1m", now=NOW)
    assert [p["equity_eur"] for p in perf["series"]] == [D("100.00"), D("90.00")]
