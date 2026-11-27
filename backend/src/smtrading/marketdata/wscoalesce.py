"""Stream sockets read by their own task, quotes coalesced per symbol.

A stream's consumer (StreamManager: the quality guard and the cache write per tick) can be
slower than a burst of frames (the US open). Reading a socket only as fast as the consumer
fills websockets' incoming queue; reading then pauses, pongs go unread and the client
keepalive drops the connection ("keepalive ping timeout", live 23/11 about once a minute
from 14:30 UTC), with every quote delivered late meanwhile.

`coalesced` drains the socket continuously in a reader task and keeps only the latest
quote per symbol (memory bounded by the symbol set); the generator yields what is pending,
each symbol at most once per batch, freshest data. A consumer that keeps up sees every tick.
1-minute bars (StreamBar) on the same socket are keyed by symbol and bar time, so none is
ever coalesced away.
The reader's errors (the handler's StreamRefused / ConnectionError, websockets' own) are
raised by the generator after the quotes already read; closing the generator cancels the
reader.
"""

import asyncio
from collections.abc import AsyncIterable, AsyncIterator, Awaitable, Callable

from smtrading.marketdata.minute_bars import StreamBar
from smtrading.marketdata.types import Quote

# keepalive stays on (a dead socket is still noticed) but tolerant of a busy event loop
PING_TIMEOUT_S = 60


def _key(item: Quote | StreamBar) -> str | tuple[str, str, object]:
    if isinstance(item, StreamBar):
        return ("b", item.symbol, item.bar.ts)
    return item.symbol


async def coalesced(
    ws: AsyncIterable,
    handle: Callable[[str | bytes], Awaitable[list[Quote] | list[Quote | StreamBar]]],
) -> AsyncIterator[Quote | StreamBar]:
    """`handle` parses one frame into quotes and bars (it may reply on the socket, e.g.
    auth); the quotes it returns must be complete for their symbol (merged state, not
    deltas)."""
    pending: dict[str | tuple[str, str, object], Quote | StreamBar] = {}
    ready = asyncio.Event()

    async def read() -> None:
        try:
            async for raw in ws:
                for item in await handle(raw):
                    pending[_key(item)] = item
                ready.set()
                await asyncio.sleep(0)  # let a waiting consumer take it: no coalescing if it keeps up
        finally:
            ready.set()

    reader = asyncio.create_task(read())
    try:
        while True:
            if not pending:
                if reader.done():
                    reader.result()  # the reader's error, if any
                    return
                await ready.wait()
                ready.clear()
                continue
            batch = list(pending.values())
            pending.clear()
            for item in batch:
                yield item
    finally:
        reader.cancel()
        await asyncio.wait([reader])  # never raises the reader's error; our cancel propagates
        if not reader.cancelled():
            reader.exception()  # retrieved: no «exception was never retrieved» on close
