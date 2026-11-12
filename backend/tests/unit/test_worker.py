import logging
from types import SimpleNamespace

import pytest

from smtrading.worker import main as worker


class _FlakyRedis:
    def __init__(self) -> None:
        self.calls = 0
        self.closed = False

    async def set(self, key, value):
        self.calls += 1
        if self.calls == 1:
            raise ConnectionError("redis down")

    async def aclose(self):
        self.closed = True


class _Stop(Exception):
    pass


async def test_heartbeat_failure_is_logged_and_loop_continues(monkeypatch, caplog):
    fake = _FlakyRedis()
    sleeps: list[float] = []

    async def fake_sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) == 2:
            raise _Stop

    monkeypatch.setattr(
        worker, "get_settings", lambda: SimpleNamespace(redis_url="redis://unused:6379/0")
    )
    monkeypatch.setattr(worker.Redis, "from_url", staticmethod(lambda url: fake))
    monkeypatch.setattr(worker.asyncio, "sleep", fake_sleep)
    with caplog.at_level(logging.WARNING, logger="smtrading.worker"), pytest.raises(_Stop):
        await worker.run()
    assert fake.calls == 2
    assert sleeps == [worker.HEARTBEAT_INTERVAL_S] * 2
    assert fake.closed
    assert any("heartbeat" in r.getMessage() for r in caplog.records)
