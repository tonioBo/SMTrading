"""«Salute del sistema»: one read-only view of what keeps SMTrading running, with an
overall status (`ok` | `attenzione` | `problema`) and its reasons.

Every section is built from data the system already keeps, nothing is called outside:
- services: the api (answering this request: build, start), the worker heartbeat
  (Redis) and every supervised worker job (`worker.jobstate`: start, crashes, last pass);
- market data providers: the connectors' last health check (`connectors`, written every
  5 minutes by the worker's health checks);
- quote freshness: the cached quote (Redis) of every open position's and watchlist's
  symbol against the «quota vecchia» threshold of its asset class, only while its market
  is open (a closed market's quote is old by design);
- price quality: the quote quality guard's counters (marketdata.quality, Redis): suspect
  quotes of the last hour and 24 h, the top symbols, the classifications by reason and
  provider, the symbols whose quotes are unconfirmed right now;
- provider quotas: per enabled provider, the calls of this minute, hour and UTC day
  against its budgets (marketdata.quota), and whether it is demoted;
- Paperclip: the reader state (last read, errors, isolation), the agent cycles of the
  day (`paperclip.cycles`) and the hand-off watchdog warnings of the day;
- Telegram: the outbox (pending, failed in the last 24 h, oldest pending);
- agent costs: today's billed spend and the month projection (`paperclip.budget`);
- MCP: the calls of the last hour that ended in an error (internal errors apart);
- backup: the record of the `backup` service (`backup.state`): the last fully ok backup
  (older than 26 h: problema), the last run and the last restore test (failed or older
  than 8 days: attenzione);
- version: the build id baked into the image (SMT_BUILD_ID, else SMTRADING_VERSION), the
  build time (SMT_BUILT_AT) and the api and worker start (the last deploy).

A section that cannot be read (Redis or a table) is `attenzione` with the reason, never
a failed page. The pure functions take plain values (tests use fakes)."""

import logging
import os
import time
from collections import Counter
from collections.abc import Callable, Iterable
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from smtrading.agents.models import McpAuditLog
from smtrading.backup import service as backup_service
from smtrading.common.config import get_settings
from smtrading.common.heartbeat import heartbeat_age_s
from smtrading.connectors.models import Connector
from smtrading.instruments.market_hours import market_status
from smtrading.instruments.models import Instrument
from smtrading.marketdata import quality, quota
from smtrading.marketdata.cache import read_quote_meta
from smtrading.marketdata.registry import PROVIDERS
from smtrading.paperclip import budget, cycles, model_costs
from smtrading.paperclip.config import load_config as load_paperclip_config
from smtrading.paperclip.config import load_state as load_paperclip_state
from smtrading.paperclip.models import AgentCostDay
from smtrading.paperclip.watchdog import PREFIX as WATCHDOG_PREFIX
from smtrading.paperclip.watchdog import STATE_KEY as WATCHDOG_STATE_KEY
from smtrading.paperclip.watchdog_settings import watchdog_on
from smtrading.portfolios.models import Portfolio, PortfolioStatus
from smtrading.positions.models import Position
from smtrading.risk.models import AppSetting
from smtrading.risk.settings import get_stale_threshold
from smtrading.telegram.config import is_ready
from smtrading.telegram.config import load_config as load_telegram_config
from smtrading.telegram.models import OutboxStatus, TelegramOutbox
from smtrading.watch.models import WatchlistItem
from smtrading.worker.jobstate import read_jobs

log = logging.getLogger(__name__)

OK, WARN, BAD, OFF = "ok", "attenzione", "problema", "spento"
_RANK = {OK: 0, OFF: 0, WARN: 1, BAD: 2}

API_STARTED_AT = time.time()  # this process (the api container) started: the last deploy

HEARTBEAT_WARN_S, HEARTBEAT_BAD_S = 30, 60
RECENT_CRASH_S = 15 * 60
# jobs that call job_tick() once per pass: their expected pass interval (s)
JOB_INTERVALS: dict[str, float] = {
    "health checks": 300, "job scheduler": 30, "paperclip reader": 300,
    "hand-off watchdog": 300, "protection alarm": 300, "goal alerts": 300,
    "telegram summaries": 30, "price quality alarm": 60,
}
LATE_FACTOR = 3  # a pass later than 3 intervals is late
GATED_JOBS = ("protection alarm", "goal alerts", "plan closer", "pre-open check",
              "hand-off watchdog", "weekly report")
