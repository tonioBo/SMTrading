"""«Sentinella di sistema», the checks (pure): what each reading of the system makes a
finding, with its thresholds."""

import uuid
from datetime import UTC, datetime, timedelta

from smtrading.sentinel import checks

NOW = datetime(2026, 11, 23, 10, 0, tzinfo=UTC)  # Monday 11:00 in Rome


def _iso(t):
    return t.isoformat().replace("+00:00", "Z")


# -- 1. «Salute del sistema» ----------------------------------------------------------------

def test_health_ok_is_no_finding():
    assert checks.health_findings({"status": "ok", "reasons": []}) == []


def test_health_problem_is_told_at_once_attention_after_30_minutes():
    (bad,) = checks.health_findings({"status": "problema", "reasons": ["worker fermo"]})
    assert (bad.key, bad.kind, bad.level, bad.after_s, bad.signature) == (
        "health", "health", "critical", 0, "problema")
    assert "worker fermo" in bad.detail
    (warn,) = checks.health_findings({"status": "attenzione", "reasons": ["x", "y"]})
    assert (warn.level, warn.after_s, warn.signature) == ("warning", 30 * 60, "attenzione")
    assert warn.title == "Salute del sistema in «attenzione»"


def test_health_reasons_are_capped():
    (f,) = checks.health_findings({"status": "problema", "reasons": [f"r{i}" for i in range(20)]})
    assert "r7" in f.detail and "r8" not in f.detail and "altri 12" in f.detail


# -- 2. Paperclip agents and runs ---------------------------------------------------------------

AGENTS = [{"id": "a1", "name": "Risk Manager", "status": "error"},
          {"id": "a2", "name": "Executor", "status": "idle"},
          {"id": "a3", "name": "Notifier", "status": "paused"},
          {"id": "a4", "name": "Analyst", "status": None},
          {"id": "a5", "name": "Old", "status": "terminated"},
          {"id": "a6", "name": "CEO", "status": "running"},
          {"id": "a7", "name": "Scout", "status": "active"}]


def test_agents_in_error_paused_or_unknown_after_ten_minutes():
    found = {f.key: f for f in checks.agent_findings(AGENTS, [], NOW)}
    assert set(found) == {"agent:a1", "agent:a3", "agent:a4"}
    assert found["agent:a1"].title == "Agente Risk Manager in errore"
    assert found["agent:a3"].title == "Agente Notifier in pausa"
    assert found["agent:a4"].title == "Agente Analyst in stato sconosciuto"
    assert all(f.after_s == 600 and f.kind == "agent" for f in found.values())
    assert found["agent:a1"].signature == "error"


def test_an_agent_in_error_carries_its_last_failed_run_error():
    runs = [{"id": "r1", "agentId": "a1", "status": "failed", "error": "old",
             "finishedAt": _iso(NOW - timedelta(hours=5))},
            {"id": "r2", "agentId": "a1", "status": "failed",
             "errorMessage": "adapter crashed token=abcdefghijkl",
             "finishedAt": _iso(NOW - timedelta(minutes=3))}]
    found = {f.key: f for f in checks.agent_findings(AGENTS, runs, NOW)}
    assert "adapter crashed" in found["agent:a1"].detail
    assert "abcdefghijkl" not in found["agent:a1"].detail  # redacted


def test_failed_runs_of_the_last_30_minutes_are_events():
    runs = [{"id": "r1", "agentId": "a2", "status": "failed", "errorMessage": "boom",
             "finishedAt": _iso(NOW - timedelta(minutes=10))},
            {"id": "r2", "agentId": "a2", "status": "timed_out",
             "finishedAt": _iso(NOW - timedelta(minutes=50))},
            {"id": "r3", "agentId": "a2", "status": "succeeded",
             "finishedAt": _iso(NOW - timedelta(minutes=1))},
            {"id": "r4", "agentId": "zz", "status": "error", "startedAt": _iso(NOW)}]
    found = {f.key: f for f in checks.agent_findings(AGENTS, runs, NOW)
             if f.kind == "run"}
    assert set(found) == {"run:r1", "run:r4"}
    assert found["run:r1"].event
    assert found["run:r1"].title == "Ciclo dell'agente Executor non riuscito (fallito)"
    assert found["run:r1"].detail == "boom"
    assert "zz" in found["run:r4"].title


def test_blocked_issues_after_30_minutes():
    issues = [{"id": "i1", "identifier": "SMT-1", "title": "PROTEGGI · X", "status": "blocked"},
              {"id": "i2", "identifier": "SMT-2", "title": "Y", "status": "todo"}]
    (f,) = checks.issue_findings(issues)
    assert (f.key, f.kind, f.after_s) == ("issue:i1", "issue", 1800)
    assert f.title == "Task SMT-1 bloccato: «PROTEGGI · X»"


