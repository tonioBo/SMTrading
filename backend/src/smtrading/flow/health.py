"""«Fiume dei flussi» v2: the health of every node, `ok | attenzione | errore |
sconosciuto` with one plain Italian reason, from the health sources SMTrading already has
(cached HEALTH_TTL_S per api process):

    db        «Salute del sistema»: backup (the database answers: this read)
    mcp       «Salute del sistema»: MCP errors of the last hour
    eng, sim  the worker heartbeat and the «order processor» job
    rules     the «scalper» job (stopped with no active rule: ok)
    sync      the «broker sync» job
    tg        the Telegram outbox (stuck, failed, not configured)
    pc        the Paperclip reader state and the page's own Paperclip read
    agent:*   the agent's status in Paperclip (and its last run failed)
    px        the stream status (`md:stream_status`) and the providers' health checks
    ng        the last get_forecast that reached Neurograph (or failed reaching it) and
              the last nightly export
    broker:*  the links of that kind: status, ongoing outage, failures counted

«Salute del sistema» `problema` is `errore` here, `spento` is `sconosciuto`."""

import logging
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from smtrading.admin.system_health import system_health
from smtrading.agents.models import McpAuditLog
from smtrading.brokers.models import BrokerLink
from smtrading.common.config import get_settings
from smtrading.common.logsetup import redact_log_text
from smtrading.flow.agents import AGENT_STATUS_IT, RUN_ERRORS, paperclip_snapshot
from smtrading.flow.cache import TtlCache
from smtrading.flow.map import agent_key, broker_key, broker_label, flow_map, price_source_label
from smtrading.flow.sources import stream_status
from smtrading.neurograph.models import NeurographExportNight
from smtrading.paperclip.config import load_config, match_agent, smt_agents
from smtrading.paperclip.costs import run_time

log = logging.getLogger(__name__)

HEALTH_TTL_S = 15
OK, WARN, BAD, UNKNOWN = "ok", "attenzione", "errore", "sconosciuto"
_RANK = {OK: 0, UNKNOWN: 1, WARN: 2, BAD: 3}
_FROM_SYSTEM = {"ok": OK, "attenzione": WARN, "problema": BAD, "spento": UNKNOWN}
# get_forecast outcomes that say something about Neurograph (others are the agent's)
FORECAST_OUTCOMES = ("ok", "PROVIDER_UNAVAILABLE", "INTERNAL")
NG_WINDOW = timedelta(days=7)  # the forecast lookup stays on recent rows (index 0051)
NG_ERRORS_IN_A_ROW = 2
REASON_MAX = 160

_cache = TtlCache(HEALTH_TTL_S)


def _node(state: str, reason: str) -> dict[str, str]:
    reason = " ".join(redact_log_text(str(reason)).split())  # stored error texts: masked
    return {"state": state, "reason": reason[:REASON_MAX - 1] + "…"
            if len(reason) > REASON_MAX else reason}


def _worst(*nodes: dict[str, str]) -> dict[str, str]:
    return max(nodes, key=lambda n: _RANK[n["state"]])


def _hm(ts: datetime, tz: str) -> str:
    return ts.astimezone(ZoneInfo(tz)).strftime("%d/%m %H:%M")


def _section(sections: dict, key: str, ok_reason: str | None = None) -> dict[str, str]:
    s = sections.get(key) or {}
    state = _FROM_SYSTEM.get(s.get("status"), UNKNOWN)
    reason = (s.get("reasons") or [None])[0] or s.get("summary") or "nessun dato"
    if state == OK and ok_reason:
        reason = ok_reason
    return _node(state, reason)


def _job(sections: dict, name: str, label: str) -> dict[str, str]:
    services = sections.get("services") or {}
    row = next((j for j in services.get("jobs") or [] if j.get("name") == name), None)
    age = services.get("heartbeat_age_s")
    if services.get("status") == "problema" and (age is None or age > 60):
        return _node(BAD, (services.get("reasons") or ["worker fermo"])[0])
    if row is None:
        return _node(UNKNOWN, f"{label}: servizio non avviato")
    state = _FROM_SYSTEM.get(row["status"], UNKNOWN)
    if state == OK:
        return _node(OK, f"{label}: {row['note']}" if row.get("note") else f"{label} regolare")
    return _node(state, f"{label}: {row.get('note') or 'da controllare'}")