GATE_GRACE_S = 15 * 60  # the gated jobs may wait for the startup gate this long
CONNECTOR_CHECK_LATE_S = 15 * 60
PAPERCLIP_LATE_S = 15 * 60
OUTBOX_WARN_S, OUTBOX_BAD_S = 10 * 60, 60 * 60
MCP_WINDOW = timedelta(hours=1)
MCP_ERROR_SHARE = Decimal("0.5")  # agent-side errors only matter when most calls fail
MCP_MIN_CALLS = 10
BACKUP_BAD_AGE_S = 26 * 3600
RESTORE_WARN_AGE_S = 8 * 86400


def worst(statuses: Iterable[str]) -> str:
    out = OK
    for s in statuses:
        if _RANK.get(s, 0) > _RANK[out]:
            out = s
    return out


def _section(status: str, summary: str, reasons: list[str] | None = None,
             **data: Any) -> dict[str, Any]:
    return {"status": status, "summary": summary, "reasons": reasons or [], **data}


def _unreadable(what: str, exc: BaseException) -> dict[str, Any]:
    log.warning("system health: %s unreadable", what, exc_info=exc)
    return _section(WARN, f"{what}: dati non leggibili", [f"{what}: dati non leggibili"])


def _iso(ts: float | None) -> str | None:
    return datetime.fromtimestamp(ts, UTC).isoformat() if ts else None


def _ago(seconds: float) -> str:
    seconds = max(int(seconds), 0)
    if seconds < 90:
        return f"{seconds} s"
    if seconds < 90 * 60:
        return f"{round(seconds / 60)} min"
    return f"{seconds / 3600:.1f} h".replace(".", ",")


# -- services ---------------------------------------------------------------------------

def job_rows(jobs: dict[str, dict[str, Any]], worker_started: float | None, now: float
             ) -> list[dict[str, Any]]:
    rows = []
    for name in sorted(jobs):
        j = jobs[name]
        status, note = OK, None
        crash_at = j.get("last_crash_at")
        tick_at = j.get("last_tick_at")
        interval = JOB_INTERVALS.get(name)
        if j.get("state") == "restarting":
            status, note = BAD, "si sta riavviando dopo un errore"
        elif crash_at and now - crash_at < RECENT_CRASH_S:
            status, note = WARN, f"errore {_ago(now - crash_at)} fa, ripartito"
        elif interval and tick_at and now - tick_at > LATE_FACTOR * interval:
            status, note = WARN, f"ultimo giro {_ago(now - tick_at)} fa"
        elif interval and not tick_at and j.get("started_at"):
            waited = now - j["started_at"]
            grace = LATE_FACTOR * interval + (GATE_GRACE_S if name in GATED_JOBS else 0)
            if waited > grace:
                status, note = WARN, f"nessun giro da {_ago(waited)}"
        rows.append({"name": name, "status": status, "note": note,
                     "started_at": _iso(j.get("started_at")),
                     "last_tick_at": _iso(tick_at), "interval_s": interval,
                     "crashes": int(j.get("crashes") or 0),
                     "last_crash_at": _iso(crash_at), "last_error": j.get("last_error")})
    return rows


async def services_section(redis, now: float) -> dict[str, Any]:
    reasons: list[str] = []
    try:
        age = await heartbeat_age_s(redis)
    except Exception as e:  # noqa: BLE001
        return _unreadable("Worker", e)
    if age is None or age > HEARTBEAT_BAD_S:
        status = BAD
        reasons.append("worker fermo: nessun battito" if age is None
                       else f"worker fermo: ultimo battito {_ago(age)} fa")
    elif age > HEARTBEAT_WARN_S:
        status = WARN
        reasons.append(f"worker lento: ultimo battito {_ago(age)} fa")
    else:
        status = OK
    try:
        worker_started, jobs = await read_jobs(redis)
    except Exception as e:
        log.warning("system health: worker jobs unreadable", exc_info=e)
        worker_started, jobs = None, {}
    rows = job_rows(jobs, worker_started, now)
    for r in rows:
        if r["status"] != OK:
            reasons.append(f"{r['name']}: {r['note']}")
    status = worst([status, *(r["status"] for r in rows)])
    summary = ("Tutti i servizi rispondono" if status == OK else reasons[0])
    return _section(status, summary, reasons, heartbeat_age_s=age,
                    worker_started_at=_iso(worker_started), jobs=rows)


