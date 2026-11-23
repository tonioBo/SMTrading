"""The sentinel's checks (pure): each turns what was read into `Finding`s. The thresholds
are the module constants; the job (`sentinel.job`) does the reading."""

import re
from collections import Counter, defaultdict
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from smtrading.common.logsetup import redact_log_text
from smtrading.paperclip import cycles as paperclip_cycles
from smtrading.paperclip.issues import CLOSED_STATUSES
from smtrading.sentinel.engine import Finding

# 1. «Salute del sistema»
HEALTH_WARN_AFTER_S = 30 * 60  # «attenzione» is told when it lasts this long
HEALTH_MAX_REASONS = 8
# 2. Paperclip agents, runs and issues
AGENT_BAD_AFTER_S = 10 * 60
AGENT_OK_STATUSES = frozenset({"active", "idle", "running", "pending_approval"})
AGENT_GONE_STATUSES = frozenset({"terminated", "archived", "deleted"})
RUN_FAILED_STATUSES = {"failed": "fallito", "error": "errore", "timed_out": "tempo scaduto"}
RUN_WINDOW = timedelta(minutes=30)  # a failed run is told when it ended this recently
BLOCKED_AFTER_S = 30 * 60
# 3. missed cycles
CYCLE_GRACE = timedelta(minutes=20)  # an execution may start this late
CYCLE_TOLERANCE = timedelta(minutes=2)  # … or this early (clocks)
CYCLE_LOOKBACK = timedelta(hours=3)
CYCLES_FRESH = timedelta(minutes=15)  # older routines/executions: not judged
# 4. MCP
MCP_WINDOW = timedelta(minutes=30)
MCP_REPEATED = 5  # non-ok outcomes of one agent on one tool in MCP_WINDOW
# 5. error logs
ERRLOG_THRESHOLD = 5  # ERROR/CRITICAL records of one logger in errorlog.WINDOW_S
# 6. identities
IDENTITY_BURST = 4  # distinct agents calling MCP within one minute
SECURITY_WINDOW = timedelta(minutes=30)
# 7. leaks: global activity types meant for every agent
GLOBAL_TYPES = frozenset({"kill_switch", "halt.set", "halt.cleared", "instrument.added"})
LEAK_WINDOW = timedelta(minutes=30)

MAX_ERROR = 200


