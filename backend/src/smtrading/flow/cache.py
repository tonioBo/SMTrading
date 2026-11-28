"""A small in-process cache with a time to live for the «Fiume dei flussi» reads that
cost something (Paperclip, the node health): one build per key at a time (the others wait
for it and share its result), failures cached like successes (a slow Paperclip is asked
at most once per TTL). Per api process; `clear_all` for the tests."""

import asyncio
import time
from collections.abc import Awaitable, Callable, Hashable
from typing import Any

_ALL: list["TtlCache"] = []


class TtlCache:
    def __init__(self, ttl_s: float, *, clock: Callable[[], float] = time.monotonic) -> None:
        self.ttl_s = ttl_s
        self._clock = clock
        self._values: dict[Hashable, tuple[float, Any]] = {}
        self._locks: dict[Hashable, asyncio.Lock] = {}
        _ALL.append(self)

    async def get(self, key: Hashable, build: Callable[[], Awaitable[Any]]) -> Any:
        hit = self._fresh(key)
        if hit is not None:
            return hit[1]
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            hit = self._fresh(key)
            if hit is not None:
                return hit[1]
            value = await build()
            self._values[key] = (self._clock(), value)
            return value

    def _fresh(self, key: Hashable) -> tuple[float, Any] | None:
        hit = self._values.get(key)
        if hit is not None and self._clock() - hit[0] < self.ttl_s:
            return hit
        return None

    def clear(self) -> None:
        self._values.clear()
        self._locks.clear()


def clear_all() -> None:
    for c in _ALL:
        c.clear()
