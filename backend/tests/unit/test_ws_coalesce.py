"""The socket is read by its own task, quotes coalesced per symbol (live 23/11 from the US
open: a consumer slower than the opening burst filled websockets' queue, reading paused,
pongs went unread and the client keepalive dropped the Alpaca socket about once a minute)."""

import asyncio
import contextlib
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from smtrading.marketdata.types import Quote
from smtrading.marketdata.wscoalesce import coalesced

T0 = datetime(2026, 11, 23, 14, 30, tzinfo=UTC)


def _q(symbol: str, i: int) -> Quote:
    return Quote(symbol=symbol, bid=None, ask=None, last=Decimal(i), ts=T0 + timedelta(seconds=i),
                 provider="fake")


class FakeWs:
    """An async iterable socket: a burst of frames, then `end` (stay open, raise, close)."""

    def __init__(self, frames: list, end: BaseException | None = None, stay_open: bool = False):
        self.frames, self.end, self.stay_open = frames, end, stay_open
        self.read = 0
        self.cancelled = False
        self.blocked = asyncio.Event()  # read everything, waiting for more

    def __aiter__(self):
        return self._gen()

    async def _gen(self):
        try:
            for f in self.frames:
                self.read += 1
                yield f
            if self.end is not None:
                raise self.end
            if self.stay_open:
                self.blocked.set()
                await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise


async def _parse(raw):
    return [_q(s, i) for s, i in raw]


async def test_slow_consumer_sees_the_latest_quote_per_symbol_not_the_backlog():
    n = 500
    ws = FakeWs([[("AAPL", i), ("SPY", i)] for i in range(n)], stay_open=True)
    seen: list[Quote] = []

    async def consume():
        async with contextlib.aclosing(coalesced(ws, _parse)) as gen:
            async for q in gen:
                seen.append(q)
                await asyncio.sleep(0.01)  # slow: the guard and the cache write per tick
                if {s.symbol: s.last for s in seen} == {"AAPL": n - 1, "SPY": n - 1}:
                    return

    await asyncio.wait_for(consume(), 5)
    assert ws.read == n  # the reader kept reading while the consumer was busy
    assert len(seen) < n  # no backlog: intermediate ticks coalesced away
    latest = {q.symbol: q.last for q in seen}
    assert latest == {"AAPL": Decimal(n - 1), "SPY": Decimal(n - 1)}
    for sym in ("AAPL", "SPY"):  # per symbol, never older than what came before
        lasts = [q.last for q in seen if q.symbol == sym]
        assert lasts == sorted(lasts)


async def test_fast_consumer_sees_every_tick_in_order():
    ws = FakeWs([[("AAPL", 1)], [("AAPL", 2)], [("SPY", 3)]])
    out = [(q.symbol, q.last) async for q in coalesced(ws, _parse)]
    assert out == [("AAPL", 1), ("AAPL", 2), ("SPY", 3)]


async def test_a_batch_carries_each_symbol_once():
    ws = FakeWs([[("AAPL", 1), ("SPY", 1), ("AAPL", 2)]])
    out = [(q.symbol, q.last) async for q in coalesced(ws, _parse)]
    assert out == [("AAPL", 2), ("SPY", 1)]


async def test_reader_error_propagates_after_the_pending_quotes():
    ws = FakeWs([[("AAPL", 1)]], end=ConnectionError("boom"))
    out = []
    with pytest.raises(ConnectionError, match="boom"):
        async for q in coalesced(ws, _parse):
            out.append(q.last)
    assert out == [1]


async def test_handler_error_propagates_with_its_type():
    class Refused(ConnectionError):
        pass

    async def handle(raw):
        raise Refused("406")

    with pytest.raises(Refused, match="406"):
        async for _ in coalesced(FakeWs([["x"]], stay_open=True), handle):
            pass


async def test_socket_end_ends_the_generator():
    assert [q async for q in coalesced(FakeWs([]), _parse)] == []


async def test_closing_the_generator_cancels_the_reader_promptly():
    ws = FakeWs([[("AAPL", 1)]], stay_open=True)
    gen = coalesced(ws, _parse)
    assert (await anext(gen)).last == 1
    await asyncio.wait_for(ws.blocked.wait(), 1)
    await asyncio.wait_for(gen.aclose(), 1)
    assert ws.cancelled


async def test_cancelling_the_consumer_cancels_the_reader():
    ws = FakeWs([], stay_open=True)

    async def consume():
        async with contextlib.aclosing(coalesced(ws, _parse)) as gen:
            async for _ in gen:
                pass

    task = asyncio.create_task(consume())
    await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 1)
    assert ws.cancelled
