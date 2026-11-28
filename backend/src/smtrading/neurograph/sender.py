"""The nightly export to Neurograph's ingest API (contract confirmed by Neurograph).

    POST {SMT_NEUROGRAPH_EXPORT_URL}/{decisioni|ordini|esecuzioni}/{AAAA-MM-GG}
    Authorization: Bearer <key>          Content-Type: application/x-ndjson
    X-SMT-Export-Id: smtrading:{tipo}:{giorno}:{versione}      X-SMT-Rows: <n>
    body: JSON Lines (one object per line, no trailing newline), at most 64 MB

Answers: 201 first send of a day, 200 «già presente» (version ≤ stored) or «sostituito»
(higher version): confirmed. 400 (format): that version is not retried, one Telegram
system alert with Neurograph's message. 401/403 (key): the run stops, one alert per streak
of refused nights. 5xx / timeout / unreachable: retried the next night (an unreachable
Neurograph or a 503, ingest not configured, also ends the run: every other file would fail
the same way); at the OUTAGE_ALERT_NIGHTS-th such night in a row, one alert per streak.

`run_nightly` runs once per Rome night, from NIGHT_AT, claimed in `job_runs` (JOB, the Rome
date), over the BACKFILL_DAYS before tonight (never before FIRST_DAY), oldest day first,
the three types in order: a file never sent, a failed latest version (again), and a
confirmed `ordini` file whose orders were updated after it was sent (next version: the
day is replaced). An unexpected error on one file is logged and the run goes on. The state
of each file is `neurograph_exports` (also the «Fiume dei flussi» events), of each night
`neurograph_export_nights`. No key = off.

The key is only ever put in the Authorization header: it is registered for log masking and
Neurograph's messages are masked before they are stored or sent on Telegram.
"""

import json
import logging
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import httpx
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from smtrading.common.config import get_settings
from smtrading.common.logsetup import redact_log_text, register_secret
from smtrading.jobs.runs import run_job_once
from smtrading.neurograph.export import KINDS, ROME, Kind, build_rows, changed_since
from smtrading.neurograph.models import NeurographExport, NeurographExportNight
from smtrading.telegram.outbox import enqueue

log = logging.getLogger("smtrading.neurograph")

JOB = "neurograph export"
NIGHT_AT = time(0, 30)  # Rome: the day just ended has settled
BACKFILL_DAYS = 30
FIRST_DAY = date(2026, 11, 23)  # the campaign's first day
MAX_BODY = 64 * 1024 * 1024
TIMEOUT = httpx.Timeout(120.0, connect=10.0)
MESSAGE_MAX = 300
CONFIRMED, REJECTED, FAILED = "confirmed", "rejected", "failed"
# result statuses beyond the stored ones: the key was refused (stored as failed)
UNAUTHORIZED = "unauthorized"


@dataclass(frozen=True)
class SendResult:
    status: str  # confirmed | rejected | unauthorized | failed
    message: str
    http_status: int | None = None
    rows: int = 0
    stop: bool = False  # the run should not go on (key refused, Neurograph unreachable)


def _key(settings) -> str | None:
    secret = getattr(settings, "neurograph_export_key", None)
    value = secret.get_secret_value() if secret is not None else None
    return value or None


def export_id(kind: str, day: date, version: int) -> str:
    return f"smtrading:{kind}:{day.isoformat()}:{version}"


def body_of(rows: list[dict]) -> bytes:
    return "\n".join(json.dumps(r, ensure_ascii=False, separators=(",", ":"))
                     for r in rows).encode("utf-8")


def _message(resp: httpx.Response) -> str:
    """Neurograph's answer in a few words (its JSON message, else the text), masked."""
    text = resp.text or ""
    try:
        data: Any = resp.json()
    except ValueError:
        data = None
    if isinstance(data, dict):
        for k in ("messaggio", "message", "errore", "error", "detail", "esito"):
            if isinstance(data.get(k), str):
                text = data[k]
                break
    elif isinstance(data, str):
        text = data
    return redact_log_text(" ".join(text.split()))[:MESSAGE_MAX] or f"risposta {resp.status_code}"


async def _record(session: AsyncSession, kind: str, day: date, version: int,
                  r: SendResult) -> None:
    status = r.status if r.status in (CONFIRMED, REJECTED) else FAILED
    values = {"status": status, "rows": r.rows, "http_status": r.http_status,
              "message": r.message[:500] or None, "sent_at": func.clock_timestamp()}
    stmt = insert(NeurographExport).values(kind=kind, day=day, version=version, attempts=1,
                                           **values)
    await session.execute(stmt.on_conflict_do_update(
        index_elements=["kind", "day", "version"],
        set_={**values, "attempts": NeurographExport.attempts + 1}))
    await session.flush()


