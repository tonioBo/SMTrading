"""The process-wide Trading 212 instrument caches never leak from one test to the next
(tests/conftest.py autouse reset): a test filling them leaves nothing for the next."""

from smtrading.brokers import t212_core

URL = "https://t212-cache-isolation.invalid"


def test_a_fills_the_instrument_cache():
    cache = t212_core.instrument_cache(URL)
    cache.data.update(at=1.0, items=[{"ticker": "ENI_IT_EQ"}])
    cache.reverse["ENI_IT_EQ"] = "ENI.MI"
    t212_core._rate_blocked_until[(URL, "k", "orders")] = 9e18


def test_b_sees_empty_caches():
    for cache in t212_core._instrument_caches.values():
        assert cache.data["items"] is None and cache.data["at"] == 0.0
        assert cache.reverse == {}
    assert t212_core._rate_blocked_until == {}
