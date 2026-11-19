"""Agent cycles of the day («Salute del sistema»): the cycles expected from the Paperclip
routines' schedules against the executions that started.

The reader (GET only) stores in `app_settings["paperclip_cycles"]` the routines
(`GET /api/companies/{id}/routines`: title, status, assignee, and the schedule triggers
with their cron expression and time zone) and the recent routine executions (`GET
…/issues?originKind=routine_execution`: one issue per execution, `originId` = the
routine). Expected = the fires of the active routines' enabled schedule triggers from
local midnight to now; done = the execution issues created today. A fire coalesced
into an execution still open («coalesce_if_active») creates no issue: done can be
lower than expected without a missed cycle; the page says so."""

import copy
from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from smtrading.risk.models import AppSetting

CYCLES_KEY = "paperclip_cycles"
KEEP_EXECUTIONS_DAYS = 2
MAX_EXECUTIONS = 500
EXECUTIONS_LIMIT = 200

DEFAULT_CYCLES: dict[str, Any] = {"routines": [], "executions": [], "read_at": None,
                                  "error": None}

_DOW = {"sun": 0, "mon": 1, "tue": 2, "wed": 3, "thu": 4, "fri": 5, "sat": 6}
_MON = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}
_MACROS = {"@yearly": "0 0 1 1 *", "@annually": "0 0 1 1 *", "@monthly": "0 0 1 * *",
           "@weekly": "0 0 * * 0", "@daily": "0 0 * * *", "@midnight": "0 0 * * *",
           "@hourly": "0 * * * *"}


class CronError(ValueError):
    pass


def _field(text: str, lo: int, hi: int, names: dict[str, int] | None = None) -> set[int]:
    out: set[int] = set()
    for part in text.split(","):
        step = 1
        if "/" in part:
            part, s = part.split("/", 1)
            if not s.isdigit() or int(s) < 1:
                raise CronError(text)
            step = int(s)
        if part in ("*", ""):
            a, b = lo, hi
        elif "-" in part:
            x, y = part.split("-", 1)
            a, b = _value(x, names), _value(y, names)
        else:
            a = _value(part, names)
            b = hi if step > 1 else a
        if a < lo or b > hi or a > b:
            raise CronError(text)
        out.update(range(a, b + 1, step))
    return out


def _value(text: str, names: dict[str, int] | None) -> int:
    t = text.strip().lower()
    if names and t in names:
        return names[t]
    if not t.isdigit():
        raise CronError(text)
    return int(t)


class Cron:
    """A 5-field cron expression (minute hour day-of-month month day-of-week; lists,
    ranges, steps, names, @daily & co.). Day-of-month and day-of-week both restricted:
    either matches (as in cron)."""

    def __init__(self, expr: str) -> None:
        expr = _MACROS.get(expr.strip().lower(), expr.strip())
        fields = expr.split()
        if len(fields) != 5:
            raise CronError(expr)
        self.minutes = _field(fields[0], 0, 59)
        self.hours = _field(fields[1], 0, 23)
        self.dom = _field(fields[2], 1, 31)
        self.months = _field(fields[3], 1, 12, _MON)
        dow = _field(fields[4], 0, 7, _DOW)
        self.dow = {d % 7 for d in dow}
        self.dom_any, self.dow_any = fields[2] in ("*", "?"), fields[4] in ("*", "?")

    def matches(self, t: datetime) -> bool:
        if t.minute not in self.minutes or t.hour not in self.hours or t.month not in self.months:
            return False
        dom_ok = t.day in self.dom
        dow_ok = (t.isoweekday() % 7) in self.dow
        if self.dom_any or self.dow_any:
            return dom_ok and dow_ok
        return dom_ok or dow_ok


def fires(expr: str, tz: str, start: datetime, end: datetime) -> list[datetime]:
    """The fire times in [start, end) (aware datetimes), minute by minute in `tz`."""
    cron = Cron(expr)
    zone = ZoneInfo(tz)
    out = []
    t = start.astimezone(UTC).replace(second=0, microsecond=0)
    if t < start:
        t += timedelta(minutes=1)
    while t < end:
        if cron.matches(t.astimezone(zone)):
            out.append(t)
        t += timedelta(minutes=1)
    return out


# -- normalising what the reader reads --------------------------------------------------

