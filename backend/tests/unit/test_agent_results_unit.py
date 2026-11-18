"""«Risultati agenti»: the period bounds (local days) and how a lesson names an agent."""

from datetime import UTC, date, datetime

import pytest

from smtrading.performance.agent_results import agent_aliases, period_bounds

TZ = "Europe/Rome"


@pytest.mark.parametrize(("period", "first"), [
    ("week", date(2026, 11, 16)),  # Monday of the week of Wed 18/11
    ("1m", date(2026, 10, 20)),  # 30 days, today included
    ("3m", date(2026, 8, 21)),  # 90 days
])
def test_period_bounds(period, first):
    got, since = period_bounds(period, datetime(2026, 11, 18, 10, 0, tzinfo=UTC), TZ)
    assert got == first
    assert since.date() == first and since.hour == 0 and since.utcoffset() is not None


def test_period_bounds_local_day_and_all():
    # Sunday 23:30 UTC is already Monday in Rome: the new week starts
    got, _ = period_bounds("week", datetime(2026, 11, 22, 23, 30, tzinfo=UTC), TZ)
    assert got == date(2026, 11, 23)
    assert period_bounds("all", datetime(2026, 11, 18, tzinfo=UTC), TZ) == (None, None)


def test_agent_aliases():
    assert agent_aliases("Portfolio Manager") == {"portfolio manager", "pm"}
    assert agent_aliases("CEO SMTrading") == {"ceo smtrading", "ceo"}
    assert agent_aliases("Notifier") == {"notifier"}
    assert "es" in agent_aliases("Execution  Supervisor")
