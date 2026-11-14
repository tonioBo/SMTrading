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


async def test_settle_retry_runs_at_most_once_a_minute(monkeypatch):
    """Final review M2: the retry pass is bounded (once a minute, not every 5 s tick)."""
    from datetime import UTC, datetime, timedelta

    class _Stop(Exception):
        pass

    def no_db():  # the reconciliation part of the tick is not under test
        raise _Stop

    start = datetime(2026, 11, 14, 12, 0, tzinfo=UTC)
    t = [start]
    calls = []

    async def fake_settle_pass(self):
        calls.append(int((t[0] - start).total_seconds()))

    monkeypatch.setattr(LiveMonitor, "_settle_disarms", fake_settle_pass)
    m = LiveMonitor(sessionmaker=no_db, clock=lambda: t[0])
    for dt in (0, 5, 30, 59, 60, 65, 125):
        t[0] = start + timedelta(seconds=dt)
        try:
            await m.tick()
        except _Stop:
            pass
    assert calls == [0, 60, 125]
