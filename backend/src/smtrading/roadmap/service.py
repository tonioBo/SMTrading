"""The «Roadmap» page: the curated file `roadmap.json` (package data) and the live numbers.

The file (written by hand in every SMTrading work, in plain Italian):

    updated     "YYYY-MM-DD", the day of the last update
    summary     «dove siamo» in one or two sentences
    campaign    {name, start, end, portfolios: [{code, name, kind}],
                 goal: {label, kind, target_pct, horizon}}
    go_no_go    "YYYY-MM-DD", the «sì/no» decision
    stages      [{id, title, status, owner, start (null: not known), end?, what, why, items}]
                items: [{title, status, detail, refs: [repo paths], owner?, date?, kind?}]
    glossary    [{term, meaning}]

`status` fatto | in_corso | prossimo | futuro; `owner` smtrading | neurograph | entrambi |
matteo; item `kind` lavoro | esercitazione | esperimento | strumento | criterio | prova.

Live numbers (`live_numbers`): the campaign day, the equity in EUR of the portfolios in the
totals (`total_portfolio_ids`, latest snapshot each) and the progress of the campaign goal
(the active goal on the total matching `campaign.goal`), the drills done / planned (items
`kind: esercitazione`), the days to the decision, the MCP tools that call Neurograph
(`flow.map.TOOL_DEST`). A missing goal or snapshot is None with a short note, never an
error."""

import json
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from functools import lru_cache
from importlib import resources
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from smtrading.common.config import get_settings
from smtrading.flow.map import TOOL_DEST
from smtrading.goals.models import Goal
from smtrading.goals.progress import goal_progress
from smtrading.portfolios.models import total_portfolio_ids
from smtrading.risk.models import EquitySnapshot

STATUSES = ("fatto", "in_corso", "prossimo", "futuro")
OWNERS = ("smtrading", "neurograph", "entrambi", "matteo")
KINDS = ("lavoro", "esercitazione", "esperimento", "strumento", "criterio", "prova")
DRILL = "esercitazione"
_NOT_STARTED = ("prossimo", "futuro")
_TOP = ("updated", "summary", "campaign", "go_no_go", "stages", "glossary")
_STAGE = ("id", "title", "status", "owner", "start", "what", "why", "items")
_ITEM = ("title", "status", "detail", "refs")


class RoadmapError(ValueError):
    pass


def _date(value: Any) -> date | None:
    if not isinstance(value, str):
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def _text(value: Any) -> bool:
    return isinstance(value, str) and value.strip() != ""


def _check_item(where: str, item: Any, errors: list[str]) -> None:
    if not isinstance(item, dict):
        errors.append(f"{where}: non è un oggetto")
        return
    for key in _ITEM:
        if key not in item:
            errors.append(f"{where}: manca {key}")
    for key in ("title", "detail"):
        if key in item and not _text(item[key]):
            errors.append(f"{where}: {key} vuoto")
    if "status" in item and item["status"] not in STATUSES:
        errors.append(f"{where}: stato {item['status']!r} non ammesso")
    if "owner" in item and item["owner"] not in OWNERS:
        errors.append(f"{where}: chi {item['owner']!r} non ammesso")
    if "kind" in item and item["kind"] not in KINDS:
        errors.append(f"{where}: tipo {item['kind']!r} non ammesso")
    if "date" in item and _date(item["date"]) is None:
        errors.append(f"{where}: data {item['date']!r} non valida (AAAA-MM-GG)")
    refs = item.get("refs", [])
    if not isinstance(refs, list) or not all(_text(r) for r in refs):
        errors.append(f"{where}: refs deve essere un elenco di percorsi")
    if item.get("kind") == DRILL and "date" not in item:
        errors.append(f"{where}: un'esercitazione ha sempre la data")


def _check_stage(n: int, stage: Any, ids: set[str], errors: list[str]) -> None:
    where = f"tappa {n + 1}"
    if not isinstance(stage, dict):
        errors.append(f"{where}: non è un oggetto")
        return
    where = f"tappa {stage.get('id', n + 1)}"
    for key in _STAGE:
        if key not in stage:
            errors.append(f"{where}: manca {key}")
    for key in ("id", "title", "what", "why"):
        if key in stage and not _text(stage[key]):
            errors.append(f"{where}: {key} vuoto")
    if stage.get("id") in ids:
        errors.append(f"{where}: id ripetuto")
    ids.add(stage.get("id"))
    if "status" in stage and stage["status"] not in STATUSES:
        errors.append(f"{where}: stato {stage['status']!r} non ammesso")
    if "owner" in stage and stage["owner"] not in OWNERS:
        errors.append(f"{where}: chi {stage['owner']!r} non ammesso")
    start = _date(stage.get("start"))
    if stage.get("start") is not None and start is None:
        errors.append(f"{where}: inizio {stage['start']!r} non valido (AAAA-MM-GG)")
    if stage.get("end") is not None:
        end = _date(stage["end"])
        if end is None:
            errors.append(f"{where}: fine {stage['end']!r} non valida (AAAA-MM-GG)")
        elif start is not None and end < start:
            errors.append(f"{where}: fine prima dell'inizio")
    items = stage.get("items")
    if "items" in stage and (not isinstance(items, list) or not items):
        errors.append(f"{where}: items deve essere un elenco non vuoto")
        return
    for k, item in enumerate(items or []):
        _check_item(f"{where}, voce {k + 1}", item, errors)


