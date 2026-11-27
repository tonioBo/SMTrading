"""«Salute del sistema» aggregation (admin.system_health): the pure section builders with
plain values and a fake Redis."""

import json
import time
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

from smtrading.admin import system_health as sh
from smtrading.common.heartbeat import HEARTBEAT_KEY
from smtrading.paperclip import budget
from smtrading.worker.jobstate import JOBS_KEY, WORKER_FIELD

NOW = datetime(2026, 11, 19, 10, 0, tzinfo=UTC)


class FakeRedis:
    def __init__(self, heartbeat: float | None, jobs: dict | None = None) -> None:
        self.values = {} if heartbeat is None else {HEARTBEAT_KEY: str(heartbeat)}
        self.hash = {k: json.dumps(v) for k, v in (jobs or {}).items()}

    async def get(self, key):
        return self.values.get(key)

    async def hgetall(self, key):
        assert key == JOBS_KEY
        return self.hash


def test_worst_and_off_counts_as_ok():
    assert sh.worst([sh.OK, sh.OFF]) == sh.OK
    assert sh.worst([sh.OK, sh.WARN, sh.OFF]) == sh.WARN
    assert sh.worst([sh.WARN, sh.BAD]) == sh.BAD


def test_job_rows_flag_restarts_recent_crashes_and_late_passes():
    now = 100_000.0
    jobs = {
        "paperclip reader": {"state": "running", "started_at": now - 5000,
                             "last_tick_at": now - 100, "crashes": 0},
        "job scheduler": {"state": "running", "started_at": now - 5000,
                          "last_tick_at": now - 500, "crashes": 0},  # 30 s job: late
        "order processor": {"state": "running", "started_at": now - 60, "crashes": 2,
                            "last_crash_at": now - 60, "last_error": "RuntimeError: x"},
        "broker sync": {"state": "restarting", "started_at": now - 50, "crashes": 1,
                        "last_crash_at": now - 3},
        # gated job that has not passed yet: the startup gate grace applies
        "goal alerts": {"state": "running", "started_at": now - 1000, "last_tick_at": None},
    }
    rows = {r["name"]: r for r in sh.job_rows(jobs, now - 5000, now)}
    assert rows["paperclip reader"]["status"] == sh.OK
    assert rows["job scheduler"]["status"] == sh.WARN
    assert rows["job scheduler"]["note"] == "ultimo giro 8 min fa"
    assert rows["order processor"]["status"] == sh.WARN
    assert rows["order processor"]["last_error"] == "RuntimeError: x"
    assert rows["broker sync"]["status"] == sh.BAD
    assert rows["goal alerts"]["status"] == sh.OK


async def test_services_worker_down_is_a_problem():
    s = await sh.services_section(FakeRedis(None), time.time())
    assert s["status"] == sh.BAD and s["reasons"][0] == "worker fermo: nessun battito"
    now = time.time()
    s = await sh.services_section(FakeRedis(now - 5, {
        WORKER_FIELD: {"started_at": now - 3600},
        "health checks": {"state": "running", "started_at": now - 3600,
                          "last_tick_at": now - 10}}), now)
    assert s["status"] == sh.OK and s["jobs"][0]["name"] == "health checks"
    assert s["worker_started_at"] is not None


def test_providers_rows():
    fresh = NOW - timedelta(minutes=3)
    rows = sh.provider_rows([
        SimpleNamespace(name="yahoo", enabled=True, health_status="ok", last_error=None,
                        last_checked_at=fresh),
        SimpleNamespace(name="alpaca", enabled=True, health_status="error",
                        last_error="HTTP 401", last_checked_at=fresh),
        SimpleNamespace(name="off", enabled=False, health_status="error", last_error=None,
                        last_checked_at=None),
        SimpleNamespace(name="old", enabled=True, health_status="ok", last_error=None,
                        last_checked_at=NOW - timedelta(hours=1)),
    ], NOW)
    assert [(r["name"], r["status"]) for r in rows] == [
        ("yahoo", sh.OK), ("alpaca", sh.WARN), ("old", sh.WARN)]


