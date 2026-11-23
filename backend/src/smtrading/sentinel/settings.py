"""The sentinel's setting and stored state, readable without the worker side (the admin
API and «Salute del sistema»): `app_settings["system_sentinel"]` = {"on"} (default on)
and `app_settings["system_sentinel_state"]` (the open problems, `sentinel.engine`)."""

from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from smtrading.common.errors import ErrorCode, SMTError
from smtrading.risk.models import AppSetting
from smtrading.sentinel import engine

SETTINGS_KEY = "system_sentinel"
STATE_KEY = "system_sentinel_state"
LATE_AFTER = timedelta(minutes=15)  # no pass for this long: the sentinel is stopped


def _on(raw: Any) -> bool:
    return bool(raw.get("on", True)) if isinstance(raw, dict) else True


async def sentinel_on(session: AsyncSession) -> bool:
    return _on(await session.scalar(
        select(AppSetting.value).where(AppSetting.key == SETTINGS_KEY)))


def sentinel_dto(raw: Any) -> dict[str, Any]:
    return {"on": _on(raw)}


def clean_sentinel(body: dict[str, Any], current: dict[str, Any]) -> dict[str, Any]:
    out = dict(current)
    if body.get("on") is not None:
        if not isinstance(body["on"], bool):
            raise SMTError(ErrorCode.INVALID_ORDER,
                           "sentinella di sistema: on deve essere vero o falso",
                           {"field": SETTINGS_KEY})
        out["on"] = body["on"]
    return out


async def load_state(session: AsyncSession) -> dict[str, Any]:
    return engine.load(await session.scalar(
        select(AppSetting.value).where(AppSetting.key == STATE_KEY)))


def sentinel_view(on: bool, state: dict[str, Any], now: datetime) -> dict[str, Any]:
    """«Salute del sistema»: the open problems and the last pass. Informative: it does not
    count in the page's overall status (the sentinel alerts on that status itself; a
    stopped sentinel shows as a late job under «Servizi»)."""
    last = engine._ts(state.get("last_pass_at"))
    opened = [{"key": k, "kind": e.get("kind"),
               "label": engine.KIND_LABELS.get(e.get("kind") or "", e.get("kind")),
               "title": e.get("title"), "detail": e.get("detail") or "",
               "level": e.get("level"), "since": e.get("since")}
              for k, e in sorted((state.get("open") or {}).items(),
                                 key=lambda kv: kv[1].get("since") or "")]
    week = [h for h in state.get("history") or []
            if (engine._ts(h.get("at")) or now) >= now - engine.WEEK]
    data = {"on": on, "last_pass_at": state.get("last_pass_at"),
            "last_error": state.get("last_error"), "open": opened, "alerts_7d": len(week),
            "label": "Sentinella di sistema"}
    if not on:
        return {"status": "spento", "summary": "Sentinella spenta: nessun avviso automatico",
                "reasons": [], **data}
    reasons: list[str] = []
    status = "ok"
    if last is None:
        status = "attenzione"
        reasons.append("la sentinella non ha ancora fatto un giro")
    elif now - last > LATE_AFTER:
        status = "attenzione"
        reasons.append(f"ultimo giro della sentinella {engine.ago(now - last)} fa")
    if opened:
        status = "attenzione"
        reasons += [f"{o['title']}" for o in opened[:10]]
    if state.get("last_error"):
        reasons.append(f"controlli non riusciti: {state['last_error']}")
    if opened:
        summary = f"{len(opened)} problemi aperti" if len(opened) > 1 else "1 problema aperto"
    elif status == "ok":
        summary = "Nessun problema aperto"
    else:
        summary = reasons[0]
    return {"status": status, "summary": summary, "reasons": reasons, **data}
