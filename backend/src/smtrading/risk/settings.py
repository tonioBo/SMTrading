"""Runtime risk settings (app_settings / system_state), with built-in defaults."""

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from smtrading.instruments.models import AssetClass
from smtrading.risk.models import AppSetting, SystemState

DEFAULT_STALE_QUOTE_S = {"us_equity": 120, "eu_equity": 1200, "crypto": 60, "forex": 600}
DEFAULT_SPREAD_BPS = {"us_equity": 2, "eu_equity": 5, "crypto": 5, "forex": 1}
KILL_SWITCH_KEY = "kill_switch"


async def _per_class(
    session: AsyncSession, key: str, asset_class: AssetClass | str, defaults: dict[str, int]
) -> int:
    cls = str(asset_class)
    value = await session.scalar(select(AppSetting.value).where(AppSetting.key == key))
    if isinstance(value, dict) and cls in value:
        return int(value[cls])
    return defaults[cls]


async def get_stale_threshold(session: AsyncSession, asset_class: AssetClass | str) -> int:
    return await _per_class(session, "stale_quote_s", asset_class, DEFAULT_STALE_QUOTE_S)


async def get_default_spread_bps(session: AsyncSession, asset_class: AssetClass | str) -> int:
    return await _per_class(session, "default_spread_bps", asset_class, DEFAULT_SPREAD_BPS)


async def is_kill_switch_on(session: AsyncSession) -> bool:
    value = await session.scalar(
        select(SystemState.value).where(SystemState.key == KILL_SWITCH_KEY)
    )
    return isinstance(value, dict) and bool(value.get("on", False))


async def set_kill_switch(session: AsyncSession, on: bool) -> None:
    value = {"on": bool(on)}
    stmt = insert(SystemState).values(key=KILL_SWITCH_KEY, value=value)
    await session.execute(
        stmt.on_conflict_do_update(
            index_elements=["key"], set_={"value": value, "updated_at": func.now()}
        )
    )
