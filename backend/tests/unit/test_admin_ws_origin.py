"""The admin WebSocket Origin check (Ruling R8) behind the https LAN entry (R12)."""

from types import SimpleNamespace

import pytest

from smtrading.admin.ws import _origin_ok


def _ws(headers: dict) -> SimpleNamespace:
    return SimpleNamespace(headers=headers)


@pytest.mark.parametrize(("origin", "host", "ok"), [
    (None, "192.168.30.7", True),  # not a browser
    ("https://192.168.30.7", "192.168.30.7", True),
    ("https://192.168.30.7", "192.168.30.7:443", True),
    ("https://192.168.30.7:443", "192.168.30.7", True),
    ("http://127.0.0.1:3000", "127.0.0.1:3000", True),
    ("http://LocalHost", "localhost:80", True),
    ("https://192.168.30.7", "192.168.30.7:80", False),
    ("http://192.168.30.7", "192.168.30.7:443", False),
    ("http://127.0.0.1:3000", "127.0.0.1", False),
    ("https://evil.example", "192.168.30.7", False),
    ("https://192.168.30.7", "", False),
    ("null", "192.168.30.7", False),
    ("ftp://192.168.30.7", "192.168.30.7", False),
])
def test_origin_ok(origin, host, ok):
    headers = {"host": host} if origin is None else {"origin": origin, "host": host}
    assert _origin_ok(_ws(headers)) is ok


async def test_archived_cache_reads_the_database_once_per_ttl():
    """M14 (carried): relays share one archived set; it is read at most once per TTL."""
    from smtrading.admin.ws import ArchivedCache

    reads = []

    class _Db:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def scalars(self, stmt):
            reads.append(stmt)
            return ["p1"]

    cache = ArchivedCache(ttl_s=60)
    assert await cache.get(_Db) == {"p1"}
    assert await cache.get(_Db) == {"p1"}
    assert len(reads) == 1
    cache.note("p2", True)
    assert await cache.get(_Db) == {"p1", "p2"}
    cache.note("p1", False)
    assert await cache.get(_Db) == {"p2"} and len(reads) == 1
    expired = ArchivedCache(ttl_s=0)
    await expired.get(_Db)
    await expired.get(_Db)
    assert len(reads) == 3
