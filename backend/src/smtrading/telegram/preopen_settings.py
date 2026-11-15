"""The setting of the pre-open check: `app_settings["preopen_check"]` = {"on", "at"},
default on at 08:45 local time. Kept apart from `telegram.preopen`, so the admin API reads
and validates it without importing the worker side (brokers, market data)."""

import re
from dataclasses import dataclass
from datetime import time
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from smtrading.common.errors import ErrorCode, SMTError
from smtrading.risk.models import AppSetting

SETTINGS_KEY = "preopen_check"
DEFAULT_AT = time(8, 45)
_AT = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")


@dataclass(frozen=True)
class PreopenSettings:
    on: bool = True
    at: time = DEFAULT_AT


def _invalid(message: str) -> SMTError:
    return SMTError(ErrorCode.INVALID_ORDER, message, {"field": "preopen_check"})


def _parse_at(value: Any) -> time:
    match = _AT.match(value) if isinstance(value, str) else None
    if match is None:
        raise _invalid("controllo pre-apertura: orario non valido (HH:MM)")
    return time(int(match[1]), int(match[2]))


def _stored(raw: Any) -> PreopenSettings:
    if not isinstance(raw, dict):
        return PreopenSettings()
    try:
        at = _parse_at(raw.get("at", "08:45"))
    except SMTError:
        at = DEFAULT_AT
    return PreopenSettings(on=bool(raw.get("on", True)), at=at)


async def load_preopen_settings(session: AsyncSession) -> PreopenSettings:
    return _stored(await session.scalar(
        select(AppSetting.value).where(AppSetting.key == SETTINGS_KEY)))


def preopen_dto(raw: Any) -> dict[str, Any]:
    """The admin view of the stored value: {"on": bool, "at": "08:45"}."""
    s = _stored(raw)
    return {"on": s.on, "at": s.at.strftime("%H:%M")}


def clean_preopen(body: dict[str, Any], current: dict[str, Any]) -> dict[str, Any]:
    """`current` (a `preopen_dto`) with the fields of `body` validated and applied."""
    out = dict(current)
    if body.get("on") is not None:
        if not isinstance(body["on"], bool):
            raise _invalid("controllo pre-apertura: on deve essere vero o falso")
        out["on"] = body["on"]
    if body.get("at") is not None:
        out["at"] = _parse_at(body["at"]).strftime("%H:%M")
    return out