async def _post(client: httpx.AsyncClient, url: str, key: str, kind: str, day: date,
                version: int, body: bytes, n: int) -> SendResult:
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/x-ndjson",
               "X-SMT-Export-Id": export_id(kind, day, version), "X-SMT-Rows": str(n)}
    target = f"{url.rstrip('/')}/{kind}/{day.isoformat()}"
    try:
        resp = await client.post(target, content=body, headers=headers, timeout=TIMEOUT)
    except httpx.HTTPError as e:
        return SendResult(FAILED, f"Neurograph non raggiungibile ({type(e).__name__})",
                          None, n, stop=True)
    msg, code = _message(resp), resp.status_code
    if code in (200, 201):
        return SendResult(CONFIRMED, msg, code, n)
    if code == 400:
        return SendResult(REJECTED, msg, code, n)
    if code in (401, 403):
        return SendResult(UNAUTHORIZED, msg, code, n, stop=True)
    return SendResult(FAILED, msg, code, n, stop=code == 503)


async def send_day(session: AsyncSession, client: httpx.AsyncClient, kind: Kind, day: date,
                   version: int = 1, *, settings=None) -> SendResult:
    """Build and send one file, record its result (in `session`, not committed). Without a
    key nothing is sent nor recorded (status failed, «invio spento»)."""
    settings = settings or get_settings()
    key = _key(settings)
    if key is None:
        return SendResult(FAILED, "invio spento: nessuna chiave", stop=True)
    register_secret(key)
    rows = await build_rows(session, kind, day, tz=ROME)
    body = body_of(rows)
    if len(body) > MAX_BODY:
        result = SendResult(REJECTED, f"file oltre 64 MB ({len(body)} byte): non inviato",
                            None, len(rows))
    else:
        result = await _post(client, settings.neurograph_export_url, key, kind, day,
                             version, body, len(rows))
    await _record(session, kind, day, version, result)
    level = logging.INFO if result.status == CONFIRMED else logging.WARNING
    log.log(level, "neurograph export %s: %s (%s, %s rows) %s", export_id(kind, day, version),
            result.status, result.http_status, result.rows, result.message)
    return result


def _it_day(day: date) -> str:
    return f"{day:%d/%m/%Y}"


async def _alert_rejected(session: AsyncSession, kind: str, day: date, version: int,
                          r: SendResult) -> None:
    text = (f"⚠️ Neurograph ha rifiutato i dati di SMTrading: {kind} del {_it_day(day)} "
            f"(versione {version}).\nMotivo: {r.message}\n"
            "Da fare: correggere il file e rimandare il giorno con una versione più alta "
            "(comando neurograph-export); gli altri giorni e tipi proseguono.")
    await enqueue(session, source="system", category="system", level="warning",
                  text=text[:3800])


async def _alert_unauthorized(session: AsyncSession, r: SendResult) -> None:
    text = ("⚠️ Invio notturno dei dati a Neurograph fermo: chiave non accettata "
            f"(risposta {r.http_status}).\n"
            "Da fare: verificare con Neurograph la chiave SMT_NEUROGRAPH_EXPORT_KEY; "
            "la prossima notte l'invio riprova con tutti i giorni mancanti.")
    await enqueue(session, source="system", category="system", level="warning", text=text)


OUTAGE_ALERT_NIGHTS = 3  # consecutive outage nights before the one alert of the streak


def pending_days(today: date) -> list[date]:
    first = max(FIRST_DAY, today - timedelta(days=BACKFILL_DAYS))
    return [first + timedelta(days=i) for i in range((today - first).days)]


def leaves_window(day: date) -> date:
    """The first night whose catch-up no longer covers `day`."""
    return day + timedelta(days=BACKFILL_DAYS + 1)


async def _latest(session: AsyncSession, first: date) -> dict[tuple[str, date], Any]:
    """The highest version of every file from `first` on: (version, status, sent_at) rows."""
    rows = (await session.execute(
        select(NeurographExport.kind, NeurographExport.day, NeurographExport.version,
               NeurographExport.status, NeurographExport.sent_at)
        .where(NeurographExport.day >= first).order_by(NeurographExport.version))).all()
    return {(r.kind, r.day): r for r in rows}


async def _plan(session: AsyncSession, days: list[date]) -> list[tuple[str, date, int]]:
    """(kind, day, version) to send tonight, oldest day first: a file never sent (v1); a
    failed latest version (again); a confirmed `ordini` whose orders changed since (next
    version, Neurograph replaces the day). A rejected latest version waits for a manual
    correction (CLI, higher version)."""
    latest = await _latest(session, days[0])
    plan = []
    for day in days:
        for kind in KINDS:
            row = latest.get((kind, day))
            if row is None:
                plan.append((kind, day, 1))
            elif row.status == FAILED:
                plan.append((kind, day, row.version))
            elif row.status == CONFIRMED and await changed_since(session, kind, day,
                                                                 row.sent_at, tz=ROME):
                plan.append((kind, day, row.version + 1))
    return plan


