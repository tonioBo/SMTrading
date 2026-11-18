"""The setting of the hand-off watchdog: `app_settings["handoff_watchdog"]` = {"on"},
default on (it also needs the Paperclip reader enabled). Kept apart from
`paperclip.watchdog`, so the admin API reads and validates it without the worker side."""

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from smtrading.common.errors import ErrorCode, SMTError
from smtrading.risk.models import AppSetting

SETTINGS_KEY = "handoff_watchdog"


def _on(raw: Any) -> bool:
    return bool(raw.get("on", True)) if isinstance(raw, dict) else True


async def watchdog_on(session: AsyncSession) -> bool:
    return _on(await session.scalar(
        select(AppSetting.value).where(AppSetting.key == SETTINGS_KEY)))


def watchdog_dto(raw: Any) -> dict[str, Any]:
    """The admin view of the stored value: {"on": bool}."""
    return {"on": _on(raw)}


def clean_watchdog(body: dict[str, Any], current: dict[str, Any]) -> dict[str, Any]:
    out = dict(current)
    if body.get("on") is not None:
        if not isinstance(body["on"], bool):
            raise SMTError(ErrorCode.INVALID_ORDER,
                           "controllo dei passaggi: on deve essere vero o falso",
                           {"field": "handoff_watchdog"})
        out["on"] = body["on"]
    return out
