from sqlalchemy import delete

from smtrading.instruments.models import AssetClass
from smtrading.risk.models import AppSetting, SystemState
from smtrading.risk.settings import (
    get_default_spread_bps,
    get_stale_threshold,
    is_kill_switch_on,
    set_kill_switch,
)


async def _clear(session):
    await session.execute(delete(AppSetting))
    await session.execute(delete(SystemState).where(SystemState.key == "kill_switch"))


async def test_defaults_when_settings_missing(db_session):
    await _clear(db_session)
    assert await get_stale_threshold(db_session, AssetClass.us_equity) == 120
    assert await get_stale_threshold(db_session, AssetClass.eu_equity) == 1200
    assert await get_stale_threshold(db_session, AssetClass.crypto) == 60
    assert await get_stale_threshold(db_session, AssetClass.forex) == 600
    assert await get_default_spread_bps(db_session, AssetClass.us_equity) == 2
    assert await get_default_spread_bps(db_session, AssetClass.eu_equity) == 5
    assert await get_default_spread_bps(db_session, AssetClass.crypto) == 5
    assert await get_default_spread_bps(db_session, AssetClass.forex) == 1
    assert await is_kill_switch_on(db_session) is False


async def test_settings_override_defaults(db_session):
    await _clear(db_session)
    db_session.add(AppSetting(key="stale_quote_s", value={"us_equity": 30}))
    db_session.add(AppSetting(key="default_spread_bps", value={"crypto": 9}))
    await db_session.flush()
    assert await get_stale_threshold(db_session, AssetClass.us_equity) == 30
    assert await get_stale_threshold(db_session, AssetClass.crypto) == 60  # key absent → default
    assert await get_default_spread_bps(db_session, "crypto") == 9


async def test_kill_switch_roundtrip(db_session):
    await _clear(db_session)
    await set_kill_switch(db_session, True)
    assert await is_kill_switch_on(db_session) is True
    await set_kill_switch(db_session, False)
    assert await is_kill_switch_on(db_session) is False
