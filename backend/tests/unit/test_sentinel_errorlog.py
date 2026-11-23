"""Error log rate (sentinel): ERROR/CRITICAL records counted per logger and minute, known
noise excluded, secrets redacted, flushed to Redis and read back over 15 minutes."""

import logging

from smtrading.sentinel import errorlog


class FakeRedis:
    def __init__(self, fail=False):
        self.h: dict[str, dict[str, str]] = {}
        self.ttl: dict[str, int] = {}
        self.fail = fail

    async def hincrby(self, key, field, n):
        if self.fail:
            raise ConnectionError("down")
        d = self.h.setdefault(key, {})
        d[field] = str(int(d.get(field, "0")) + n)

    async def hset(self, key, field, value):
        self.h.setdefault(key, {})[field] = value

    async def expire(self, key, ttl):
        self.ttl[key] = ttl

    async def hgetall(self, key):
        return {k.encode(): v.encode() for k, v in self.h.get(key, {}).items()}


def _record(name, level, msg, created, *args, exc=None):
    r = logging.LogRecord(name, level, __file__, 1, msg, args, exc)
    r.created = created
    return r


T = 1_800_000_000.0  # an epoch second


def test_errors_are_counted_per_logger_and_minute_warnings_and_noise_are_not():
    c = errorlog.ErrorCounter("worker")
    logger = logging.getLogger("sentinel-test")
    logger.addHandler(c)
    try:
        logger.setLevel(logging.DEBUG)
        logger.warning("just a warning")
        logger.error("boom %s", 1)
        logger.critical("very bad")
    finally:
        logger.removeHandler(c)
    c.handle(_record("yfinance", logging.ERROR, "yahoo failed", T))
    c.handle(_record("smtrading.marketdata", logging.ERROR, "finnhub: HTTP 429", T))
    c.handle(_record("smtrading.marketdata", logging.ERROR, "quota esaurita", T))
    counts, last = c.drain()
    assert sum(counts.values()) == 2
    assert {k[1] for k in counts} == {"sentinel-test"}
    assert "very bad" in last.values()
    assert c.drain() == ({}, {})


def test_messages_are_redacted():
    c = errorlog.ErrorCounter("api")
    c.handle(_record("smtrading.x", logging.ERROR, "call failed token=supersecret123", T))
    _, last = c.drain()
    assert list(last.values()) == ["call failed token=<redacted>"]


def test_a_broken_record_never_raises():
    c = errorlog.ErrorCounter("api")
    c.handle(_record("smtrading.x", logging.ERROR, "%d", T, "not a number"))
    assert c.drain()[0] == {}


async def test_flush_and_read_over_the_window():
    redis = FakeRedis()
    c = errorlog.ErrorCounter("worker")
    for i in range(3):
        c.handle(_record("smtrading.engine", logging.ERROR, f"fill failed {i}", T))
    c.handle(_record("smtrading.engine", logging.ERROR, "old", T - 20 * 60))
    await errorlog.flush(redis, c)
    c.handle(_record("smtrading.engine", logging.ERROR, "fill failed 3", T + 60))
    await errorlog.flush(redis, c)
    assert all(ttl == errorlog.TTL_S for ttl in redis.ttl.values())
    counts = await errorlog.read_counts(redis, T + 90)
    assert counts == {("worker", "smtrading.engine"): {"count": 4, "message": "fill failed 3"}}


async def test_a_failed_flush_keeps_the_counts_for_the_next_one():
    redis = FakeRedis(fail=True)
    c = errorlog.ErrorCounter("worker", clock=lambda: T)
    c.handle(_record("smtrading.engine", logging.ERROR, "x", T))
    await errorlog.flush(redis, c)
    redis.fail = False
    await errorlog.flush(redis, c)
    assert (await errorlog.read_counts(redis, T))[("worker", "smtrading.engine")]["count"] == 1


def test_install_is_idempotent_and_removable():
    root = logging.getLogger()
    a = errorlog.install("worker", extra_loggers=("uvicorn-test",))
    try:
        assert errorlog.install("worker") is a
        assert sum(isinstance(h, errorlog.ErrorCounter) for h in root.handlers) == 1
        assert a in logging.getLogger("uvicorn-test").handlers
    finally:
        errorlog.uninstall(a, extra_loggers=("uvicorn-test",))
    assert a not in root.handlers
    assert a not in logging.getLogger("uvicorn-test").handlers
