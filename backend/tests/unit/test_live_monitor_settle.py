"""The live monitor retries failed disarm settlements every tick, alerting once per link
until a pass finds nothing left on it."""

import uuid

from smtrading.brokers.live import monitor as monitor_mod
from smtrading.brokers.live.monitor import LiveMonitor


async def test_settle_retry_alerts_once_per_episode(monkeypatch):
    link = uuid.uuid4()
    results = [[link], [link], [], [link]]
    alerted = []

    async def fake_settle(sm, *, redis=None, alert=None):
        out = results.pop(0)
        for lid in out:
            if alert(lid):
                alerted.append(lid)
        return out

    monkeypatch.setattr(monitor_mod, "settle_disarm_leftovers", fake_settle)
    m = LiveMonitor(sessionmaker=None)
    for _ in range(4):
        await m._settle_disarms()
    assert alerted == [link, link]  # first episode once, the new episode once


async def test_settle_retry_failure_does_not_stop_the_tick(monkeypatch):
    async def boom(sm, **kw):
        raise RuntimeError("db down")

    monkeypatch.setattr(monitor_mod, "settle_disarm_leftovers", boom)
    await LiveMonitor(sessionmaker=None)._settle_disarms()  # logged, no raise