def test_quote_freshness_counts_only_open_markets():
    rows = sh.quote_freshness([
        {"symbol": "AAPL", "asset_class": "us_equity", "open": True, "age_s": 10,
         "threshold_s": 120},
        {"symbol": "MSFT", "asset_class": "us_equity", "open": True, "age_s": 500,
         "threshold_s": 120},
        {"symbol": "NVDA", "asset_class": "us_equity", "open": True, "age_s": None,
         "threshold_s": 120},
        {"symbol": "ENI", "asset_class": "eu_equity", "open": False, "age_s": 90000,
         "threshold_s": 1200},
    ])
    assert rows[0]["asset_class"] == "eu_equity" and rows[0]["closed"] == 1
    us = rows[1]
    assert (us["symbols"], us["fresh"], us["stale"], us["stale_symbols"]) == (
        3, 1, 2, ["MSFT", "NVDA"])


def test_paperclip_reader_section():
    assert sh.paperclip_reader_section(False, {}, NOW)["status"] == sh.OFF
    ok = {"consecutive_errors": 0, "last_ok_at": (NOW - timedelta(minutes=4)).isoformat(),
          "isolation": "ok"}
    assert sh.paperclip_reader_section(True, ok, NOW)["status"] == sh.OK
    bad = {**ok, "consecutive_errors": 3, "last_error": "timeout"}
    s = sh.paperclip_reader_section(True, bad, NOW)
    assert s["status"] == sh.BAD and "timeout" in s["reasons"][0]
    late = {**ok, "last_ok_at": (NOW - timedelta(hours=1)).isoformat()}
    assert sh.paperclip_reader_section(True, late, NOW)["status"] == sh.WARN
    changed = {**ok, "isolation": "changed"}
    assert sh.paperclip_reader_section(True, changed, NOW)["status"] == sh.BAD


def test_cycles_and_watchdog_sections():
    view = {"expected": 4, "done": 3, "day_total": 9, "missing": 1, "read_at": "x",
            "error": None, "invalid_cron": [], "routines": []}
    s = sh.cycles_section(True, view)
    assert s["status"] == sh.WARN
    assert s["summary"] == "3 cicli partiti su 4 attesi finora (9 nella giornata)"
    assert sh.cycles_section(True, {**view, "missing": 0, "done": 4})["status"] == sh.OK
    assert sh.watchdog_section(True, [])["status"] == sh.OK
    w = sh.watchdog_section(True, [{"at": None, "text": "⚠ Passaggio mancante: AGENDA"}])
    assert w["status"] == sh.WARN and w["summary"] == "1 passaggi mancanti oggi"
    assert sh.watchdog_section(False, [])["status"] == sh.OFF


def test_outbox_section():
    assert sh.outbox_section(False, False, 0, None, 0, NOW)["status"] == sh.OFF
    assert sh.outbox_section(True, False, 0, None, 0, NOW)["status"] == sh.OK
    s = sh.outbox_section(True, False, 2, NOW - timedelta(minutes=20), 0, NOW)
    assert s["status"] == sh.WARN and s["oldest_pending_age_s"] == 1200
    s = sh.outbox_section(True, False, 2, NOW - timedelta(hours=2), 1, NOW)
    assert s["status"] == sh.BAD and len(s["reasons"]) == 2


def test_costs_section_reuses_the_budget_summary():
    today = NOW.date()
    month = budget.summary(today, Decimal(60), budget.Budget(Decimal(100), "settings"))
    s = sh.costs_section(Decimal("3.456"), month)
    # 60 $ in 19 days → 94.74 $ projected: within the budget
    assert s["status"] == sh.OK and s["today_usd"] == Decimal("3.46")
    assert s["summary"] == "Oggi 3.46 $, proiezione del mese 94.74 $"
    month = budget.summary(today, Decimal(80), budget.Budget(Decimal(100), "settings"))
    assert sh.costs_section(Decimal(1), month)["status"] == sh.WARN
    month = budget.summary(today, Decimal(100), budget.Budget(Decimal(100), "settings"))
    assert sh.costs_section(Decimal(1), month)["status"] == sh.BAD
    no_budget = budget.summary(today, Decimal(100), budget.Budget(None, None))
    assert sh.costs_section(Decimal(1), no_budget)["status"] == sh.OK