def _px(status: dict | None, sections: dict) -> dict[str, str]:
    providers = _section(sections, "providers")
    if status is None:
        return _worst(_node(UNKNOWN, "stato dei flussi prezzi non pubblicato (worker fermo?)"),
                      providers if providers["state"] == BAD else _node(OK, ""))
    streams = [(k.split(":", 1)[0], s) for k, s in sorted((status.get("streams") or {}).items())
               if isinstance(s, dict)]
    live = [p for p, s in streams if s.get("state") == "connected"]
    refused = [(p, s) for p, s in streams if s.get("state") == "backoff"]
    polled = len(status.get("polling") or [])
    if refused:
        p, s = refused[0]
        node = _node(WARN, f"{price_source_label(p)}: flusso rifiutato"
                     + (f" ({s['note']})" if s.get("note") else ""))
    elif streams and not live:
        node = _node(WARN, "flussi prezzi in collegamento")
    else:
        node = _node(OK, f"{len(live)} flussi in diretta, {polled} titoli a lettura periodica")
    return _worst(node, providers) if providers["state"] in (BAD, WARN) else node


async def _ng(session: AsyncSession, tz: str, *, now: datetime,
              neurograph_url: str | None) -> dict[str, str]:
    """Not configured (`SMT_NEUROGRAPH_URL` empty): «sconosciuto». Of the get_forecast
    calls of the last NG_WINDOW that say something about Neurograph, the latest: ok, or
    one refusal («attenzione»: PROVIDER_UNAVAILABLE also means too few bars), or
    NG_ERRORS_IN_A_ROW in a row («errore»); a failed nightly export is «errore» too."""
    if not neurograph_url:
        return _node(UNKNOWN, "Neurograph non configurato")
    since = now - NG_WINDOW
    calls = (await session.execute(
        select(McpAuditLog.outcome, McpAuditLog.created_at)
        .where(McpAuditLog.method == "get_forecast", McpAuditLog.created_at > since,
               McpAuditLog.created_at <= now, McpAuditLog.outcome.in_(FORECAST_OUTCOMES))
        .order_by(McpAuditLog.created_at.desc()).limit(NG_ERRORS_IN_A_ROW))).all()
    night = (await session.execute(
        select(NeurographExportNight.outcome, NeurographExportNight.night)
        .where(NeurographExportNight.night >= since.date())
        .order_by(NeurographExportNight.night.desc()).limit(1))).first()
    nodes = []
    if calls:
        last = calls[0]
        failed = 0
        for c in calls:
            if c.outcome == "ok":
                break
            failed += 1
        when = _hm(last.created_at, tz)
        if failed == 0:
            nodes.append(_node(OK, f"ultima previsione riuscita il {when}"))
        elif failed >= NG_ERRORS_IN_A_ROW:
            nodes.append(_node(BAD, f"ultime {failed} previsioni non riuscite (l'ultima il "
                                    f"{when})"))
        else:
            nodes.append(_node(WARN, f"ultima previsione non riuscita il {when}: può dipendere "
                                     "da barre insufficienti"))
    if night is not None:
        day = night.night.strftime("%d/%m")
        nodes.append({"ok": _node(OK, f"invio notturno riuscito ({day})"),
                      "outage": _node(BAD, f"invio notturno non riuscito: Neurograph non "
                                           f"raggiungibile ({day})"),
                      "unauthorized": _node(BAD, f"invio notturno non riuscito: chiave "
                                                 f"rifiutata ({day})")
                      }.get(night.outcome, _node(UNKNOWN, f"invio notturno: {night.outcome}")))
    if not nodes:
        return _node(UNKNOWN, "nessuna previsione né invio notturno negli ultimi 7 giorni")
    worst = _worst(*nodes)
    if worst["state"] == OK and len(nodes) == 2:
        return _node(OK, f"{nodes[0]['reason']}; {nodes[1]['reason']}")
    return worst


def _safe(text: str | None) -> str | None:
    """An error text stored by a connector or a broker link, with secrets masked."""
    return redact_log_text(text) if text else text


