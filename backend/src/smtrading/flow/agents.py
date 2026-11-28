"""«Fiume dei flussi» v2: the agents' runs and costs, read from Paperclip.

The same access as SMTrading's Paperclip reader (`PaperclipClient`: GET only, the board
key server side, never in an answer), cached CACHE_TTL_S per configuration so an open page
never weighs on Paperclip; a failed or slow read (PAPERCLIP_TIMEOUT_S) is cached too and
answers «costi non disponibili» (the page keeps working). No transaction is open during
the request (R9).

    runs    every heartbeat run of an agent with a node (Paperclip agent matched to an
            SMTrading agent: `agent_map`, else the same name) as an impulse Paperclip →
            agent: {id: "run:<id>", ts (start), kind "run", who "pc", path ["pc",
            "agent:<id>"], hops [reason in Italian], outcome ok|error, latency_ms (run
            time), symbol null, reason, run_outcome}
    agents  per SMTrading agent: Paperclip status, today's billed spend (the runs of the
            local day: metered API plus other, not the subscription; when the runs read
            do not reach back to midnight, the reader's `agent_cost_days` row), the month's
            spend (`spentMonthlyCents` of the Paperclip agent, else the reader's month) and
            the monthly cap (`budgetMonthlyCents` > 0), the last runs
"""

import logging
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from smtrading.common.config import get_settings
from smtrading.flow.cache import TtlCache
from smtrading.flow.map import agent_key
from smtrading.paperclip import budget
from smtrading.paperclip.client import PaperclipClient, PaperclipError
from smtrading.paperclip.config import PaperclipConfig, load_config, match_agent, smt_agents
from smtrading.paperclip.costs import _amount, billed_usd, bucket, run_time
from smtrading.paperclip.models import AgentCostDay

log = logging.getLogger(__name__)

CACHE_TTL_S = 30
PAPERCLIP_TIMEOUT_S = 5.0
RUNS_LIMIT = 500
LAST_RUNS = 10
UNAVAILABLE = "costi non disponibili"
CENT = Decimal("0.01")

REASON_IT = {"timer": "programmata", "task": "task", "mention": "menzione",
             "on_demand": "a richiesta", "automation": "automatica", "other": "risveglio"}
RUN_OUTCOME_IT = {"succeeded": "riuscita", "failed": "errore", "error": "errore",
                  "timed_out": "errore", "cancelled": "annullata", "canceled": "annullata",
                  "queued": "in coda", "running": "in corso"}
RUN_ERRORS = frozenset({"failed", "error", "timed_out"})
AGENT_STATUS_IT = {"idle": "in attesa", "active": "attivo", "running": "al lavoro",
                   "paused": "in pausa", "error": "in errore",
                   "pending_approval": "in attesa di approvazione",
                   "terminated": "archiviato", "archived": "archiviato"}

_snapshots = TtlCache(CACHE_TTL_S)


def _client(cfg: PaperclipConfig) -> PaperclipClient:
    return PaperclipClient(cfg, timeout=PAPERCLIP_TIMEOUT_S)


def _off_reason(cfg: PaperclipConfig) -> str | None:
    if not cfg.enabled:
        return "lettura di Paperclip disattivata"
    if not cfg.complete:
        return "Paperclip non configurato"
    if cfg.unreadable:
        return "chiave di Paperclip illeggibile"
    return None


async def _fetch(cfg: PaperclipConfig) -> dict[str, Any]:
    now = datetime.now(UTC)
    try:
        async with _client(cfg) as client:
            agents = await client.agents()
            runs = await client.heartbeat_runs(RUNS_LIMIT)
    except PaperclipError as e:
        return {"ok": False, "error": f"Paperclip {e.message}", "agents": [], "runs": [],
                "read_at": now}
    except Exception:
        log.warning("flow: paperclip read failed", exc_info=True)
        return {"ok": False, "error": "Paperclip: errore di lettura", "agents": [],
                "runs": [], "read_at": now}
    return {"ok": True, "error": None, "agents": agents, "runs": runs, "read_at": now}


async def paperclip_snapshot(cfg: PaperclipConfig) -> dict[str, Any]:
    """{ok, error (Italian, never the key), agents, runs, read_at}, cached."""
    off = _off_reason(cfg)
    if off is not None:
        return {"ok": False, "error": off, "agents": [], "runs": [], "read_at": None}
    key = (str(cfg.base_url), str(cfg.company_id), cfg.board_key)
    return await _snapshots.get(key, lambda: _fetch(cfg))


# -- runs ---------------------------------------------------------------------------------

def run_reason(run: dict) -> str:
    """timer | task | mention | on_demand | automation | other."""
    ctx = run.get("contextSnapshot") if isinstance(run.get("contextSnapshot"), dict) else {}
    wake = str(ctx.get("wakeReason") or run.get("wakeReason") or "").lower()
    if "mention" in wake:
        return "mention"
    if "assign" in wake or wake.startswith("issue"):
        return "task"
    source = str(run.get("invocationSource") or "").lower()
    return {"timer": "timer", "assignment": "task", "on_demand": "on_demand",
            "automation": "automation"}.get(source, "timer" if "timer" in wake else "other")


