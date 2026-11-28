"""The «ultima seduta USA» replay preset: the last completed regular NYSE session,
holiday-aware (calendar of `instruments.market_hours`)."""

from datetime import UTC, datetime

from smtrading.flow.map import last_session


def test_during_a_session_the_previous_one():
    # Monday 30/11/2026 16:00 UTC (NYSE open): Friday 27/11 (day after Thanksgiving, early close)
    s = last_session(datetime(2026, 11, 30, 16, tzinfo=UTC))
    assert s["from"] == datetime(2026, 11, 27, 14, 30, tzinfo=UTC)
    assert s["to"] == datetime(2026, 11, 27, 18, 0, tzinfo=UTC)


def test_after_the_close_the_same_day():
    s = last_session(datetime(2026, 11, 30, 22, tzinfo=UTC))
    assert s["from"] == datetime(2026, 11, 30, 14, 30, tzinfo=UTC)
    assert s["to"] == datetime(2026, 11, 30, 21, 0, tzinfo=UTC)


def test_skips_the_holiday():
    # Friday 27/11 10:00 UTC, before the open: Thanksgiving (26/11) skipped → Wednesday 25/11
    s = last_session(datetime(2026, 11, 27, 10, tzinfo=UTC))
    assert s["from"] == datetime(2026, 11, 25, 14, 30, tzinfo=UTC)
    assert s["to"] == datetime(2026, 11, 25, 21, 0, tzinfo=UTC)
