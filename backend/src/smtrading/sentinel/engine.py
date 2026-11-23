"""The sentinel's memory (pure): what to tell the owner of this pass's findings.

A `Finding` is a problem seen in this pass, with a stable `key`:
- a condition (default): one «Nuovo problema» when it starts (once it has lasted
  `after_s`), a reminder at most every REMINDER_EVERY while it lasts, a «Risolto» when a
  pass that checked its kind no longer finds it; a new `signature` while it is open (e.g.
  «attenzione» became «problema») is told at once as an update;
- an event (`event=True`, e.g. a failed agent run): told once, remembered for
  EVENT_RETENTION so it is never told again, no reminder, no «Risolto».

`covered` is the set of kinds this pass really checked: the open and pending problems of a
kind whose check could not run (Paperclip unreachable …) are kept as they are, never
resolved by a failed read. The state is plain JSON (kept in `app_settings`, it survives a
restart)."""

import copy
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

REMINDER_EVERY = timedelta(hours=2)
EVENT_RETENTION = timedelta(days=8)
HISTORY_RETENTION = timedelta(days=8)
WEEK = timedelta(days=7)
MAX_TEXT = 3800  # under Telegram's 4096
MAX_DETAIL = 600
WEEKLY_DAY, WEEKLY_HOUR = 6, 20  # Sunday 20:00 local
MAX_HISTORY = 5000

LEVELS = {"info": 0, "warning": 1, "critical": 2}

KIND_LABELS = {
    "health": "Salute del sistema",
    "agent": "Agenti Paperclip",
    "run": "Cicli agenti non riusciti",
    "cycle": "Cicli mancati",
    "issue": "Task bloccati",
    "mcp": "Errori MCP ripetuti",
    "errlog": "Errori nei log",
    "identity": "Più identità in un minuto",
    "author": "Autore delle decisioni",
    "leak": "Dati visibili agli agenti",
}


@dataclass(frozen=True)
class Finding:
    key: str
    kind: str
    title: str
    detail: str = ""
    level: str = "warning"
    after_s: float = 0
    event: bool = False
    signature: str | None = None


@dataclass(frozen=True)
class Note:
    """What to tell: `what` is start | reminder | update | resolved | event."""
    what: str
    title: str
    detail: str
    level: str
    kind: str
    since: datetime


def fresh_state() -> dict[str, Any]:
    return {"open": {}, "pending": {}, "events": {}, "history": [], "last_pass_at": None,
            "last_error": None, "weekly_sent": None, "cursors": {}}


def load(raw: Any) -> dict[str, Any]:
    state = fresh_state()
    if not isinstance(raw, dict):
        return state
    for key, default in fresh_state().items():
        value = raw.get(key, default)
        if isinstance(default, dict) and not isinstance(value, dict):
            value = {}
        if isinstance(default, list) and not isinstance(value, list):
            value = []
        state[key] = copy.deepcopy(value)
    return state