# -- market data ------------------------------------------------------------------------

def provider_rows(connectors: list[Connector], now: datetime) -> list[dict[str, Any]]:
    rows = []
    for c in connectors:
        if not c.enabled:
            continue
        checked = c.last_checked_at
        late = checked is None or (now - checked).total_seconds() > CONNECTOR_CHECK_LATE_S
        status = WARN if c.health_status == "error" or late else OK
        rows.append({"name": c.name, "status": status, "health": c.health_status,
                     "last_error": c.last_error,
                     "last_checked_at": checked.isoformat() if checked else None})
    return rows


async def providers_section(session: AsyncSession, now: datetime) -> dict[str, Any]:
    rows = provider_rows(list(await session.scalars(select(Connector).order_by(Connector.name))),
                         now)
    if not rows:
        return _section(BAD, "Nessun fornitore di dati attivo",
                        ["nessun fornitore di dati di mercato attivo"], providers=[])
    failing = [r for r in rows if r["health"] == "error"]
    late = [r for r in rows if r["status"] == WARN and r["health"] != "error"]
    reasons = [f"{r['name']}: {r['last_error'] or 'controllo non riuscito'}" for r in failing]
    reasons += [f"{r['name']}: controllo non recente" for r in late]
    status = BAD if len(failing) == len(rows) else worst(r["status"] for r in rows)
    summary = (f"{len(rows)} fornitori attivi, tutti ok" if status == OK
               else f"{len(failing)} su {len(rows)} fornitori in errore" if failing
               else reasons[0])
    return _section(status, summary, reasons, providers=rows)