def normalize_routines(routines: list[dict]) -> list[dict[str, Any]]:
    out = []
    for r in routines:
        if not isinstance(r, dict) or not r.get("id"):
            continue
        triggers = []
        for t in r.get("triggers") or []:
            if isinstance(t, dict) and t.get("kind") == "schedule" and t.get("cronExpression"):
                triggers.append({"cron": str(t["cronExpression"])[:64],
                                 "tz": str(t.get("timezone") or "")[:64] or None,
                                 "enabled": bool(t.get("enabled", True)),
                                 "next_run_at": t.get("nextRunAt")})
        out.append({"id": str(r["id"]), "title": str(r.get("title") or "")[:200],
                    "status": str(r.get("status") or ""),
                    "agent_id": str(r.get("assigneeAgentId") or "") or None,
                    "triggers": triggers})
    return out


def normalize_executions(issues: list[dict], now: datetime) -> list[dict[str, Any]]:
    first = now - timedelta(days=KEEP_EXECUTIONS_DAYS)
    out = []
    for i in issues:
        if not isinstance(i, dict) or i.get("originKind") != "routine_execution":
            continue
        created = i.get("createdAt")
        try:
            ts = datetime.fromisoformat(created) if isinstance(created, str) else None
        except ValueError:
            ts = None
        if ts is None or ts.tzinfo is None or ts < first:
            continue
        out.append({"routine_id": str(i.get("originId") or "") or None,
                    "identifier": str(i.get("identifier") or "")[:32] or None,
                    "status": str(i.get("status") or ""), "created_at": ts.isoformat()})
    return sorted(out, key=lambda e: e["created_at"])[-MAX_EXECUTIONS:]


async def load_cycles(session: AsyncSession) -> dict[str, Any]:
    raw = await session.scalar(select(AppSetting.value).where(AppSetting.key == CYCLES_KEY))
    state = copy.deepcopy(DEFAULT_CYCLES)
    if isinstance(raw, dict):
        state.update({k: v for k, v in raw.items() if k in DEFAULT_CYCLES})
    return state


async def save_cycles(session: AsyncSession, state: dict[str, Any]) -> None:
    value = {k: state.get(k, v) for k, v in DEFAULT_CYCLES.items()}
    stmt = insert(AppSetting).values(key=CYCLES_KEY, value=value)
    await session.execute(stmt.on_conflict_do_update(index_elements=["key"],
                                                     set_={"value": value}))
    await session.flush()


# -- the day's view -------------------------------------------------------------------

def _zone(tz: str | None, default: str) -> str:
    if tz:
        try:
            ZoneInfo(tz)
            return tz
        except (ZoneInfoNotFoundError, ValueError):
            pass
    return default


def today_cycles(state: dict[str, Any], now: datetime, tz: str,
                 names: dict[str, str] | None = None) -> dict[str, Any]:
    """Expected (so far and in the whole day) and done today, per routine and in total."""
    zone = ZoneInfo(tz)
    day: date = now.astimezone(zone).date()
    start = datetime.combine(day, time(0), tzinfo=zone)
    end_of_day = start + timedelta(days=1)
    done: dict[str | None, list[dict]] = {}
    for e in state.get("executions") or []:
        try:
            ts = datetime.fromisoformat(e["created_at"])
        except (KeyError, TypeError, ValueError):
            continue
        if start <= ts < end_of_day:
            done.setdefault(e.get("routine_id"), []).append(e)
    rows, invalid = [], []
    for r in state.get("routines") or []:
        if r.get("status") != "active":
            continue
        expected = day_total = 0
        for t in r.get("triggers") or []:
            if not t.get("enabled"):
                continue
            try:
                times = fires(t["cron"], _zone(t.get("tz"), tz), start, end_of_day)
            except CronError:
                invalid.append(r.get("title"))
                continue
            day_total += len(times)
            expected += sum(1 for f in times if f <= now)
        if not day_total and not done.get(r["id"]):
            continue
        executions = done.get(r["id"], [])
        rows.append({"routine": r.get("title"),
                     "agent": (names or {}).get(r.get("agent_id") or "", None),
                     "expected": expected, "day_total": day_total,
                     "done": len(executions),
                     "completed": sum(1 for e in executions if e.get("status") == "done")})
    rows.sort(key=lambda x: x["routine"] or "")
    return {"day": day.isoformat(), "read_at": state.get("read_at"),
            "error": state.get("error"),
            "expected": sum(r["expected"] for r in rows),
            "day_total": sum(r["day_total"] for r in rows),
            "done": sum(r["done"] for r in rows),
            "missing": sum(max(r["expected"] - r["done"], 0) for r in rows),
            "routines": rows, "invalid_cron": sorted({t for t in invalid if t})}
