"""The Tracker's measures of a closed trade (`Tracker._close`): the gross result counts
at most the trade's own quantity, even when the exits sold more (e.g. a manual close of
a position that also held shares bought before the rule's entry). Fakes only."""
import uuid
from datetime import UTC, datetime
from decimal import Decimal as D
from types import SimpleNamespace

from smtrading.autorules.tracker import Tracker, _Exit, _Fills, _Item, _Snapshot

AT = datetime(2026, 11, 25, 16, 0, tzinfo=UTC)


class _Session:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def commit(self):
        pass

    async def rollback(self):
        pass


class _QS:
    async def get_fx_rate(self, base, quote):
        return D(1)


def _trade(qty="1", fill="100"):
    return SimpleNamespace(id=1, qty=D(qty), fill_price=D(fill), entry_order_id=uuid.uuid4(),
                           exit_order_id=None, portfolio_id=uuid.uuid4(), symbol="SYM")


def _order(filled, price):
    return SimpleNamespace(id=uuid.uuid4(), filled_qty=D(filled), avg_fill_price=D(price),
                           parent_id=None, updated_at=AT)


async def _close(trade, exits):
    tracker = Tracker(lambda: _Session(), None, _QS(), clock=lambda: AT)
    row = SimpleNamespace(**vars(trade), exit_filled_at=None)

    async def locked(s, t, status):
        return row

    tracker._locked = locked
    snap = _Snapshot(items=[], orders={}, fills={e.order.id: _Fills() for e in exits},
                     positions={})
    item = _Item(trade=trade, entry_timeout_s=5, currency="USD", base_currency="USD")
    await tracker._close(item, snap, exits, None)
    return row


async def test_gross_capped_at_the_trade_quantity():
    trade = _trade(qty="1", fill="100")
    row = await _close(trade, [_Exit(_order("3", "110"), "manual", None)])
    assert row.exit_price == D(110)
    assert row.net_result == D(10)  # (110 - 100) x 1, not x 3


async def test_gross_on_the_whole_sold_when_within_the_trade():
    trade = _trade(qty="2", fill="100")
    row = await _close(trade, [_Exit(_order("1", "110"), "time", None),
                               _Exit(_order("1", "120"), "time", None)])
    assert row.exit_price == D(115)
    assert row.net_result == D(30)


async def _track_dust(lot, held):
    """An open ETH trade of 0.11527377 whose time exit sold the lot-floored position
    (0.11498558): `held` is what the position keeps (the coin fee's remainder)."""
    from smtrading.autorules.tables import OPEN
    tracker = Tracker(lambda: _Session(), None, _QS(), clock=lambda: AT)
    trade = SimpleNamespace(**vars(_trade(qty="0.11527377", fill="1735")), status=OPEN)
    exits = [_Exit(_order("0.11498558", "1736"), "time", None)]
    tracker._exits_of = lambda t, snap: exits
    closed = []

    async def close(item, snap, ex, runner):
        closed.append(ex)
        return "closed"

    tracker._close = close
    snap = _Snapshot(items=[], orders={}, fills={},
                     positions={(trade.portfolio_id, trade.symbol): D(held)})
    item = _Item(trade=trade, entry_timeout_s=5, currency="USD", base_currency="USD",
                 lot=None if lot is None else D(lot))
    await tracker._track(item, snap, None, AT)
    return closed


async def test_a_remainder_below_one_lot_closes_the_trade():
    assert await _track_dust("0.00000001", "0.000000005")  # dust: nothing left to sell
    assert await _track_dust("0.00000001", "0")
    assert not await _track_dust("0.00000001", "0.00000002")  # one lot or more: open
    assert not await _track_dust(None, "0.000000005")  # no lot known: as before
