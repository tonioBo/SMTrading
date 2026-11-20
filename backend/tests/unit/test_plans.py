"""Piani (plan 17): the end of a period, the latest ended period, the refusal of an
addition (no grace since 20/11/2026)."""

from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

from smtrading.common.errors import ErrorCode
from smtrading.goals import plans
from smtrading.goals.periods import Period
from smtrading.goals.plans import ended_at, last_ended_period, plan_closing_error

ROME = ZoneInfo("Europe/Rome")


def test_ended_at_is_local_midnight_after_the_last_day():
    assert ended_at(Period(date(2027, 1, 11), date(2027, 1, 17)), ROME) == datetime(
        2027, 1, 17, 23, 0, tzinfo=UTC)
    # summer time
    assert ended_at(Period(date(2027, 6, 1), date(2027, 6, 30)), ROME) == datetime(
        2027, 6, 30, 22, 0, tzinfo=UTC)


def test_last_ended_period():
    # recurring week, Wednesday: last week
    assert last_ended_period("week", date(2027, 1, 4), None, True, date(2027, 1, 13)) == \
        Period(date(2027, 1, 4), date(2027, 1, 10))
    # recurring, first period not over yet
    assert last_ended_period("week", date(2027, 1, 11), None, True,
                             date(2027, 1, 13)) is None
    # not recurring: its only period once over
    assert last_ended_period("month", date(2027, 1, 5), None, False,
                             date(2027, 1, 20)) is None
    assert last_ended_period("month", date(2027, 1, 5), None, False, date(2027, 2, 1)) == \
        Period(date(2027, 1, 1), date(2027, 1, 31))
    assert last_ended_period("custom", date(2027, 1, 5), date(2027, 1, 9), False,
                             date(2027, 1, 10)) == Period(date(2027, 1, 5), date(2027, 1, 9))


def test_no_grace_left():
    for name in ("grace_days", "grace_deadline", "grace_until", "GRACE_BY_HORIZON"):
        assert not hasattr(plans, name), name


def test_addition_refusal_says_closing_for_the_plan_end():
    e = plan_closing_error("AAPL")
    assert e.code == ErrorCode.RISK_LIMIT_EXCEEDED
    assert e.details == {"reason": "plan_grace", "symbol": "AAPL"}
    assert "posizione in chiusura per fine piano" in e.message
    assert "grazia" not in e.message.lower() and "grace" not in e.message.lower()