def _ts(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        ts = datetime.fromisoformat(value)
    except ValueError:
        return None
    return ts if ts.tzinfo else ts.replace(tzinfo=UTC)


def step(state: dict[str, Any], findings: list[Finding], now: datetime,
         covered: set[str]) -> list[Note]:
    """Updates `state` with this pass's findings; returns what to tell."""
    notes: list[Note] = []
    seen = {f.key: f for f in findings}
    opened: dict[str, dict] = state["open"]
    pending: dict[str, dict] = state["pending"]
    events: dict[str, str] = state["events"]
    history: list[dict] = state["history"]

    def remember(kind: str) -> None:
        history.append({"kind": kind, "at": now.isoformat()})

    for f in findings:
        if f.event:
            if f.key in events:
                continue
            events[f.key] = now.isoformat()
            remember(f.kind)
            notes.append(Note("event", f.title, f.detail, f.level, f.kind, now))
            continue
        entry = opened.get(f.key)
        if entry is not None:
            entry.update(title=f.title, detail=f.detail, level=f.level)
            last = _ts(entry.get("told_at")) or now
            if f.signature != entry.get("signature"):
                entry.update(signature=f.signature, told_at=now.isoformat())
                notes.append(Note("update", f.title, f.detail, f.level, f.kind,
                                  _ts(entry["since"]) or now))
            elif now - last >= REMINDER_EVERY:
                entry["told_at"] = now.isoformat()
                notes.append(Note("reminder", f.title, f.detail, f.level, f.kind,
                                  _ts(entry["since"]) or now))
            continue
        wait = pending.get(f.key)
        first = (_ts(wait.get("since")) if isinstance(wait, dict) else None) or now
        pending[f.key] = {"since": first.isoformat(), "kind": f.kind}
        if (now - first).total_seconds() < f.after_s:
            continue
        pending.pop(f.key, None)
        opened[f.key] = {"kind": f.kind, "title": f.title, "detail": f.detail,
                         "level": f.level, "since": first.isoformat(),
                         "told_at": now.isoformat(), "signature": f.signature}
        remember(f.kind)
        notes.append(Note("start", f.title, f.detail, f.level, f.kind, first))

    for key in [k for k, e in opened.items() if k not in seen and e.get("kind") in covered]:
        e = opened.pop(key)
        notes.append(Note("resolved", e.get("title") or key, "", e.get("level") or "warning",
                          e.get("kind") or "", _ts(e.get("since")) or now))
    for key in [k for k, w in pending.items() if k not in seen
                and (w.get("kind") if isinstance(w, dict) else None) in covered]:
        pending.pop(key, None)
    for key in [k for k, at in events.items()
                if (_ts(at) or now) < now - EVENT_RETENTION]:
        events.pop(key, None)
    state["history"] = [h for h in history
                        if (_ts(h.get("at")) or now) >= now - HISTORY_RETENTION][-MAX_HISTORY:]
    return notes


# -- texts ---------------------------------------------------------------------------------

def ago(delta: timedelta) -> str:
    seconds = max(int(delta.total_seconds()), 0)
    if seconds < 90 * 60:
        return f"{max(round(seconds / 60), 1)} min"
    if seconds < 48 * 3600:
        return f"{seconds / 3600:.1f} h".replace(".", ",")
    return f"{seconds / 86400:.1f} giorni".replace(".", ",")


_ICON = {"critical": "🔴", "warning": "🟠", "info": "ℹ️"}


def _detail(text: str) -> str:
    text = text.strip()
    if len(text) > MAX_DETAIL:
        text = text[:MAX_DETAIL - 1] + "…"
    return "\n".join(f"   {line}" for line in text.splitlines() if line.strip())


def _line(n: Note, now: datetime, tz: str) -> str:
    icon = _ICON.get(n.level, "🟠")
    if n.what == "resolved":
        return f"🟢 Risolto: {n.title} (durato {ago(now - n.since)})"
    if n.what == "start":
        head = f"{icon} Nuovo problema: {n.title}"
        if now - n.since >= timedelta(minutes=1):
            head += f" (da {n.since.astimezone(ZoneInfo(tz)).strftime('%H:%M')})"
    elif n.what == "event":
        head = f"{icon} {n.title}"
    elif n.what == "update":
        head = f"{icon} Cambiato: {n.title} (aperto da {ago(now - n.since)})"
    else:
        head = f"{icon} Ancora aperto da {ago(now - n.since)}: {n.title}"
    detail = _detail(n.detail) if n.detail else ""
    return f"{head}\n{detail}" if detail else head


_ORDER = {"start": 0, "event": 1, "update": 2, "reminder": 3, "resolved": 4}


def compose(notes: list[Note], now: datetime, tz: str) -> tuple[str, str]:
    """(level, text) of one Telegram message telling every note of a pass."""
    ordered = sorted(notes, key=lambda n: (_ORDER.get(n.what, 9), -LEVELS.get(n.level, 1)))
    told = [n for n in ordered if n.what != "resolved"]
    level = max((n.level for n in told), key=lambda lv: LEVELS.get(lv, 1), default="info")
    lines = ["🛡 Sentinella di sistema"]
    size = len(lines[0])
    for i, n in enumerate(ordered):
        line = _line(n, now, tz)
        if size + len(line) + 1 > MAX_TEXT - 80:
            lines.append(f"… e altri {len(ordered) - i} avvisi: vedi «Salute del sistema».")
            break
        lines.append(line)
        size += len(line) + 1
    return level, "\n".join(lines)


# -- weekly summary ------------------------------------------------------------------------

def _week_id(now: datetime, tz: str) -> str:
    year, week, _ = now.astimezone(ZoneInfo(tz)).isocalendar()
    return f"{year}-W{week:02d}"


def weekly_due(state: dict[str, Any], now: datetime, tz: str) -> bool:
    local = now.astimezone(ZoneInfo(tz))
    return (local.weekday() == WEEKLY_DAY and local.hour >= WEEKLY_HOUR
            and state.get("weekly_sent") != _week_id(now, tz))


def mark_weekly(state: dict[str, Any], now: datetime, tz: str) -> None:
    state["weekly_sent"] = _week_id(now, tz)


def weekly_summary(state: dict[str, Any], now: datetime, tz: str) -> str:
    counts: dict[str, int] = {}
    for h in state.get("history") or []:
        at = _ts(h.get("at"))
        if at is not None and at >= now - WEEK:
            counts[h.get("kind") or "?"] = counts.get(h.get("kind") or "?", 0) + 1
    lines = ["📊 Sentinella di sistema: Riepilogo della settimana"]
    if counts:
        total = sum(counts.values())
        lines.append(f"{total} avvisi in 7 giorni:")
        for kind, n in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])):
            lines.append(f"• {KIND_LABELS.get(kind, kind)}: {n}")
    else:
        lines.append("Questa settimana nessun avviso.")
    opened = state.get("open") or {}
    if opened:
        lines.append("Ancora aperti:")
        for e in sorted(opened.values(), key=lambda e: e.get("since") or ""):
            since = _ts(e.get("since")) or now
            lines.append(f"• {e.get('title')} (da {ago(now - since)})")
    else:
        lines.append("Nessun problema aperto ora.")
    text = "\n".join(lines)
    return text if len(text) <= MAX_TEXT else text[:MAX_TEXT - 1] + "…"
