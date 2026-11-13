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
