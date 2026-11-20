"""The setting of the weekly PDF report: `app_settings["weekly_report_pdf"]` =
{"on", "weekday", "at"}, default on, Friday (weekday 4, Monday = 0) at 22:30 local time
(after the US close and the PM's 22:15 cycle). Kept apart from `telegram.weekly_report`,
so the admin API reads and validates it without importing the report and PDF code."""

import re
from dataclasses import dataclass
from datetime import time
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from smtrading.common.errors import ErrorCode, SMTError
from smtrading.risk.models import AppSetting

SETTINGS_KEY = "weekly_report_pdf"
DEFAULT_WEEKDAY = 4
DEFAULT_AT = time(22, 30)
_AT = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")


@dataclass(frozen=True)
class WeeklyReportSettings:
    on: bool = True
    weekday: int = DEFAULT_WEEKDAY
    at: time = DEFAULT_AT


def _invalid(message: str) -> SMTError:
    return SMTError(ErrorCode.INVALID_ORDER, message, {"field": SETTINGS_KEY})


def _parse_at(value: Any) -> time:
    match = _AT.match(value) if isinstance(value, str) else None
    if match is None:
        raise _invalid("resoconto settimanale: orario non valido (HH:MM)")
    return time(int(match[1]), int(match[2]))


def _parse_weekday(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 6:
        raise _invalid("resoconto settimanale: giorno non valido (0 = lunedì … 6 = domenica)")
    return value


def _stored(raw: Any) -> WeeklyReportSettings:
    if not isinstance(raw, dict):
        return WeeklyReportSettings()
    try:
        at = _parse_at(raw.get("at", "22:30"))
    except SMTError:
        at = DEFAULT_AT
    try:
        weekday = _parse_weekday(raw.get("weekday", DEFAULT_WEEKDAY))
    except SMTError:
        weekday = DEFAULT_WEEKDAY
    return WeeklyReportSettings(on=bool(raw.get("on", True)), weekday=weekday, at=at)


async def load_weekly_report_settings(session: AsyncSession) -> WeeklyReportSettings:
    return _stored(await session.scalar(
        select(AppSetting.value).where(AppSetting.key == SETTINGS_KEY)))


def weekly_report_dto(raw: Any) -> dict[str, Any]:
    """The admin view of the stored value: {"on": bool, "weekday": 4, "at": "22:30"}."""
    s = _stored(raw)
    return {"on": s.on, "weekday": s.weekday, "at": s.at.strftime("%H:%M")}


def clean_weekly_report(body: dict[str, Any], current: dict[str, Any]) -> dict[str, Any]:
    """`current` (a `weekly_report_dto`) with the fields of `body` validated and applied."""
    out = dict(current)
    if body.get("on") is not None:
        if not isinstance(body["on"], bool):
            raise _invalid("resoconto settimanale: on deve essere vero o falso")
        out["on"] = body["on"]
    if body.get("weekday") is not None:
        out["weekday"] = _parse_weekday(body["weekday"])
    if body.get("at") is not None:
        out["at"] = _parse_at(body["at"]).strftime("%H:%M")
    return out
