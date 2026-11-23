"""Agent cycles of the day (paperclip.cycles): the cron fires of the routines against the
execution issues."""

from datetime import UTC, datetime

import pytest

from smtrading.paperclip import cycles

TZ = "Europe/Rome"
# 19/11/2026 is a Thursday; 10:00 UTC = 11:00 in Rome
NOW = datetime(2026, 11, 19, 10, 0, tzinfo=UTC)


def test_cron_fields():
    c = cycles.Cron("*/15 9-17 * * MON-FRI")
    assert c.minutes == {0, 15, 30, 45} and c.hours == set(range(9, 18))
    assert c.dow == {1, 2, 3, 4, 5}
    assert cycles.Cron("@daily").hours == {0}
    assert cycles.Cron("0 8 * * 7").dow == {0}  # 7 is Sunday too
    for bad in ("* * *", "61 * * * *", "a b c d e", "*/0 * * * *"):
        with pytest.raises(cycles.CronError):
            cycles.Cron(bad)


def test_fires_in_the_trigger_time_zone():
    start = datetime(2026, 11, 19, tzinfo=UTC)
    end = datetime(2026, 11, 20, tzinfo=UTC)
    times = cycles.fires("35 15 * * 1-5", TZ, start, end)
    assert times == [datetime(2026, 11, 19, 14, 35, tzinfo=UTC)]


def _state():
    routines = cycles.normalize_routines([
        {"id": "r1", "title": "Ciclo mattina", "status": "active", "assigneeAgentId": "a1",
         "triggers": [{"kind": "schedule", "cronExpression": "0 9,10,15 * * 1-5",
                       "timezone": TZ, "enabled": True}]},
        {"id": "r2", "title": "In pausa", "status": "paused",
         "triggers": [{"kind": "schedule", "cronExpression": "* * * * *", "enabled": True}]},
        {"id": "r3", "title": "Webhook", "status": "active",
         "triggers": [{"kind": "webhook"}]},
    ])
    executions = cycles.normalize_executions([
        {"originKind": "routine_execution", "originId": "r1", "identifier": "SMT-1",
         "status": "done", "createdAt": "2026-11-19T08:00:05.000Z"},
        {"originKind": "routine_execution", "originId": "r1", "status": "in_progress",
         "createdAt": "2026-11-18T08:00:05.000Z"},  # yesterday
        {"originKind": "manual", "originId": "r1", "createdAt": "2026-11-19T08:30:00Z"},
    ], NOW)
    return {"routines": routines, "executions": executions, "read_at": NOW.isoformat(),
            "error": None}


def test_today_expected_against_done():
    view = cycles.today_cycles(_state(), NOW, TZ, {"a1": "Portfolio Manager"})
    # 09:00 and 10:00 Rome are past at 11:00 Rome, 15:00 is still to come
    assert (view["expected"], view["day_total"], view["done"], view["missing"]) == (2, 3, 1, 1)
    assert view["routines"] == [{"routine": "Ciclo mattina", "agent": "Portfolio Manager",
                                 "expected": 2, "day_total": 3, "done": 1, "completed": 1}]


def test_an_invalid_cron_is_reported_not_raised():
    state = _state()
    state["routines"][0]["triggers"][0]["cron"] = "bad"
    view = cycles.today_cycles(state, NOW, TZ)
    assert view["invalid_cron"] == ["Ciclo mattina"]


def test_executions_keep_when_they_closed():
    """The sentinel judges a fire coalesced into an execution open at that time: the
    closing time of a closed execution (completedAt / cancelledAt, else updatedAt)."""
    out = cycles.normalize_executions([
        {"originKind": "routine_execution", "originId": "r1", "status": "done",
         "createdAt": "2026-11-19T08:00:05Z", "completedAt": "2026-11-19T08:20:00Z",
         "updatedAt": "2026-11-19T08:21:00Z"},
        {"originKind": "routine_execution", "originId": "r1", "status": "cancelled",
         "createdAt": "2026-11-19T08:30:00Z", "updatedAt": "2026-11-19T08:31:00Z"},
        {"originKind": "routine_execution", "originId": "r1", "status": "in_progress",
         "createdAt": "2026-11-19T08:40:00Z", "updatedAt": "2026-11-19T08:41:00Z"},
    ], NOW)
    assert [e["closed_at"] for e in out] == ["2026-11-19T08:20:00+00:00",
                                            "2026-11-19T08:31:00+00:00", None]
