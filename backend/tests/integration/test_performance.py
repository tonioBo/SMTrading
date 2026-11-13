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
    assert [p["equity_eur"] for p in perf["series"]] == [
        D("100.00"), D("105.00"), D("102.00"), D("99.75"), D("106.08"), D("110.00"),
    ]
    assert perf["series"][0]["ts"] == NOW - timedelta(days=3)
    assert perf["return_pct"] == D("10.0000")
    assert perf["max_drawdown_pct"] == D("-5.0000")  # 105 -> 99.75
    # day_open returns +2%, +4%: mean 3%, sample stdev 1.41421%, x sqrt(252) = 3*sqrt(126)
    assert perf["sharpe"] == D("33.6749")


async def test_periods_filter_the_series(db_session, series):
    everything = await get_performance(db_session, series, "all", now=NOW)
    assert len(everything["series"]) == 7
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