def quote_freshness(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """`items`: {symbol, asset_class, open (market), age_s (None = no quote), threshold_s}
    → per asset class: symbols, fresh, stale (market open, older than the threshold or
    missing), closed (market closed), and the stale symbols."""
    by: dict[str, dict[str, Any]] = {}
    for it in items:
        row = by.setdefault(it["asset_class"], {"asset_class": it["asset_class"], "symbols": 0,
                                                "fresh": 0, "stale": 0, "closed": 0,
                                                "threshold_s": it["threshold_s"],
                                                "stale_symbols": []})
        row["symbols"] += 1
        if not it["open"]:
            row["closed"] += 1
        elif it["age_s"] is None or it["age_s"] > it["threshold_s"]:
            row["stale"] += 1
            row["stale_symbols"].append(it["symbol"])
        else:
            row["fresh"] += 1
    return [by[k] for k in sorted(by)]


async def _symbols(session: AsyncSession) -> list[Instrument]:
    positions = select(Position.symbol).join(Portfolio, Portfolio.id == Position.portfolio_id) \
        .where(Position.qty != 0, Portfolio.status != PortfolioStatus.archived)
    watched = select(WatchlistItem.symbol)
    symbols = set(await session.scalars(positions)) | set(await session.scalars(watched))
    if not symbols:
        return []
    return list(await session.scalars(select(Instrument).where(Instrument.symbol.in_(symbols))
                                      .order_by(Instrument.symbol)))


async def quotes_section(session: AsyncSession, redis, now: datetime) -> dict[str, Any]:
    items = []
    thresholds: dict[str, int] = {}
    for instr in await _symbols(session):
        cls = str(instr.asset_class)
        if cls not in thresholds:
            thresholds[cls] = await get_stale_threshold(session, cls)
        try:
            is_open = market_status(instr.market, now).is_open
        except Exception:  # noqa: BLE001 - an unknown market: judged as open
            is_open = True
        meta = await read_quote_meta(redis, instr.symbol)
        age = (now - meta[1]).total_seconds() if meta else None
        items.append({"symbol": instr.symbol, "asset_class": cls, "open": is_open,
                      "age_s": age, "threshold_s": thresholds[cls]})
    rows = quote_freshness(items)
    stale = sum(r["stale"] for r in rows)
    reasons = [f"{r['stale']} quotazioni vecchie ({r['asset_class']}): "
               f"{', '.join(r['stale_symbols'][:5])}" for r in rows if r["stale"]]
    if not items:
        return _section(OK, "Nessun simbolo in posizioni o liste", classes=[])
    status = WARN if stale else OK
    summary = (f"{len(items)} simboli, quotazioni aggiornate" if not stale
               else f"{stale} quotazioni vecchie su {len(items)} simboli")
    return _section(status, summary, reasons, classes=rows)


# -- price quality and provider quotas --------------------------------------------------

REASON_LABELS = {quality.WIDE_SPREAD: "spread ampio", quality.JUMP: "salto improvviso",
                 quality.DISAGREE: "fonti in disaccordo", quality.STALE: "quotazione vecchia"}
TOP_SYMBOLS = 5


def price_quality_section(events: dict[tuple[str, str, str], int],
                          suspects: list[tuple[float, str, str, str]],
                          running: dict[str, float], now: float) -> dict[str, Any]:
    """`events`: (provider, symbol, reason) -> count (24 h); `suspects`: (epoch, provider,
    symbol, reason) of the suspect quotes (24 h); `running`: symbol -> start of its current
    suspect run. Attention when a run lasts more than SUSPECT_ALERT_AFTER_S."""
    last_hour = [x for x in suspects if now - x[0] <= 3600]
    top = Counter(x[2] for x in suspects).most_common(TOP_SYMBOLS)
    by_reason = Counter()
    by_provider: dict[str, Counter] = {}
    for (provider, _, reason), n in events.items():
        by_reason[reason] += n
        by_provider.setdefault(provider, Counter())[reason] += n
    current = [{"symbol": sym, "since": _iso(start), "age_s": round(now - start)}
               for sym, start in sorted(running.items())]
    late = [c for c in current if c["age_s"] > quality.SUSPECT_ALERT_AFTER_S]
    reasons = [f"{c['symbol']}: prezzo non confermato da {_ago(c['age_s'])}" for c in late]
    status = WARN if late else OK
    if late:
        summary = reasons[0]
    elif current:
        summary = f"{len(current)} simboli con prezzo da verificare ora"
    elif suspects:
        summary = (f"{len(last_hour)} quotazioni sospette nell'ultima ora, "
                   f"{len(suspects)} nelle 24 ore")
    else:
        summary = "Nessuna quotazione sospetta nelle 24 ore"
    return _section(
        status, summary, reasons, suspects_1h=len(last_hour), suspects_24h=len(suspects),
        top_symbols=[{"symbol": sym, "count": n} for sym, n in top],
        by_reason=[{"reason": r, "label": REASON_LABELS.get(r, r), "count": n}
                   for r, n in sorted(by_reason.items(), key=lambda kv: (-kv[1], kv[0]))],
        by_provider=[{"provider": p, "counts": dict(sorted(c.items()))}
                     for p, c in sorted(by_provider.items())],
        current=current)


async def price_quality(redis, now: datetime) -> dict[str, Any]:
    ts = now.timestamp()
    events = await quality.read_events(redis, ts)
    suspects = await quality.read_suspects(redis, ts)
    running = await quality.suspect_since(redis, sorted({x[2] for x in suspects}))
    return price_quality_section(events, suspects, running, ts)


def _demoted_text(name: str, q: dict[str, Any]) -> str:
    if q["demoted_reason"] == quota.QUOTA_429:
        return f"{name}: ha risposto 429 (quota esaurita), usato per ultimo fino al reset"
    unit = "giornaliera" if q["demoted_reason"] == quota.DAY else "oraria"
    pct = q["pct_day"] if q["demoted_reason"] == quota.DAY else q["pct_hour"]
    return f"{name}: quota {unit} al {pct:g}%, usato per ultimo fino al reset"


def quotas_section(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """`rows`: {name, quota (quota.usage_dto)} of the enabled providers."""
    demoted = [r for r in rows if r["quota"]["demoted"]]
    reasons = [_demoted_text(r["name"], r["quota"]) for r in demoted]
    status = WARN if demoted else OK
    summary = (reasons[0] if demoted else
               f"{len(rows)} fornitori entro la quota" if rows else "Nessun fornitore attivo")
    return _section(status, summary, reasons, providers=rows, note=quota.NOTE)


async def quota_rows(session: AsyncSession, redis, now: datetime) -> list[dict[str, Any]]:
    """The enabled, registered connectors with their quota block."""
    connectors = [c for c in await session.scalars(select(Connector).order_by(Connector.name))
                  if c.enabled and c.name in PROVIDERS]
    overrides = await quota.load_overrides(session)
    used = await quota.usage(redis, [c.name for c in connectors], now)
    return [{"name": c.name, "quota": quota.usage_dto(
        quota.budget_of(PROVIDERS[c.name], c.rate_limit_per_min, overrides.get(c.name)),
        used.get(c.name), now)} for c in connectors]


async def quotas(session: AsyncSession, redis, now: datetime) -> dict[str, Any]:
    return quotas_section(await quota_rows(session, redis, now))


# -- Paperclip --------------------------------------------------------------------------

def paperclip_reader_section(enabled: bool, state: dict[str, Any], now: datetime
                             ) -> dict[str, Any]:
    if not enabled:
        return _section(OFF, "Lettura di Paperclip disattivata", reader={"enabled": False})
    reasons = []
    errors = int(state.get("consecutive_errors") or 0)
    last_ok = state.get("last_ok_at")
    last_ok_dt = datetime.fromisoformat(last_ok) if last_ok else None
    status = OK
    if errors >= 3:
        status = BAD
        reasons.append(f"Paperclip non raggiungibile da {errors} letture: "
                       f"{state.get('last_error') or ''}".rstrip(": "))
    elif errors:
        status = WARN
        reasons.append(f"ultima lettura di Paperclip non riuscita: {state.get('last_error')}")
    if last_ok_dt is None:
        status = worst([status, WARN])
        reasons.append("Paperclip mai letto")
    elif (now - last_ok_dt).total_seconds() > PAPERCLIP_LATE_S and errors == 0:
        status = worst([status, WARN])
        reasons.append(f"ultima lettura di Paperclip {_ago((now - last_ok_dt).total_seconds())} fa")
    if state.get("isolation") == "changed":
        status = BAD
        reasons.append("regole di isolamento cambiate")
    summary = "Lettura regolare" if status == OK else reasons[0]
    return _section(status, summary, reasons, reader={
        "enabled": True, "last_ok_at": last_ok, "last_error": state.get("last_error"),
        "consecutive_errors": errors, "isolation": state.get("isolation")})


def cycles_section(enabled: bool, view: dict[str, Any]) -> dict[str, Any]:
    if not enabled:
        return _section(OFF, "Lettura di Paperclip disattivata", cycles=view)
    reasons = []
    status = OK
    if view.get("error"):
        status = WARN
        reasons.append(f"routine di Paperclip non lette: {view['error']}")
    if view.get("read_at") is None and not view.get("error"):
        status = WARN
        reasons.append("routine di Paperclip non ancora lette")
    if view.get("missing"):
        status = WARN
        reasons.append(f"{view['missing']} cicli agenti attesi e non partiti oggi")
    if view.get("invalid_cron"):
        reasons.append(f"orario non interpretato: {', '.join(view['invalid_cron'])}")
    summary = (f"{view.get('done', 0)} cicli partiti su {view.get('expected', 0)} attesi "
               f"finora ({view.get('day_total', 0)} nella giornata)")
    return _section(status, summary, reasons, cycles=view)


def watchdog_section(on: bool, warnings: list[dict[str, Any]]) -> dict[str, Any]:
    if not on:
        return _section(OFF, "Controllo dei passaggi disattivato", warnings=[])
    if not warnings:
        return _section(OK, "Nessun passaggio mancante oggi", warnings=[])
    return _section(WARN, f"{len(warnings)} passaggi mancanti oggi",
                    [w["text"] for w in warnings[:5]], warnings=warnings)


# -- Telegram ---------------------------------------------------------------------------

def outbox_section(ready: bool, paused: bool, pending: int, oldest_pending: datetime | None,
                   failed_24h: int, now: datetime) -> dict[str, Any]:
    age = (now - oldest_pending).total_seconds() if oldest_pending else None
    data = {"pending": pending, "failed_24h": failed_24h, "paused": paused,
            "oldest_pending_at": oldest_pending.isoformat() if oldest_pending else None,
            "oldest_pending_age_s": age, "configured": ready}
    if not ready:
        return _section(OFF, "Telegram non configurato o disattivato", **data)
    reasons = []
    status = OK
    if age is not None and age > OUTBOX_BAD_S:
        status = BAD
        reasons.append(f"messaggi Telegram fermi da {_ago(age)}")
    elif age is not None and age > OUTBOX_WARN_S:
        status = WARN
        reasons.append(f"messaggi Telegram in attesa da {_ago(age)}")
    if failed_24h:
        status = worst([status, WARN])
        reasons.append(f"{failed_24h} messaggi Telegram non inviati nelle ultime 24 ore")
    if paused:
        reasons.append("invio Telegram in pausa")
    summary = (f"{pending} in coda, nessun errore" if status == OK and not paused
               else reasons[0])
    return _section(status, summary, reasons, **data)


# -- costs ------------------------------------------------------------------------------

def costs_section(today_usd: Decimal, month: dict[str, Any]) -> dict[str, Any]:
    reasons = []
    status = OK
    budget_usd = month.get("budget_usd")
    if budget_usd is not None:
        if month["spent_usd"] >= budget_usd:
            status = BAD
            reasons.append(f"budget mensile superato: {month['spent_usd']} $ su {budget_usd} $")
        elif (month["days_elapsed"] >= budget.PROJECTION_MIN_DAYS
              and month["projected_usd"] > budget_usd):
            status = WARN
            reasons.append(f"a questo ritmo il mese chiude a {month['projected_usd']} $, "
                           f"oltre il budget di {budget_usd} $")
    summary = (f"Oggi {today_usd.quantize(budget.CENT)} $, proiezione del mese "
               f"{month['projected_usd']} $")
    return _section(status, summary, reasons, today_usd=today_usd.quantize(budget.CENT),
                    month=month)


# -- MCP --------------------------------------------------------------------------------

def mcp_section(outcomes: list[str]) -> dict[str, Any]:
    total = len(outcomes)
    errors = [o for o in outcomes if o != "ok"]
    internal = sum(1 for o in errors if o == "INTERNAL")
    by_code = Counter(errors).most_common(5)
    data = {"calls": total, "errors": len(errors), "internal": internal,
            "by_code": [{"code": c, "count": n} for c, n in by_code]}
    reasons = []
    status = OK
    if internal:
        status = WARN
        reasons.append(f"{internal} errori interni MCP nell'ultima ora")
    if total >= MCP_MIN_CALLS and Decimal(len(errors)) / total > MCP_ERROR_SHARE:
        status = WARN
        reasons.append(f"{len(errors)} chiamate MCP su {total} in errore nell'ultima ora")
    summary = (f"{total} chiamate nell'ultima ora, {len(errors)} in errore" if total
               else "Nessuna chiamata nell'ultima ora")
    return _section(status, summary, reasons, **data)


# -- backup -----------------------------------------------------------------------------

def _age_s(iso: str | None, now: datetime) -> float | None:
    if not iso:
        return None
    try:
        return (now - datetime.fromisoformat(iso)).total_seconds()
    except (TypeError, ValueError):
        return None


def backup_section(state: dict[str, Any], now: datetime) -> dict[str, Any]:
    last = state.get("last_backup") or None
    restore = state.get("last_restore_test") or None
    ok_age = _age_s(state.get("last_ok_at"), now)
    restore_age = _age_s(restore.get("finished_at") if restore else None, now)
    data = {"last_ok_at": state.get("last_ok_at"), "last_ok_name": state.get("last_ok_name"),
            "last_ok_age_s": ok_age, "last_backup": last, "last_restore_test": restore,
            "restore_age_s": restore_age,
            "destinations": [{"dir": d.get("dir"), "files": len(d.get("files") or []),
                              "error": d.get("error")}
                             for d in state.get("destinations") or []]}
    reasons: list[str] = []
    status = OK
    if ok_age is None:
        status = BAD
        reasons.append("nessun backup automatico riuscito registrato")
    elif ok_age > BACKUP_BAD_AGE_S:
        status = BAD
        reasons.append(f"ultimo backup riuscito {_ago(ok_age)} fa")
    if last and last.get("status") != backup_service.OK and (
            ok_age is None or last.get("started_at", "") > (state.get("last_ok_at") or "")):
        status = worst([status, WARN])
        reasons.append(f"ultimo backup {last.get('status')}: {last.get('error') or ''}"
                       .rstrip(": "))
    if restore is None:
        status = worst([status, WARN])
        reasons.append("prova di ripristino mai eseguita")
    elif restore.get("status") != backup_service.OK:
        status = worst([status, WARN])
        reasons.append(f"prova di ripristino non riuscita: {restore.get('error') or ''}"
                       .rstrip(": "))
    elif restore_age is not None and restore_age > RESTORE_WARN_AGE_S:
        status = worst([status, WARN])
        reasons.append(f"ultima prova di ripristino {_ago(restore_age)} fa")
    summary = (f"Ultimo backup {_ago(ok_age)} fa, prova di ripristino riuscita"
               if status == OK else reasons[0])
    return _section(status, summary, reasons, **data)


async def _backup(session: AsyncSession, now: datetime) -> dict[str, Any]:
    return backup_section(await backup_service.load_state(session), now)


# -- version ----------------------------------------------------------------------------

def version_info(worker_started_at: str | None) -> dict[str, Any]:
    build = os.environ.get("SMT_BUILD_ID") or os.environ.get("SMTRADING_VERSION") or "dev"
    return {"build": build, "built_at": os.environ.get("SMT_BUILT_AT") or None,
            "api_started_at": _iso(API_STARTED_AT), "worker_started_at": worker_started_at}


# -- the whole view ---------------------------------------------------------------------

LABELS = {
    "services": "Servizi", "providers": "Fornitori di dati", "quotes": "Quotazioni",
    "paperclip": "Lettura di Paperclip", "cycles": "Cicli agenti di oggi",
    "watchdog": "Passaggi mancanti", "telegram": "Telegram", "costs": "Costi agenti",
    "mcp": "Errori MCP", "backup": "Backup", "price_quality": "Qualità dei prezzi",
    "quotas": "Quota delle fonti",
}


async def _guard(session: AsyncSession, name: str, build: Callable[[], Any]
                 ) -> dict[str, Any]:
    try:
        return await build()
    except Exception as e:  # noqa: BLE001 - one section never breaks the page
        await session.rollback()  # a failed query must not poison the next sections
        return _unreadable(LABELS[name], e)


async def _paperclip(session: AsyncSession, now: datetime, tz: str) -> tuple[dict, dict, bool]:
    cfg = await load_paperclip_config(session)
    state = await load_paperclip_state(session)
    names = {pid: a.get("name") or pid for pid, a in
             ((await model_costs.load_models(session)).get("agents") or {}).items()}
    view = cycles.today_cycles(await cycles.load_cycles(session), now, tz, names)
    return (paperclip_reader_section(cfg.enabled, state, now),
            cycles_section(cfg.enabled, view), cfg.enabled)


async def _watchdog(session: AsyncSession, enabled: bool, today: date, start: datetime
                    ) -> dict[str, Any]:
    on = enabled and await watchdog_on(session)
    rows = (await session.execute(
        select(TelegramOutbox.created_at, TelegramOutbox.text)
        .where(TelegramOutbox.created_at >= start,
               TelegramOutbox.text.like(f"{WATCHDOG_PREFIX}%"))
        .order_by(TelegramOutbox.created_at.desc()).limit(50))).all()
    warnings = [{"at": at.isoformat(), "text": text} for at, text in rows]
    if not warnings:  # the warned items of the day (a message may have been purged)
        raw = await session.scalar(select(AppSetting.value)
                                   .where(AppSetting.key == WATCHDOG_STATE_KEY))
        if isinstance(raw, dict) and raw.get("day") == today.isoformat():
            warnings = [{"at": None, "text": f"{WATCHDOG_PREFIX}{k}"}
                        for k in raw.get("warned") or []]
    return watchdog_section(on, warnings)


async def _telegram(session: AsyncSession, now: datetime) -> dict[str, Any]:
    cfg = await load_telegram_config(session)
    pending, oldest = (await session.execute(
        select(func.count(), func.min(TelegramOutbox.created_at))
        .where(TelegramOutbox.status == OutboxStatus.pending))).one()
    failed = await session.scalar(
        select(func.count()).where(TelegramOutbox.status == OutboxStatus.failed,
                                   TelegramOutbox.created_at >= now - timedelta(hours=24)))
    return outbox_section(is_ready(cfg), cfg.paused, int(pending or 0), oldest,
                          int(failed or 0), now)


async def _costs(session: AsyncSession, today: date) -> dict[str, Any]:
    cfg = await load_paperclip_config(session)
    state = await load_paperclip_state(session)
    today_usd = Decimal(await session.scalar(
        select(func.coalesce(func.sum(AgentCostDay.api_usd + AgentCostDay.other_usd), 0))
        .where(AgentCostDay.day == today)) or 0)
    spent = sum((usd for _, _, usd in await budget.month_spend(session, today)),
                start=Decimal(0))
    month = budget.summary(today, spent, budget.resolve_budget(
        state.get("company_budget_cents"), cfg.monthly_budget_usd))
    return costs_section(today_usd, month)


async def _mcp(session: AsyncSession, now: datetime) -> dict[str, Any]:
    outcomes = list(await session.scalars(
        select(McpAuditLog.outcome).where(McpAuditLog.created_at >= now - MCP_WINDOW,
                                          ~McpAuditLog.method.like("admin:%"))))
    return mcp_section(outcomes)


async def system_health(session: AsyncSession, redis, *, now: datetime | None = None
                        ) -> dict[str, Any]:
    now = now or datetime.now(UTC)
    tz = get_settings().tz
    zone = ZoneInfo(tz)
    today = now.astimezone(zone).date()
    start = datetime.combine(today, datetime.min.time(), tzinfo=zone)
    sections: dict[str, dict[str, Any]] = {}
    sections["services"] = await _guard(session, "services",
                                        lambda: services_section(redis, now.timestamp()))
    sections["providers"] = await _guard(session, "providers", lambda: providers_section(session, now))
    sections["quotes"] = await _guard(session, "quotes", lambda: quotes_section(session, redis, now))
    sections["price_quality"] = await _guard(session, "price_quality",
                                             lambda: price_quality(redis, now))
    sections["quotas"] = await _guard(session, "quotas", lambda: quotas(session, redis, now))
    enabled = False
    try:
        reader, cyc, enabled = await _paperclip(session, now, tz)
    except Exception as e:  # noqa: BLE001
        await session.rollback()
        reader = _unreadable(LABELS["paperclip"], e)
        cyc = _unreadable(LABELS["cycles"], e)
    sections["paperclip"], sections["cycles"] = reader, cyc
    sections["watchdog"] = await _guard(session, "watchdog",
                                        lambda: _watchdog(session, enabled, today, start))
    sections["telegram"] = await _guard(session, "telegram", lambda: _telegram(session, now))
    sections["costs"] = await _guard(session, "costs", lambda: _costs(session, today))
    sections["mcp"] = await _guard(session, "mcp", lambda: _mcp(session, now))
    sections["backup"] = await _guard(session, "backup", lambda: _backup(session, now))
    for key, s in sections.items():
        s["label"] = LABELS[key]
    status = worst(s["status"] for s in sections.values())
    reasons = [r for s in sections.values() if s["status"] in (WARN, BAD) for r in s["reasons"]]
    worker_started = sections["services"].get("worker_started_at")
    return {"status": status, "reasons": reasons, "checked_at": now.isoformat(),
            "sections": sections, "version": version_info(worker_started)}
