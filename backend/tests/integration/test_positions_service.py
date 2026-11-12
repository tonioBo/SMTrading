from decimal import Decimal as D

from smtrading.engine.models import OrderSide
from smtrading.instruments.models import AssetClass, Instrument
from smtrading.positions.service import get_positions, upsert_position
from smtrading.risk import models as _risk_models  # noqa: F401  (registers risk_profiles for FK resolution)


async def _instr(session, symbol):
    session.add(Instrument(
        symbol=symbol, name=symbol, asset_class=AssetClass.us_equity, market="NASDAQ",
        currency="USD", lot_size=D("0.000001"), tick_size=D("0.01"),
    ))
    await session.flush()


async def test_upsert_creates_updates_and_tracks_opened_at(db_session, portfolio):
    await _instr(db_session, "ZZPOS1")
    pid = portfolio.id
    p = await upsert_position(db_session, pid, "ZZPOS1", OrderSide.buy, D(10), D(100))
    assert (p.qty, p.avg_price, p.realized_pnl) == (10, 100, 0)
    opened = p.opened_at
    assert opened is not None
    p = await upsert_position(db_session, pid, "ZZPOS1", OrderSide.buy, D(10), D(110))
    assert (p.qty, p.avg_price) == (20, 105)
    assert p.opened_at == opened
    p = await upsert_position(db_session, pid, "ZZPOS1", OrderSide.sell, D(20), D(120))
    assert (p.qty, p.avg_price, p.realized_pnl) == (0, 0, 300)
    assert p.opened_at is None
    p = await upsert_position(db_session, pid, "ZZPOS1", OrderSide.sell, D(1), D(120))
    assert p.qty == -1
    assert p.opened_at is not None


async def test_get_positions_only_nonzero(db_session, portfolio):
    await _instr(db_session, "ZZPOS2")
    await _instr(db_session, "ZZPOS3")
    pid = portfolio.id
    await upsert_position(db_session, pid, "ZZPOS2", OrderSide.buy, D(1), D(10))
    await upsert_position(db_session, pid, "ZZPOS3", OrderSide.buy, D(1), D(10))
    await upsert_position(db_session, pid, "ZZPOS3", OrderSide.sell, D(1), D(11))
    got = await get_positions(db_session, pid)
    assert [p.symbol for p in got] == ["ZZPOS2"]
