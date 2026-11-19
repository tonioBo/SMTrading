"""Agent costs per model (paperclip.model_costs): runs bucketed per day, agent and model,
the configured model history and the before/after comparison."""

from datetime import UTC, date, datetime
from decimal import Decimal

from smtrading.paperclip import model_costs as mc

TZ = "Europe/Rome"
T0 = datetime(2026, 11, 19, 8, 0, tzinfo=UTC)


def _run(rid, agent, at, cost=None, model=None, billing="metered_api", **usage):
    u = {"billingType": billing, **usage}
    if cost is not None:
        u["costUsd"] = cost
    if model is not None:
        u["model"] = model
    return {"id": rid, "agentId": agent, "startedAt": at, "usageJson": u}


def test_runs_are_bucketed_by_day_agent_and_model():
    runs = [
        _run("r1", "a1", "2026-11-18T09:00:00Z", 1, "claude-opus"),
        _run("r2", "a1", "2026-11-18T10:00:00Z", 2, "claude-opus"),
        _run("r3", "a1", "2026-11-19T09:00:00Z", 0.5, "claude-sonnet"),
        _run("r4", "a2", "2026-11-19T09:00:00Z", 3, "claude-sonnet",
             billing="subscription_included"),
    ]
    out = mc.bucket_runs(runs, TZ, {}, truncated=False)
    opus = out["2026-11-18"]["a1"]["claude-opus"]
    assert Decimal(opus["api_usd"]) == 3 and opus["runs"] == 2
    assert opus["first_at"] == "2026-11-18T09:00:00+00:00"
    assert opus["last_at"] == "2026-11-18T10:00:00+00:00"
    assert Decimal(out["2026-11-19"]["a2"]["claude-sonnet"]["subscription_usd"]) == 3
    # capped answer: the oldest day is incomplete and dropped
    assert "2026-11-18" not in mc.bucket_runs(runs, TZ, {}, truncated=True)


def test_usage_by_model_is_split_and_a_missing_model_is_derived_from_the_history():
    agents = {"a1": {"name": "Trader", "history": [
        {"model": "old-model", "since": None},
        {"model": "new-model", "since": "2026-11-19T09:38:00+00:00"}]}}
    runs = [
        _run("r1", "a1", "2026-11-19T09:00:00Z", 1),  # before the change: old model
        _run("r2", "a1", "2026-11-19T10:00:00Z", 1),  # after: new model
        _run("r3", "a1", "2026-11-19T11:00:00Z", 4, usageByModel=[
            {"model": "big", "costUsd": 3}, {"model": "small", "costUsd": 1}]),
        _run("r4", "a9", "2026-11-19T11:00:00Z", 1),  # no history: «non indicato»
    ]
    day = mc.bucket_runs(runs, TZ, agents, truncated=False)["2026-11-19"]
    assert day["a1"]["old-model"]["runs"] == 1 and day["a1"]["old-model"]["derived"] == 1
    assert day["a1"]["new-model"]["runs"] == 1
    assert Decimal(day["a1"]["big"]["api_usd"]) == 3 and day["a1"]["big"]["runs"] == 1
    assert Decimal(day["a1"]["small"]["api_usd"]) == 1 and day["a1"]["small"]["runs"] == 0
    assert day["a9"][mc.UNKNOWN]["runs"] == 1


def test_model_changes_are_recorded_with_the_agent_update_time():
    state = {"agents": {}, "changes": []}
    mc.update_agents(state, [{"id": "a1", "name": "Trader",
                              "adapterConfig": {"model": "opus"}}], T0, None)
    assert state["agents"]["a1"]["history"] == [{"model": "opus", "since": None}]
    later = datetime(2026, 11, 19, 9, 40, tzinfo=UTC)
    changes = mc.update_agents(state, [{
        "id": "a1", "name": "Trader", "adapterConfig": {"model": "sonnet"},
        "updatedAt": "2026-11-19T09:38:00Z"}], later, T0)
    assert changes == [{"agent_id": "a1", "name": "Trader", "from": "opus", "to": "sonnet",
                        "at": "2026-11-19T09:38:00+00:00"}]
    assert mc.current_models(state)[0]["model"] == "sonnet"
    # an updatedAt from before the previous read is not the change time: the read is
    mc.update_agents(state, [{"id": "a1", "name": "Trader", "adapterConfig": {"model": "x"},
                              "updatedAt": "2026-11-01T00:00:00Z"}], later, later)
    assert state["changes"][-1]["at"] == later.isoformat()


def test_by_model_and_compare_around_a_change():
    runs = [
        _run("r1", "a1", "2026-11-17T09:00:00Z", 2, "opus"),
        _run("r2", "a1", "2026-11-19T08:00:00Z", 2, "opus"),
        _run("r3", "a1", "2026-11-19T10:00:00Z", 0.5, "sonnet"),
        _run("r4", "a1", "2026-11-20T10:00:00Z", 0.5, "sonnet"),
    ]
    state = {"buckets": mc.bucket_runs(runs, TZ, {}, truncated=False),
             "agents": {"a1": {"name": "Trader", "history": []}}}
    rows = mc.by_model(state, date(2026, 11, 17), date(2026, 11, 20))
    assert [(r["model"], r["runs"], r["billed_per_run_usd"]) for r in rows] == [
        ("opus", 2, Decimal("2.0000")), ("sonnet", 2, Decimal("0.5000"))]
    assert rows[0]["agents"] == ["Trader"]
    at = datetime(2026, 11, 19, 9, 38, tzinfo=UTC)
    cmp = mc.compare(state, at, 7, TZ, datetime(2026, 11, 21, tzinfo=UTC))
    assert [(r["model"], r["runs"]) for r in cmp["before"]["models"]] == [("opus", 2)]
    assert [(r["model"], r["runs"]) for r in cmp["after"]["models"]] == [("sonnet", 2)]
    assert cmp["straddling"] == []
    # a time inside a model's runs of that day: that model's day is left out
    cmp = mc.compare(state, datetime(2026, 11, 19, 9, 0, tzinfo=UTC), 7, TZ,
                     datetime(2026, 11, 21, tzinfo=UTC))
    assert cmp["straddling"] == [] and cmp["after"]["models"][0]["model"] == "sonnet"


def test_merge_keeps_the_last_days_only():
    state = {"buckets": {"2026-01-01": {}, "2026-11-18": {"a": {}}}}
    mc.merge_buckets(state, {"2026-11-19": {"b": {}}}, date(2026, 11, 19))
    assert sorted(state["buckets"]) == ["2026-11-18", "2026-11-19"]