def test_mcp_section():
    assert sh.mcp_section([])["summary"] == "Nessuna chiamata nell'ultima ora"
    s = sh.mcp_section(["ok"] * 5 + ["INVALID_ORDER", "INTERNAL"])
    assert s["status"] == sh.WARN and s["internal"] == 1 and s["errors"] == 2
    # agent-side errors alone matter only when most of the calls fail
    assert sh.mcp_section(["ok"] * 9 + ["STALE_QUOTE"])["status"] == sh.OK
    assert sh.mcp_section(["ok"] * 4 + ["STALE_QUOTE"] * 6)["status"] == sh.WARN


def test_version_info(monkeypatch):
    monkeypatch.setenv("SMT_BUILD_ID", "abc123")
    monkeypatch.setenv("SMT_BUILT_AT", "2026-11-19T09:00:00+01:00")
    v = sh.version_info(None)
    assert v["build"] == "abc123" and v["built_at"] == "2026-11-19T09:00:00+01:00"
    assert v["api_started_at"] is not None
    monkeypatch.delenv("SMT_BUILD_ID")
    monkeypatch.setenv("SMTRADING_VERSION", "v1.2")
    assert sh.version_info(None)["build"] == "v1.2"


def test_intraday_job_late_shows_late():
    now = 100_000.0
    jobs = {"intraday": {"state": "running", "started_at": now - 5000,
                         "last_tick_at": now - 300}}
    [row] = sh.job_rows(jobs, now - 5000, now)
    assert row["interval_s"] == 15
    assert row["status"] == sh.WARN and row["note"] == "ultimo giro 5 min fa"
    ok = {"intraday": {"state": "running", "started_at": now - 5000,
                       "last_tick_at": now - 10}}
    assert sh.job_rows(ok, now - 5000, now)[0]["status"] == sh.OK
    # gated: waiting for the startup gate is not late
    waiting = {"intraday": {"state": "running", "started_at": now - 600, "last_tick_at": None}}
    assert sh.job_rows(waiting, now - 600, now)[0]["status"] == sh.OK


def test_scalper_stopped_with_active_rules_is_a_warning():
    now = 100_000.0
    jobs = {"scalper": {"state": "stopped", "started_at": now - 5000,
                        "last_tick_at": now - 4000}}
    [row] = sh.job_rows(jobs, now - 5000, now, active_auto_rules=2)
    assert row["status"] == sh.WARN
    assert row["note"] == "fermato con 2 regole automatiche attive"
    [row] = sh.job_rows(jobs, now - 5000, now)  # no active rule: stopped on purpose
    assert (row["status"], row["note"]) == (sh.OK, "fermato")


async def test_services_reads_the_active_rules_only_when_the_scalper_is_stopped():
    now = time.time()
    asked: list[int] = []

    async def active_rules():
        asked.append(1)
        return 1

    running = {WORKER_FIELD: {"started_at": now - 3600},
               "scalper": {"state": "running", "started_at": now - 3600,
                           "last_tick_at": now - 5}}
    s = await sh.services_section(FakeRedis(now - 5, running), now, active_rules)
    assert s["status"] == sh.OK and asked == []
    stopped = {**running, "scalper": {"state": "stopped", "started_at": now - 3600,
                                      "last_tick_at": now - 600}}
    s = await sh.services_section(FakeRedis(now - 5, stopped), now, active_rules)
    assert s["status"] == sh.WARN and asked == [1]
    assert "scalper: fermato con 1 regola automatica attiva" in s["reasons"]

    async def none_active():
        return 0

    s = await sh.services_section(FakeRedis(now - 5, stopped), now, none_active)
    assert s["status"] == sh.OK


async def test_services_without_intraday_rows_no_alarm():
    """Everything off: no intraday or scalper row yet, nothing flagged."""
    now = time.time()
    s = await sh.services_section(FakeRedis(now - 5, {
        WORKER_FIELD: {"started_at": now - 3600},
        "health checks": {"state": "running", "started_at": now - 3600,
                          "last_tick_at": now - 10}}), now)
    assert s["status"] == sh.OK and s["reasons"] == []