def _ts(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        ts = datetime.fromisoformat(value)
    except ValueError:
        return None
    return ts if ts.tzinfo else ts.replace(tzinfo=UTC)


def _short(text: Any, limit: int = MAX_ERROR) -> str:
    text = " ".join(redact_log_text(str(text or "")).split())
    return text if len(text) <= limit else text[:limit - 1] + "…"


def _local(t: datetime, tz: str) -> str:
    return t.astimezone(ZoneInfo(tz)).strftime("%H:%M")


# the health reason of a provider demoted for its quota (admin/system_health.py)
QUOTA_REASON_SUFFIX = "usato per ultimo fino al reset"

# -- 1. «Salute del sistema» ----------------------------------------------------------------

def health_findings(view: dict[str, Any]) -> list[Finding]:
    status = view.get("status")
    if status not in ("problema", "attenzione"):
        return []
    reasons = [str(r) for r in view.get("reasons") or []]
    if status == "attenzione":
        # a provider over its quota is demoted and the others serve: nothing to act on
        reasons = [r for r in reasons if not r.endswith(QUOTA_REASON_SUFFIX)]
        if not reasons:
            return []
    lines = [f"• {r}" for r in reasons[:HEALTH_MAX_REASONS]]
    if len(reasons) > HEALTH_MAX_REASONS:
        lines.append(f"… e altri {len(reasons) - HEALTH_MAX_REASONS}")
    bad = status == "problema"
    return [Finding(key="health", kind="health", title=f"Salute del sistema in «{status}»",
                    detail="\n".join(lines), level="critical" if bad else "warning",
                    after_s=0 if bad else HEALTH_WARN_AFTER_S, signature=status)]


# -- 2. Paperclip agents, runs, issues --------------------------------------------------------

def _run_end(run: dict) -> datetime | None:
    for key in ("finishedAt", "endedAt", "updatedAt", "startedAt", "createdAt"):
        if (ts := _ts(run.get(key))) is not None:
            return ts
    return None


def _run_error(run: dict) -> str:
    return _short(run.get("errorMessage") or run.get("error") or run.get("errorCode") or "")


def agent_findings(agents: list[dict], runs: list[dict], now: datetime) -> list[Finding]:
    names = {str(a.get("id")): str(a.get("name") or a.get("id")) for a in agents if a.get("id")}
    failed = [r for r in runs if str(r.get("status") or "").lower() in RUN_FAILED_STATUSES]
    failed.sort(key=lambda r: _run_end(r) or datetime.min.replace(tzinfo=UTC))
    last_error = {str(r.get("agentId")): _run_error(r) for r in failed if _run_error(r)}
    out: list[Finding] = []
    for a in agents:
        aid = str(a.get("id") or "")
        status = str(a.get("status") or "").lower() or "unknown"
        if not aid or status in AGENT_OK_STATUSES or status in AGENT_GONE_STATUSES:
            continue
        label = {"error": "in errore", "paused": "in pausa"}.get(status, "in stato sconosciuto")
        detail = _short(a.get("lastError") or a.get("errorMessage") or a.get("pauseReason")
                        or last_error.get(aid) or "")
        if status not in ("error", "paused", "unknown"):
            detail = f"stato «{_short(status, 40)}»" + (f": {detail}" if detail else "")
        out.append(Finding(key=f"agent:{aid}", kind="agent",
                           title=f"Agente {names.get(aid, aid)} {label}", detail=detail,
                           after_s=AGENT_BAD_AFTER_S, signature=status))
    for r in failed:
        end = _run_end(r)
        if not r.get("id") or end is None or end < now - RUN_WINDOW:
            continue
        agent = str(r.get("agentId") or "?")
        status = RUN_FAILED_STATUSES[str(r.get("status")).lower()]
        out.append(Finding(key=f"run:{r['id']}", kind="run", event=True,
                           title=f"Ciclo dell'agente {names.get(agent, agent)} non riuscito "
                                 f"({status})", detail=_run_error(r)))
    return out


def issue_findings(issues: list[dict]) -> list[Finding]:
    out = []
    for i in issues:
        if str(i.get("status") or "").lower() != "blocked" or not i.get("id"):
            continue
        ident = i.get("identifier") or i["id"]
        title = _short(i.get("title"), 100)
        out.append(Finding(key=f"issue:{i['id']}", kind="issue", after_s=BLOCKED_AFTER_S,
                           title=f"Task {ident} bloccato: «{title}»"))
    return out


# -- 3. missed cycles -------------------------------------------------------------------------

def _covered(fire: datetime, executions: list[dict]) -> bool:
    """An execution started for this fire, or one already open took it in (coalesced or
    skipped while active: no issue of its own)."""
    for e in executions:
        created = _ts(e.get("created_at"))
        if created is None:
            continue
        if fire - CYCLE_TOLERANCE <= created <= fire + CYCLE_GRACE:
            return True
        if created < fire:
            if str(e.get("status") or "").lower() not in CLOSED_STATUSES:
                return True  # still open now: open at the fire too
            closed = _ts(e.get("closed_at"))
            if closed is not None and closed >= fire - CYCLE_TOLERANCE:
                return True
    return False


def cycle_findings(state: dict[str, Any], now: datetime, tz: str,
                   names: dict[str, str]) -> list[Finding] | None:
    """The scheduled fires of the active routines from CYCLE_LOOKBACK ago up to the last
    read minus CYCLE_GRACE with no execution: one event per fire. None when the
    routines were not read recently (nothing can be judged)."""
    read_at = _ts(state.get("read_at"))
    if read_at is None or now - read_at > CYCLES_FRESH or state.get("error"):
        return None
    start = now - CYCLE_LOOKBACK
    end = min(read_at, now) - CYCLE_GRACE
    if end <= start:
        return []
    by_routine: dict[str | None, list[dict]] = defaultdict(list)
    for e in state.get("executions") or []:
        by_routine[e.get("routine_id")].append(e)
    out: list[Finding] = []
    for r in state.get("routines") or []:
        if r.get("status") != "active" or not r.get("id"):
            continue
        agent = names.get(r.get("agent_id") or "")
        who = f" ({agent})" if agent else ""
        for t in r.get("triggers") or []:
            if not t.get("enabled"):
                continue
            try:
                fires = paperclip_cycles.fires(
                    t["cron"], paperclip_cycles._zone(t.get("tz"), tz), start, end)
            except (paperclip_cycles.CronError, KeyError):
                continue
            for fire in fires:
                if _covered(fire, by_routine.get(r["id"], [])):
                    continue
                out.append(Finding(
                    key=f"cycle:{r['id']}:{fire.isoformat()}", kind="cycle", event=True,
                    title=f"Ciclo mancato: «{r.get('title') or r['id']}»{who} delle "
                          f"{_local(fire, tz)} non è partito",
                    detail=f"nessuna esecuzione entro {int(CYCLE_GRACE.total_seconds() // 60)} "
                           "min dall'orario previsto"))
    return sorted(out, key=lambda f: f.key)


# -- 4. MCP -----------------------------------------------------------------------------------

def mcp_findings(rows: list[tuple[Any, str | None, str, str]]) -> list[Finding]:
    """`rows`: (agent id, agent name, method, outcome) of the MCP calls of MCP_WINDOW."""
    errors: dict[tuple[str, str], Counter] = defaultdict(Counter)
    names: dict[str, str] = {}
    internal: Counter = Counter()
    for agent_id, name, method, outcome in rows:
        if outcome in ("ok", "OK"):
            continue
        aid = str(agent_id) if agent_id is not None else "?"
        names[aid] = name or aid
        errors[(aid, method)][outcome] += 1
        if outcome == "INTERNAL":
            internal[f"{names[aid]}: {method}"] += 1
    out: list[Finding] = []
    for (aid, method), codes in sorted(errors.items()):
        n = sum(codes.values())
        if n < MCP_REPEATED:
            continue
        out.append(Finding(
            key=f"mcp:{aid}:{method}", kind="mcp",
            title=f"{names[aid]}: {n} errori MCP su {method} in 30 min",
            detail=", ".join(f"{c} ×{k}" for c, k in
                             sorted(codes.items(), key=lambda kv: (-kv[1], kv[0])))))
    if internal:
        n = sum(internal.values())
        noun = "errore interno" if n == 1 else "errori interni"
        out.append(Finding(
            key="mcp:internal", kind="mcp", title=f"{n} {noun} MCP negli ultimi 30 min",
            detail=", ".join(f"{k} ×{v}" for k, v in internal.most_common(5))))
    return out


# -- 5. error logs ----------------------------------------------------------------------------

_PROCESS = {"worker": "del worker", "api": "dell'api"}


def errlog_findings(counts: dict[tuple[str, str], dict[str, Any]]) -> list[Finding]:
    out = []
    for (process, logger), c in sorted(counts.items()):
        n = int(c.get("count") or 0)
        if n < ERRLOG_THRESHOLD:
            continue
        where = _PROCESS.get(process, f"di {process}")
        message = _short(c.get("message"))
        out.append(Finding(key=f"errlog:{process}:{logger}", kind="errlog",
                           title=f"{n} errori nei log {where} ({logger}) in 15 min",
                           detail=f"Ultimo: {message}" if message else ""))
    return out


# -- 6. identities and decision authors --------------------------------------------------------

def identity_findings(rows: list[tuple[datetime, int]], tz: str) -> list[Finding]:
    """`rows`: (minute, distinct agents calling MCP in it)."""
    return [Finding(key=f"identity:{minute.isoformat()}", kind="identity", event=True,
                    level="critical",
                    title=f"{n} identità di agenti diverse in un solo minuto su MCP "
                          f"({_local(minute, tz)})",
                    detail="Controlla che ogni agente usi solo il suo token: più identità "
                           "dallo stesso processo indicano una connessione condivisa.")
            for minute, n in rows if n >= IDENTITY_BURST]


def author_mismatch(agent_name: str, by: str) -> bool:
    """`by` names someone else: neither the agent's first name nor its role initials
    («PM» for Portfolio Manager) appear in it."""
    words = agent_name.split()
    first = (words or [""])[0].lower()
    if not first or first in by.lower():
        return False
    initials = "".join(w[0] for w in words).lower()
    tokens = {t.lower() for t in re.findall(r"[A-Za-zÀ-ÿ]+", by)}
    return not (len(initials) >= 2 and initials in tokens)


def author_findings(rows: list[tuple[int, str, str]]) -> list[Finding]:
    """`rows`: (decision id, signing agent's name, details.by)."""
    return [Finding(key=f"author:{did}", kind="author", event=True, level="critical",
                    title=f"Decisione {did} firmata da {name} ma scritta «da» {_short(by, 80)}",
                    detail="Un agente ha registrato una decisione a nome di un altro: "
                           "controlla i token degli agenti.")
            for did, name, by in rows if by and author_mismatch(name, str(by))]


# -- 7. agent-visible leaks ---------------------------------------------------------------------

def leak_findings(rows: list[tuple[int, Any, str | None, str]]) -> list[Finding]:
    """`rows`: (activity id, portfolio id or None, portfolio name, type) of the new
    activity rows written for a portfolio no agent can access, or global rows with no
    agent visible to every agent."""
    groups: dict[str, list[tuple[int, str | None, str]]] = defaultdict(list)
    for aid, pid, name, type_ in rows:
        groups[str(pid) if pid is not None else "global"].append((aid, name, type_))
    out = []
    for key, items in sorted(groups.items()):
        n = len(items)
        types = ", ".join(sorted({t for _, _, t in items}))
        last = max(i for i, _, _ in items)
        if key == "global":
            title = (f"{n} {'riga' if n == 1 else 'righe'} del registro "
                     f"{'visibile' if n == 1 else 'visibili'} a tutti gli agenti "
                     "(senza portafoglio né agente)")
        else:
            name = items[0][1] or key
            title = (f"{n} {'riga' if n == 1 else 'righe'} nel registro degli agenti per "
                     f"«{name}», un portafoglio a cui nessun agente ha accesso")
        out.append(Finding(key=f"leak:{key}:{last}", kind="leak", event=True,
                           title=title, detail=f"Tipi: {types}"))
    return out