def validate_roadmap(data: Any) -> list[str]:
    """The problems of a roadmap (empty when valid), in Italian, naming the place."""
    if not isinstance(data, dict):
        return ["la roadmap non è un oggetto"]
    errors = [f"manca {key}" for key in _TOP if key not in data]
    for key in ("updated", "go_no_go"):
        if key in data and _date(data[key]) is None:
            errors.append(f"{key}: data {data[key]!r} non valida (AAAA-MM-GG)")
    if "summary" in data and not _text(data["summary"]):
        errors.append("summary vuoto")
    c = data.get("campaign")
    if "campaign" in data:
        if not isinstance(c, dict) or _date(c.get("start")) is None or _date(c.get("end")) is None:
            errors.append("campaign: servono start ed end (AAAA-MM-GG)")
        elif _date(c["end"]) < _date(c["start"]):
            errors.append("campaign: fine prima dell'inizio")
        elif not isinstance(c.get("goal"), dict) or not _text(c["goal"].get("label")):
            errors.append("campaign: manca goal.label")
    ids: set[str] = set()
    stages = data.get("stages", [])
    if not isinstance(stages, list) or ("stages" in data and not stages):
        errors.append("stages deve essere un elenco non vuoto")
        stages = []
    for n, stage in enumerate(stages):
        _check_stage(n, stage, ids, errors)
    glossary = data.get("glossary", [])
    if not isinstance(glossary, list):
        errors.append("glossario: deve essere un elenco")
        glossary = []
    for n, g in enumerate(glossary):
        if not isinstance(g, dict) or not _text(g.get("term")) or not _text(g.get("meaning")):
            errors.append(f"glossario, voce {n + 1}: servono term e meaning")
    return errors


def stale_warnings(data: dict, today: date) -> list[str]:
    """What the file still calls «prossimo»/«futuro» although its date has passed (a stage
    that should have started, an item whose day is over): the file needs an update."""
    out = []
    for s in data["stages"]:
        start = _date(s["start"])
        if s["status"] in _NOT_STARTED and start is not None and start < today:
            out.append(f"tappa «{s['title']}»: ancora {s['status']} ma iniziava il {s['start']}")
        end = _date(s.get("end"))
        if s["status"] != "fatto" and end is not None and end < today:
            out.append(f"tappa «{s['title']}»: ancora {s['status']} ma finiva il {s['end']}")
        for i in s["items"]:
            day = _date(i.get("date"))
            if i["status"] in _NOT_STARTED and day is not None and day < today:
                out.append(f"voce «{i['title']}»: ancora {i['status']} ma era il {i['date']}")
    return out


def all_refs(data: dict) -> set[str]:
    return {r for s in data["stages"] for i in s["items"] for r in i["refs"]}


@lru_cache(maxsize=1)
def _load_text() -> str:
    return (resources.files("smtrading.roadmap") / "roadmap.json").read_text("utf-8")


def load_roadmap() -> dict:
    """The validated roadmap file (a fresh copy each call); RoadmapError when invalid."""
    data = json.loads(_load_text())
    errors = validate_roadmap(data)
    if errors:
        raise RoadmapError("roadmap non valida: " + "; ".join(errors))
    return data


# -- live numbers ----------------------------------------------------------------------


def campaign_day(start: date, end: date, today: date) -> tuple[int, int, str]:
    """(day, total days, state): day 1 on `start`, both ends included (the 23/11 → 22/12
    campaign has 30 days), state prima | in_corso | conclusa."""
    total = (end - start).days + 1
    if today < start:
        return 0, total, "prima"
    if today > end:
        return total, total, "conclusa"
    return min((today - start).days + 1, total), total, "in_corso"


def _drills(data: dict) -> list[dict]:
    return sorted((i for s in data["stages"] for i in s["items"] if i.get("kind") == DRILL),
                  key=lambda i: i["date"])


def drills_count(data: dict) -> tuple[int, int]:
    """(done, planned) over the items marked as a drill."""
    drills = _drills(data)
    return sum(1 for i in drills if i["status"] == "fatto"), len(drills)