def _broker(links: list, now: datetime) -> dict[str, str]:
    on = [b for b in links if b.enabled]
    if not on:
        return _node(UNKNOWN, "collegamento disattivato")
    nodes = []
    for b in on:
        if b.outage_since is not None and b.outage_until is None:
            minutes = max(int((now - b.outage_since).total_seconds() // 60), 0)
            nodes.append(_node(BAD, f"«{b.name}» non risponde da {minutes} min"))
        elif b.status == "error":
            nodes.append(_node(BAD, f"«{b.name}»: {_safe(b.last_error) or 'in errore'}"))
        elif b.health_failures:
            nodes.append(_node(WARN, f"«{b.name}»: {b.health_failures} chiamate senza "
                                     "risposta"))
        elif b.status == "unknown":
            nodes.append(_node(UNKNOWN, f"«{b.name}»: mai verificato"))
        else:
            nodes.append(_node(OK, f"«{b.name}» collegato"))
    return _worst(*nodes)


def _agents(snap: dict, agents, cfg, now: datetime) -> dict[str, dict[str, str]]:
    out = {agent_key(a.id): _node(UNKNOWN, snap["error"] or "nessun dato") for a in agents}
    if not snap["ok"]:
        return out
    last_run: dict[str, dict] = {}
    for r in snap["runs"]:
        if isinstance(r, dict) and (t := run_time(r)) is not None:
            aid = str(r.get("agentId"))
            if aid not in last_run or t > run_time(last_run[aid]):
                last_run[aid] = r
    for a in snap["agents"]:
        pid = str(a.get("id") or "")
        smt = match_agent(pid, a.get("name"), agents, cfg.agent_map) if pid else None
        if smt is None:
            continue
        status = str(a.get("status") or "").lower()
        label = AGENT_STATUS_IT.get(status, status or "stato sconosciuto")
        if status in ("idle", "active", "running", "pending_approval"):
            run = last_run.get(pid)
            if run is not None and str(run.get("status") or "").lower() in RUN_ERRORS:
                node = _node(WARN, f"{label}; ultima esecuzione non riuscita")
            else:
                node = _node(OK, label)
        elif status == "paused":
            node = _node(WARN, label)
        elif status == "error":
            node = _node(BAD, label)
        else:
            node = _node(UNKNOWN, label)
        out[agent_key(smt.id)] = node
    for key, node in out.items():
        if node["reason"] == "nessun dato":
            out[key] = _node(UNKNOWN, "non trovato in Paperclip")
    return out


def _db(backup: dict[str, str]) -> dict[str, str]:
    """The database answers (this read); an old or failed backup is «attenzione»."""
    if backup["state"] in (WARN, BAD):
        return _node(WARN, backup["reason"])
    return _node(OK, "il database risponde, backup regolare" if backup["state"] == OK
                 else "il database risponde")


async def _build(session: AsyncSession, redis, now: datetime) -> dict[str, Any]:
    tz = get_settings().tz
    fmap = await flow_map(session)
    cfg = await load_config(session)
    agents = await smt_agents(session)
    links = (await session.execute(  # plain rows: they outlive the rollback below
        select(BrokerLink.name, BrokerLink.kind, BrokerLink.enabled, BrokerLink.status,
               BrokerLink.last_error, BrokerLink.health_failures, BrokerLink.outage_since,
               BrokerLink.outage_until))).all()
    ng = await _ng(session, tz, now=now, neurograph_url=get_settings().neurograph_url)
    await session.rollback()  # no transaction open during the Paperclip read (R9)
    snap = await paperclip_snapshot(cfg)
    try:
        sections = (await system_health(session, redis, now=now))["sections"]
    except Exception:
        log.warning("flow: system health unreadable", exc_info=True)
        await session.rollback()
        sections = {}
    status = await stream_status(redis)

    pc = _section(sections, "paperclip", "lettura regolare")
    if snap["ok"] is False and cfg.enabled:
        pc = _worst(pc, _node(WARN, snap["error"]))
    eng = _job(sections, "order processor", "motore ordini")
    nodes: dict[str, dict[str, str]] = {
        "db": _db(_section(sections, "backup")),
        "mcp": _section(sections, "mcp"),
        "eng": eng, "sim": eng,
        "rules": _job(sections, "scalper", "regole automatiche"),
        "sync": _job(sections, "broker sync", "BrokerSync"),
        "tg": _section(sections, "telegram"),
        "pc": pc,
        "px": _px(status, sections),
        "ng": ng,
        **_agents(snap, agents, cfg, now),
    }
    by_kind: dict[str, list] = {}
    for b in links:
        by_kind.setdefault(b.kind, []).append(b)
    for kind, group in by_kind.items():
        nodes[broker_key(kind)] = _broker(group, now)
    out = {}
    for n in fmap["nodes"]:
        out[n["key"]] = nodes.get(n["key"]) or _node(UNKNOWN, "nessun dato")
        if n["key"].startswith("broker:") and n["key"] not in nodes:
            out[n["key"]] = _node(UNKNOWN, f"{broker_label(n['key'][7:])}: nessun collegamento")
    return {"checked_at": now, "nodes": out}


async def flow_health(session: AsyncSession, redis, *, now: datetime | None = None
                      ) -> dict[str, Any]:
    """{checked_at, nodes: {key: {state, reason}}}: every node of the map, cached."""
    return await _cache.get("health", lambda: _build(session, redis, now or datetime.now(UTC)))