# -- 3. missed cycles -----------------------------------------------------------------------------

def _cycles(executions, *, read_at=NOW, cron="0 * * * *"):
    return {"routines": [{"id": "rt1", "title": "Ciclo orario", "status": "active",
                          "agent_id": "a1",
                          "triggers": [{"cron": cron, "tz": "UTC", "enabled": True}]}],
            "executions": executions, "read_at": _iso(read_at), "error": None}


def _ex(created, status="done", closed=None):
    return {"routine_id": "rt1", "identifier": "SMT-9", "status": status,
            "created_at": created.isoformat(),
            "closed_at": closed.isoformat() if closed else None}


def test_a_fire_with_no_execution_after_the_grace_is_a_missed_cycle():
    # fires at 07:00, 08:00, 09:00 (10:00 not past the grace yet), lookback 3 h
    execs = [_ex(datetime(2026, 11, 23, 7, 0, 5, tzinfo=UTC),
                 closed=datetime(2026, 11, 23, 7, 10, tzinfo=UTC)),
             _ex(datetime(2026, 11, 23, 9, 1, tzinfo=UTC),
                 closed=datetime(2026, 11, 23, 9, 20, tzinfo=UTC))]
    found = checks.cycle_findings(_cycles(execs), NOW, "Europe/Rome", {"a1": "Risk Manager"})
    assert [f.key for f in found] == ["cycle:rt1:2026-11-23T08:00:00+00:00"]
    f = found[0]
    assert f.event and f.kind == "cycle"
    assert f.title == "Ciclo mancato: «Ciclo orario» (Risk Manager) delle 09:00 non è partito"


def test_a_fire_coalesced_into_an_execution_still_open_is_not_missed():
    execs = [_ex(datetime(2026, 11, 23, 7, 0, tzinfo=UTC), status="in_progress")]
    assert checks.cycle_findings(_cycles(execs), NOW, "Europe/Rome", {}) == []


def test_a_fire_coalesced_into_an_execution_closed_after_it_is_not_missed():
    execs = [_ex(datetime(2026, 11, 23, 7, 0, tzinfo=UTC),
                 closed=datetime(2026, 11, 23, 9, 30, tzinfo=UTC))]
    assert checks.cycle_findings(_cycles(execs), NOW, "Europe/Rome", {}) == []


def test_a_fire_within_the_grace_is_not_judged_yet():
    at = datetime(2026, 11, 23, 9, 30, tzinfo=UTC)  # the 09:15 fire is 15 min old
    state = _cycles([], read_at=at, cron="15 9 * * *")
    assert checks.cycle_findings(state, at, "Europe/Rome", {}) == []
    later = at + timedelta(minutes=6)
    state = _cycles([], read_at=later, cron="15 9 * * *")
    assert [f.key for f in checks.cycle_findings(state, later, "Europe/Rome", {})] == [
        "cycle:rt1:2026-11-23T09:15:00+00:00"]


def test_stale_cycles_are_not_checked():
    assert checks.cycle_findings(_cycles([], read_at=NOW - timedelta(minutes=30)), NOW,
                                 "Europe/Rome", {}) is None
    state = _cycles([])
    state["read_at"] = None
    assert checks.cycle_findings(state, NOW, "Europe/Rome", {}) is None


def test_paused_routines_and_disabled_triggers_are_not_expected():
    state = _cycles([])
    state["routines"][0]["status"] = "paused"
    assert checks.cycle_findings(state, NOW, "Europe/Rome", {}) == []
    state = _cycles([])
    state["routines"][0]["triggers"][0]["enabled"] = False
    assert checks.cycle_findings(state, NOW, "Europe/Rome", {}) == []


def test_fires_after_the_last_read_minus_the_grace_are_not_judged_yet():
    # executions known up to read_at: a fire is judged once read_at is past it + grace
    fresh = _cycles([], cron="0,30 * * * *")
    keys = [f.key for f in checks.cycle_findings(fresh, NOW, "Europe/Rome", {})]
    assert keys[-1] == "cycle:rt1:2026-11-23T09:30:00+00:00"
    assert len(keys) == 6  # 07:00 … 09:30
    older = _cycles([], cron="0,30 * * * *", read_at=NOW - timedelta(minutes=14))
    keys = [f.key for f in checks.cycle_findings(older, NOW, "Europe/Rome", {})]
    assert keys[-1] == "cycle:rt1:2026-11-23T09:00:00+00:00"


# -- 4. repeated MCP errors -------------------------------------------------------------------

A1, A2 = uuid.uuid4(), uuid.uuid4()