async def _waiting(session: AsyncSession, days: list[date]) -> tuple[int, date | None]:
    """(files of the window not confirmed nor rejected, the oldest day among them)."""
    latest = await _latest(session, days[0])
    waiting = [day for day in days for kind in KINDS
               if (r := latest.get((kind, day))) is None or r.status == FAILED]
    return len(waiting), min(waiting, default=None)


async def _streak(session: AsyncSession, outcome: str, before: date) -> int:
    """Consecutive nights before `before` (the most recent first) with `outcome`."""
    n = 0
    for o in await session.scalars(
            select(NeurographExportNight.outcome).where(NeurographExportNight.night < before)
            .order_by(NeurographExportNight.night.desc()).limit(OUTAGE_ALERT_NIGHTS + 1)):
        if o != outcome:
            break
        n += 1
    return n


async def _alert_outage(session: AsyncSession, nights: int, waiting: int,
                        oldest: date | None, last: str) -> None:
    leaves = f"{leaves_window(oldest):%d/%m}" if oldest is not None else "—"
    text = (f"⚠️ Invio notturno dei dati a Neurograph non riuscito da {nights} notti "
            f"(ultima risposta: {last}).\n"
            f"Da fare: verificare la ricezione di Neurograph; {waiting} file in attesa, "
            f"il più vecchio esce dal recupero il {leaves}.")
    await enqueue(session, source="system", category="system", level="warning",
                  text=text[:3800])


def _is_outage(r: SendResult) -> bool:
    return r.status == FAILED and (r.stop or (r.http_status or 0) >= 500)


async def _night_end(sessionmaker, today: date, days: list[date], outcome: str,
                     last: str) -> None:
    """Record tonight's outcome; the outage alert on the OUTAGE_ALERT_NIGHTS-th outage night
    in a row, once per streak."""
    async with sessionmaker() as session:
        before = await _streak(session, outcome, today) if outcome != "ok" else 0
        await session.execute(insert(NeurographExportNight).values(night=today, outcome=outcome)
                               .on_conflict_do_update(index_elements=["night"],
                                                      set_={"outcome": outcome}))
        if outcome == "outage" and before + 1 == OUTAGE_ALERT_NIGHTS:
            waiting, oldest = await _waiting(session, days)
            await _alert_outage(session, before + 1, waiting, oldest, last)
        await session.commit()


async def run_nightly(now: datetime, *, sessionmaker: async_sessionmaker[AsyncSession],
                      http: httpx.AsyncClient, settings=None) -> dict | None:
    """Tonight's export (see the module doc); None when off (no key), not yet NIGHT_AT, or
    tonight already ran; else {sent, confirmed, rejected, failed, stopped, outcome}."""
    settings = settings or get_settings()
    if _key(settings) is None:
        return None
    local = now.astimezone(ZoneInfo(ROME))
    if local.time() < NIGHT_AT:
        return None
    today = local.date()
    async with sessionmaker() as session:
        if not await run_job_once(session, JOB, today.isoformat()):
            return None
        await session.commit()
    counts: dict[str, Any] = {"sent": 0, CONFIRMED: 0, REJECTED: 0, FAILED: 0,
                              "stopped": False, "outcome": "ok"}
    days = pending_days(today)
    if not days:
        return counts
    async with sessionmaker() as session:
        plan = await _plan(session, days)
    outcome, last = "ok", ""
    for kind, day, version in plan:
        try:
            async with sessionmaker() as session:
                r = await send_day(session, http, kind, day, version, settings=settings)
                if r.status == REJECTED:
                    await _alert_rejected(session, kind, day, version, r)
                elif r.status == UNAUTHORIZED and await _streak(
                        session, "unauthorized", today) == 0:
                    await _alert_unauthorized(session, r)  # once per streak
                await session.commit()
        except Exception:
            log.exception("neurograph export of %s failed", export_id(kind, day, version))
            counts["sent"] += 1
            counts[FAILED] += 1
            continue
        counts["sent"] += 1
        counts[r.status if r.status in (CONFIRMED, REJECTED) else FAILED] += 1
        if r.status == UNAUTHORIZED:
            outcome = "unauthorized"
        elif _is_outage(r) and outcome == "ok":
            outcome, last = "outage", r.message
        if r.stop:
            counts["stopped"] = True
            log.warning("neurograph export stopped for tonight: %s", r.message)
            break
    counts["outcome"] = outcome
    await _night_end(sessionmaker, today, days, outcome, last)
    log.info("neurograph export of %s: %s", today, counts)
    return counts
