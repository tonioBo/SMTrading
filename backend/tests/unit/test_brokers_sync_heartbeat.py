"""BrokerSync's heartbeat with Redis down: one warning when it goes down, not one per
pass; one info line when it is back. A fake Redis only."""
import logging

from smtrading.brokers.sync import BrokerSync


class _Redis:
    def __init__(self):
        self.down = True
        self.written = 0

    async def set(self, *args, **kwargs):
        if self.down:
            raise ConnectionError("redis down")
        self.written += 1


async def test_heartbeat_warning_throttled_while_redis_is_down(caplog):
    redis = _Redis()
    sync = BrokerSync(None, redis, None, None, interval_s=1)
    caplog.set_level(logging.INFO, logger="smtrading.brokers.sync")

    def lines(level):
        return [r for r in caplog.records
                if r.levelno == level and "heartbeat" in r.getMessage()]

    for _ in range(5):
        await sync._heartbeat()
    assert len(lines(logging.WARNING)) == 1
    redis.down = False
    await sync._heartbeat()
    await sync._heartbeat()
    assert redis.written == 2
    assert len(lines(logging.INFO)) == 1  # back again, said once
    redis.down = True
    await sync._heartbeat()
    assert len(lines(logging.WARNING)) == 2  # down again: a new warning