def neurograph_tools() -> list[str]:
    return sorted(name for name, dest in TOOL_DEST.items() if dest == "ng")


def _pct(raw: Any) -> Decimal | None:
    try:
        return Decimal(str(raw))
    except (InvalidOperation, ValueError):
        return None


async def _campaign_goal(session: AsyncSession, spec: dict) -> Goal | None:
    """The active goal on the total matching the campaign goal (kind and target), the one
    with the same horizon first."""
    target = _pct(spec.get("target_pct"))
    if target is None:
        return None
    goals = list(await session.scalars(
        select(Goal).where(Goal.active.is_(True), Goal.scope == "total",
                           Goal.kind == spec.get("kind", "min"), Goal.target_pct == target)
        .order_by(Goal.created_at, Goal.id)))
    goals.sort(key=lambda g: g.horizon != spec.get("horizon"))
    return goals[0] if goals else None


async def _equity(session: AsyncSession, now: datetime) -> dict | None:
    """The latest snapshot of each portfolio in the totals, summed (EUR)."""
    latest = (select(EquitySnapshot.portfolio_id, func.max(EquitySnapshot.ts).label("ts"))
              .where(EquitySnapshot.portfolio_id.in_(total_portfolio_ids()),
                     EquitySnapshot.ts <= now)
              .group_by(EquitySnapshot.portfolio_id).subquery())
    rows = list(await session.execute(
        select(EquitySnapshot.portfolio_id, EquitySnapshot.equity_eur, EquitySnapshot.ts,
               EquitySnapshot.id)
        .join(latest, (latest.c.portfolio_id == EquitySnapshot.portfolio_id)
              & (latest.c.ts == EquitySnapshot.ts))
        .order_by(EquitySnapshot.portfolio_id, EquitySnapshot.id.desc())))
    seen: dict = {}
    for pid, equity, ts, _ in rows:
        seen.setdefault(pid, (equity, ts))  # two rows at the same instant: the newest id
    if not seen:
        return None
    return {"current_eur": sum((e for e, _ in seen.values()), Decimal(0)).quantize(Decimal("0.01")),
            "as_of": max(ts for _, ts in seen.values()), "portfolios": len(seen)}


async def live_numbers(session: AsyncSession, data: dict, now: datetime,
                       zone: ZoneInfo) -> dict[str, Any]:
    today = now.astimezone(zone).date()
    c = data["campaign"]
    start, end = date.fromisoformat(c["start"]), date.fromisoformat(c["end"])
    day, total, state = campaign_day(start, end, today)
    notes: list[str] = []

    equity = await _equity(session, now)
    if equity is None:
        notes.append("Valore dei portafogli non ancora disponibile: nessuna rilevazione.")

    label = c["goal"]["label"]
    goal = None
    g = await _campaign_goal(session, c["goal"])
    if g is None:
        notes.append(f"Obiettivo «{label}» sul totale non impostato: numeri non disponibili.")
    else:
        p = await goal_progress(session, g, now, zone)
        goal = {"label": label, "name": p["name"], "status": p["status"],
                "start_eur": p["start_eur"], "current_eur": p["current_eur"],
                "target_eur": p["target_eur"], "pct": p["pct"], "target_pct": p["target_pct"],
                "period_start": p["period_start"], "period_end": p["period_end"],
                "as_of": p["as_of"]}
        if p["current_eur"] is None:
            notes.append(f"Obiettivo «{label}»: ancora nessuna rilevazione nel periodo.")

    done, planned = drills_count(data)
    upcoming = [i for i in _drills(data) if i["status"] != "fatto" and i["date"] >= today.isoformat()]
    decision = date.fromisoformat(data["go_no_go"])
    tools = neurograph_tools()
    return {
        "campaign": {"start": c["start"], "end": c["end"], "day": day, "total_days": total,
                     "state": state},
        "equity": equity,
        "goal": goal,
        "drills": {"done": done, "planned": planned,
                   "next": ({"title": upcoming[0]["title"], "date": upcoming[0]["date"]}
                            if upcoming else None)},
        "go_no_go": {"date": data["go_no_go"], "days_left": max((decision - today).days, 0)},
        "neurograph": {"tools": tools, "count": len(tools)},
        "notes": notes,
    }


async def roadmap_view(session: AsyncSession, now: datetime,
                       zone: ZoneInfo | None = None) -> dict[str, Any]:
    """The file plus `live` (Decimals, dates and datetimes: the route makes them JSON);
    "today" in `zone` (default: the configured time zone)."""
    data = load_roadmap()
    zone = zone or ZoneInfo(get_settings().tz)
    return {**data, "live": await live_numbers(session, data, now, zone)}