def test_repeated_mcp_errors_for_the_same_agent_and_tool():
    rows = ([(A1, "Executor", "place_order", "FORBIDDEN")] * 3
            + [(A1, "Executor", "place_order", "INVALID_ORDER")] * 2
            + [(A1, "Executor", "get_quote", "PROVIDER_UNAVAILABLE")] * 4
            + [(A2, "Analyst", "place_order", "FORBIDDEN")] * 4
            + [(A1, "Executor", "place_order", "ok")] * 10)
    (f,) = checks.mcp_findings(rows)
    assert f.key == f"mcp:{A1}:place_order"
    assert f.title == "Executor: 5 errori MCP su place_order in 30 min"
    assert f.detail == "FORBIDDEN ×3, INVALID_ORDER ×2"


def test_any_internal_mcp_error_is_a_finding():
    rows = [(A1, "Executor", "get_positions", "INTERNAL"), (A1, "Executor", "x", "ok")]
    (f,) = checks.mcp_findings(rows)
    assert f.key == "mcp:internal"
    assert f.title == "1 errore interno MCP negli ultimi 30 min"
    assert "Executor: get_positions" in f.detail


# -- 5. error log rate ----------------------------------------------------------------------

def test_error_log_rate_per_logger():
    counts = {("worker", "smtrading.engine"): {"count": 7, "message": "fill failed"},
              ("api", "smtrading.mcp"): {"count": 4, "message": "x"}}
    (f,) = checks.errlog_findings(counts)
    assert f.key == "errlog:worker:smtrading.engine"
    assert f.title == "7 errori nei log del worker (smtrading.engine) in 15 min"
    assert f.detail == "Ultimo: fill failed"


# -- 6. identity burst and decision author ---------------------------------------------------------

def test_identity_burst_is_a_critical_event():
    minute = datetime(2026, 11, 23, 9, 41, tzinfo=UTC)
    (f,) = checks.identity_findings([(minute, 4), (minute + timedelta(minutes=1), 3)], "Europe/Rome")
    assert (f.key, f.event, f.level) == (f"identity:{minute.isoformat()}", True, "critical")
    assert f.title == "4 identità di agenti diverse in un solo minuto su MCP (10:41)"


def test_role_initials_are_the_same_author():
    # «PM» is how the Portfolio Manager signs (seen on 23/11): not another agent
    rows = [(1, "Portfolio Manager", "PM"), (2, "Risk Manager", "RM (sentinella)"),
            (3, "Execution Supervisor", "PM")]
    (f,) = checks.author_findings(rows)
    assert f.key == "author:3"


def test_quota_only_attention_is_not_a_problem_to_report():
    # a provider over its quota is demoted and the router uses the others: no alert
    view = {"status": "attenzione", "reasons": [
        "twelvedata: quota giornaliera al 104%, usato per ultimo fino al reset",
        "yahoo: ha risposto 429 (quota esaurita), usato per ultimo fino al reset"]}
    assert checks.health_findings(view) == []
    view["reasons"].append("worker: job «plan closer» in ritardo")
    (f,) = checks.health_findings(view)
    assert "plan closer" in f.detail and "twelvedata" not in f.detail


def test_decision_author_mismatch():
    rows = [(1, "Risk Manager", "Risk Manager"), (2, "Executor", "il Risk Manager"),
            (3, "Portfolio Manager", "portfolio manager (CEO)")]
    (f,) = checks.author_findings(rows)
    assert (f.key, f.event, f.level) == ("author:2", True, "critical")
    assert f.title == "Decisione 2 firmata da Executor ma scritta «da» il Risk Manager"


# -- 7. agent-visible leaks ------------------------------------------------------------------

P1 = uuid.uuid4()


def test_leaks_grouped_by_portfolio_and_global():
    rows = [(10, P1, "Prova", "order.filled"), (12, P1, "Prova", "broker.reconciliation_diff"),
            (11, None, None, "portfolio.cash")]
    found = {f.key: f for f in checks.leak_findings(rows)}
    assert set(found) == {f"leak:{P1}:12", "leak:global:11"}
    p = found[f"leak:{P1}:12"]
    assert p.event and p.kind == "leak"
    assert p.title == ("2 righe nel registro degli agenti per «Prova», un portafoglio a cui "
                       "nessun agente ha accesso")
    assert "broker.reconciliation_diff, order.filled" in p.detail
    assert found["leak:global:11"].title == (
        "1 riga del registro visibile a tutti gli agenti (senza portafoglio né agente)")


def test_global_types_meant_for_everyone_are_not_leaks():
    assert checks.GLOBAL_TYPES >= {"kill_switch", "halt.set", "halt.cleared",
                                   "instrument.added"}
