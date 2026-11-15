"""Paperclip costs, pure functions: the billed amount of a run, its bucket and the daily
grouping per agent (local time zone, oldest partial day dropped)."""

from datetime import date
from decimal import Decimal

from smtrading.paperclip.costs import billed_usd, bucket, daily_costs


def test_billed_prefers_cache_adjusted():
    assert billed_usd({"cacheAdjustedCostUsd": 0.25, "costUsd": 1.0}) == Decimal("0.25")
    assert billed_usd({"cacheAdjustedCostUsd": 0, "costUsd": 1.0}) == Decimal(0)
    assert billed_usd({"cacheAdjustedCostUsd": -1, "costUsd": 1.5}) == Decimal("1.5")
    assert billed_usd({"cacheAdjustedCostUsd": None, "costUsd": "0.75"}) == Decimal("0.75")
    assert billed_usd({"cacheAdjustedCostUsd": True, "costUsd": -2}) is None
    assert billed_usd({"costUsd": "abc"}) is None
    assert billed_usd({}) is None


def test_bucket_mapping():
    assert bucket("metered_api") == "api"
    assert bucket("subscription_included") == "subscription"
    assert bucket("subscription_overage") == "subscription"
    for other in ("credits", "fixed", "unknown", None, "new_kind"):
        assert bucket(other) == "other"


def _run(agent, ts, cost, billing="metered_api", field="startedAt"):
    return {"agentId": agent, field: ts,
            "usageJson": {"costUsd": cost, "billingType": billing}}


def test_daily_costs_drops_oldest_partial_day_and_uses_local_tz():
    runs = [
        # oldest run: 2026-11-12 in Rome -> that day is incomplete and dropped
        _run("a1", "2026-11-12T10:00:00Z", 9),
        # 23:30 UTC on the 13th is already the 14th in Rome (UTC+1)
        _run("a1", "2026-11-13T23:30:00Z", 1, field="createdAt"),
        _run("a1", "2026-11-14T08:00:00Z", 2, billing="subscription_included"),
        _run("a1", "2026-11-14T09:00:00Z", 0.5, billing="credits", field="finishedAt"),
        _run("a2", "2026-11-13T12:00:00Z", 3),
        {"agentId": "a2", "startedAt": "2026-11-13T13:00:00Z", "usageJson": None},
        {"agentId": None, "startedAt": "2026-11-13T13:00:00Z"},  # no agent: ignored
        {"agentId": "a3"},  # no date: ignored
    ]
    out = daily_costs(runs, "Europe/Rome")
    assert set(out) == {(date(2026, 11, 14), "a1"), (date(2026, 11, 13), "a2")}
    a1 = out[(date(2026, 11, 14), "a1")]
    assert (a1.api_usd, a1.subscription_usd, a1.other_usd, a1.runs) == (
        Decimal(1), Decimal(2), Decimal("0.5"), 3)
    a2 = out[(date(2026, 11, 13), "a2")]
    assert (a2.api_usd, a2.subscription_usd, a2.other_usd, a2.runs) == (
        Decimal(3), Decimal(0), Decimal(0), 2)
    # a complete history (fewer runs than the limit) keeps its oldest day
    full = daily_costs(runs, "Europe/Rome", truncated=False)
    assert full[(date(2026, 11, 12), "a1")].api_usd == Decimal(9)


def test_real_shaped_run():
    """A heartbeat run as Paperclip returns it: the top-level costUsd is null, the amounts
    are in usageJson, startedAt has milliseconds and a Z."""
    run = {"id": "r1", "companyId": "c", "agentId": "a1", "status": "succeeded",
           "inputTokens": "1200", "totalTokens": None, "costUsd": None,
           "startedAt": "2026-11-15T09:12:33.123Z", "finishedAt": "2026-11-15T09:13:01.456Z",
           "usageJson": {"model": "m", "biller": "b", "costUsd": 0.31,
                         "cacheAdjustedCostUsd": 0.12, "billingType": "subscription_included",
                         "costUsdExact": None, "inputTokens": 1200}}
    out = daily_costs([run], "Europe/Rome", truncated=False)
    row = out[(date(2026, 11, 15), "a1")]
    assert (row.api_usd, row.subscription_usd, row.other_usd, row.runs) == (
        Decimal(0), Decimal("0.12"), Decimal(0), 1)