def _ts(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        ts = datetime.fromisoformat(value)
    except ValueError:
        return None
    return ts if ts.tzinfo else None


def run_event(run: dict, node: str) -> dict | None:
    ts = run_time(run)
    if ts is None or not run.get("id"):
        return None
    status = str(run.get("status") or "").lower()
    start, end = _ts(run.get("startedAt")), _ts(run.get("finishedAt"))
    reason = run_reason(run)
    return {"id": f"run:{run['id']}", "ts": ts, "kind": "run", "who": "pc",
            "path": ["pc", node], "hops": [REASON_IT[reason]],
            "outcome": "error" if status in RUN_ERRORS else "ok",
            "latency_ms": (round((end - start).total_seconds() * 1000)
                           if start and end and end >= start else None),
            "symbol": None, "reason": reason,
            "run_outcome": RUN_OUTCOME_IT.get(status, status or "sconosciuto")}


def _billed(run: dict) -> Decimal:
    usage = run.get("usageJson") if isinstance(run.get("usageJson"), dict) else {}
    amount = billed_usd(usage)
    if amount is None or bucket(usage.get("billingType")) == "subscription":
        return Decimal(0)
    return amount


def _usd(value: Decimal | None) -> Decimal | None:
    return value.quantize(CENT) if value is not None else None


def _cents_usd(value: Any) -> Decimal | None:
    cents = _amount(value)
    return (cents / 100).quantize(CENT) if cents is not None else None


# -- the route ----------------------------------------------------------------------------

async def _reader_costs(session: AsyncSession, today) -> tuple[dict[str, Decimal],
                                                                 dict[str, Decimal]]:
    """The reader's billed spend per Paperclip agent: today and month to date."""
    billed = AgentCostDay.api_usd + AgentCostDay.other_usd
    day = dict((await session.execute(
        select(AgentCostDay.paperclip_agent_id, func.sum(billed))
        .where(AgentCostDay.day == today)
        .group_by(AgentCostDay.paperclip_agent_id))).all())
    month = {pid: usd for pid, _, usd in await budget.month_spend(session, today)}
    return day, month


async def flow_agents(session: AsyncSession, *, now: datetime | None = None) -> dict:
    """{available, note, read_at, currency, agents: [...], runs: [...]} (module docstring)."""
    now = now or datetime.now(UTC)
    zone = ZoneInfo(get_settings().tz)
    today = now.astimezone(zone).date()
    cfg = await load_config(session)
    agents = await smt_agents(session)
    await session.rollback()  # no transaction open during the request (R9)
    snap = await paperclip_snapshot(cfg)

    remote: dict[str, dict] = {}  # SMTrading agent id -> Paperclip agent
    for a in snap["agents"]:
        pid = str(a.get("id") or "")
        smt = match_agent(pid, a.get("name"), agents, cfg.agent_map) if pid else None
        if smt is not None:
            remote.setdefault(str(smt.id), a)
    node_of = {str(a.get("id")): agent_key(smt_id) for smt_id, a in remote.items()}

    runs = [r for r in snap["runs"] if isinstance(r, dict)]
    events = [e for r in runs if str(r.get("agentId")) in node_of
              if (e := run_event(r, node_of[str(r.get("agentId"))])) is not None]
    events.sort(key=lambda e: (e["ts"], e["id"]))
    times = [t for r in runs if (t := run_time(r)) is not None]
    reaches_midnight = (len(runs) < RUNS_LIMIT
                        or (times and min(times).astimezone(zone).date() < today))
    day_db, month_db = await _reader_costs(session, today) if snap["ok"] else ({}, {})

    rows = []
    for smt in agents:
        a = remote.get(str(smt.id))
        node = agent_key(smt.id)
        pid = str(a.get("id")) if a else None
        mine = [e for e in events if e["path"][1] == node]
        today_usd = month_usd = cap_usd = None
        if snap["ok"] and a is not None:
            if reaches_midnight:
                today_usd = sum((_billed(r) for r in runs if str(r.get("agentId")) == pid
                                 and (t := run_time(r)) is not None
                                 and t.astimezone(zone).date() == today), start=Decimal(0))
            else:
                today_usd = Decimal(day_db.get(pid) or 0)
            month_usd = _cents_usd(a.get("spentMonthlyCents"))
            if month_usd is None:
                month_usd = Decimal(month_db.get(pid) or 0)
            cap = _cents_usd(a.get("budgetMonthlyCents"))
            cap_usd = cap if cap else None
        status = (str(a.get("status") or "").lower() or None) if a else None
        rows.append({
            "key": node, "name": smt.name, "paperclip_id": pid, "status": status,
            "status_it": AGENT_STATUS_IT.get(status, status) if status else None,
            "today_usd": _usd(today_usd), "month_usd": _usd(month_usd),
            "cap_usd": _usd(cap_usd),
            "runs": [{"id": e["id"].removeprefix("run:"), "ts": e["ts"],
                      "reason": e["reason"], "reason_it": e["hops"][0],
                      "outcome": e["run_outcome"], "latency_ms": e["latency_ms"]}
                     for e in reversed(mine[-LAST_RUNS:])],
        })
    return {"available": snap["ok"],
            "note": None if snap["ok"] else f"{snap['error']}: {UNAVAILABLE}",
            "read_at": snap["read_at"], "currency": "USD", "agents": rows, "runs": events}
