"""The sounds of the TV screens: only real events, each with a stable id (the page plays a
sound once per id; it synthesises the sounds itself, Web Audio).

    fill_buy | fill_sell   an order filled (`fill:<id>`), portfolios counted in the totals
    stop | target          the fill of a stop-loss / take-profit (role, or a stop type)
    health_bad             something turned red, once per incident
                           (`health:<key>:<since>:bad`)
    health_ok              the same incident back to normal (`health:<key>:<since>:ok`)

Health incidents (`app_settings["tv_health_incidents"]`, shared by every TV): the
«Salute del sistema» sections in `problema` (key `section:<name>`, every user) and the
river's nodes in `errore` (key `node:<key>`, admins only, like the river). They are
updated when a TV with sounds on polls (from the cached health reads), the last
HISTORY_MAX transitions kept; the first look at each scope (sections, nodes) takes what
is already red as its baseline (no sound for it).

Settings per user (`tv_displays`): on/off (off by default), volume 0–100, quiet hours
«HH:MM»–«HH:MM» in SMT_TZ (default 22:00–08:00): `quiet_now` says whether it is quiet."""

from datetime import datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from smtrading.admin.common import _invalid
from smtrading.common.config import get_settings
from smtrading.engine.models import Fill, Order, OrderType
from smtrading.portfolios.models import total_portfolio_ids
from smtrading.risk.models import AppSetting
from smtrading.tv.models import TvDisplay

INCIDENTS_KEY = "tv_health_incidents"
HISTORY_MAX = 50
EVENTS_MAX = 50
LOOKBACK = timedelta(minutes=10)  # a cursor older than this reads from here
STOP_TYPES = (OrderType.stop, OrderType.stop_limit, OrderType.trailing_stop)
VOLUME_DEFAULT = 40


def clean_hhmm(raw: Any, field: str) -> str | None:
    if raw in (None, ""):
        return None
    try:
        t = time.fromisoformat(str(raw).strip())
    except ValueError:
        raise _invalid(field, "ora non valida: scrivi HH:MM, per esempio 22:00") from None
    return f"{t.hour:02d}:{t.minute:02d}"


def clean_volume(raw: Any) -> int:
    try:
        n = int(raw)
    except (TypeError, ValueError):
        raise _invalid("sound_volume", "volume: un numero da 0 a 100") from None
    if not 0 <= n <= 100:
        raise _invalid("sound_volume", "volume: un numero da 0 a 100")
    return n


def is_quiet(start: str | None, end: str | None, now: datetime, tz: str) -> bool:
    if not start or not end or start == end:
        return False
    local = now.astimezone(ZoneInfo(tz)).strftime("%H:%M")
    return (start <= local < end) if start < end else (local >= start or local < end)


def settings_dto(d: TvDisplay, now: datetime) -> dict[str, Any]:
    tz = get_settings().tz
    return {"enabled": bool(d.sound_enabled), "volume": d.sound_volume,
            "quiet_start": d.quiet_start, "quiet_end": d.quiet_end, "tz": tz,
            "quiet_now": is_quiet(d.quiet_start, d.quiet_end, now, tz)}


async def fill_events(session: AsyncSession, since: datetime, until: datetime) -> list[dict]:
    rows = (await session.execute(
        select(Fill.id, Fill.symbol, Fill.side, Fill.created_at, Order.role, Order.type)
        .join(Order, Order.id == Fill.order_id)
        .where(Fill.created_at > since, Fill.created_at <= until,
               Fill.portfolio_id.in_(total_portfolio_ids()))
        .order_by(Fill.created_at, Fill.id).limit(EVENTS_MAX))).all()
    out = []
    for r in rows:
        if r.role == "take_profit":
            kind = "target"
        elif r.role == "stop_loss" or r.type in STOP_TYPES:
            kind = "stop"
        else:
            kind = f"fill_{r.side}"
        out.append({"id": f"fill:{r.id}", "kind": kind, "side": str(r.side),
                    "symbol": r.symbol, "at": r.created_at})
    return out


def red_keys(health: dict[str, Any], flow_nodes: dict[str, dict] | None) -> dict[str, str]:
    """key -> label of what is red now."""
    out = {f"section:{k}": s.get("label") or k for k, s in (health.get("sections") or {}).items()
           if s.get("status") == "problema"}
    for k, n in (flow_nodes or {}).items():
        if n.get("state") == "errore":
            out[f"node:{k}"] = n.get("label") or k
    return out


def step_incidents(state: dict[str, Any], red: dict[str, str], now: datetime,
                   covered: set[str]) -> bool:
    """Open an incident for each new red key, close the open ones in `covered` scopes
    (`section`, `node`) no longer red; True when something changed. The first look at a
    scope (`state["scopes"]`) is its baseline: what is already red opens silently (the
    sections at a viewer's first poll, the nodes at an admin's)."""
    open_: dict[str, dict] = state.setdefault("open", {})
    history: list[dict] = state.setdefault("history", [])
    seen: list[str] = state.setdefault("scopes", [])
    changed = False
    for key, label in red.items():
        if key not in open_:
            since = now.isoformat()
            open_[key] = {"since": since, "label": label}
            if key.split(":", 1)[0] in seen:
                history.append({"id": f"health:{key}:{since}:bad", "kind": "health_bad",
                                "key": key, "label": label, "at": since})
            changed = True
    for key in [k for k in open_ if k not in red and k.split(":", 1)[0] in covered]:
        e = open_.pop(key)
        if key.split(":", 1)[0] in seen:
            history.append({"id": f"health:{key}:{e['since']}:ok", "kind": "health_ok",
                            "key": key, "label": e.get("label"), "at": now.isoformat()})
        changed = True
    for scope in sorted(covered - set(seen)):
        seen.append(scope)
        changed = True
    state["history"] = history[-HISTORY_MAX:]
    return changed


async def update_incidents(session: AsyncSession, health: dict[str, Any],
                           flow_nodes: dict[str, dict] | None, now: datetime) -> dict:
    row = await session.scalar(
        select(AppSetting).where(AppSetting.key == INCIDENTS_KEY).with_for_update())
    state = dict(row.value) if row is not None and isinstance(row.value, dict) else {}
    state = {"open": dict(state.get("open") or {}), "history": list(state.get("history") or []),
             "scopes": list(state.get("scopes") or [])}
    covered = {"section"} | ({"node"} if flow_nodes is not None else set())
    if step_incidents(state, red_keys(health, flow_nodes), now, covered):
        stmt = insert(AppSetting).values(key=INCIDENTS_KEY, value=state)
        await session.execute(stmt.on_conflict_do_update(index_elements=["key"],
                                                         set_={"value": state}))
    await session.commit()
    return state


def health_events(state: dict[str, Any], since: datetime, until: datetime, *,
                  admin: bool) -> list[dict]:
    out = []
    for h in state.get("history") or []:
        try:
            at = datetime.fromisoformat(h["at"])
        except (KeyError, TypeError, ValueError):
            continue
        if not since < at <= until:
            continue
        if not admin and str(h.get("key", "")).startswith("node:"):
            continue
        out.append({"id": h["id"], "kind": h["kind"], "label": h.get("label"), "at": at})
    return out
