"""_YF transport: one shared HTTP/1.1 curl_cffi session and one retry on transient errors.
No network: yfinance entry points are replaced with fakes."""

import threading
from datetime import UTC, datetime
from typing import ClassVar

import pandas as pd
import pytest
import yfinance as yf
from curl_cffi import CurlHttpVersion
from curl_cffi import requests as curl_requests
from curl_cffi.const import CurlECode
from curl_cffi.requests.exceptions import HTTPError

from smtrading.marketdata.providers import yahoo
from smtrading.marketdata.providers.yahoo import _YF

START, END = datetime(2026, 1, 1, tzinfo=UTC), datetime(2026, 1, 3, tzinfo=UTC)


def _df() -> pd.DataFrame:
    idx = pd.DatetimeIndex([pd.Timestamp("2026-01-02 00:00", tz="America/New_York")])
    return pd.DataFrame(
        {"Open": [1.0], "High": [2.0], "Low": [0.5], "Close": [1.5], "Volume": [10]}, index=idx
    )


def _http2_error() -> HTTPError:
    return HTTPError(
        "Failed to perform, curl: (16) Error in the HTTP2 framing layer", CurlECode.HTTP2
    )


class FakeTicker:
    """Each construction pops the next scripted outcome for the called attribute."""

    script: ClassVar[dict[str, list]] = {}
    made: ClassVar[list[tuple[str, object]]] = []

    def __init__(self, sym, session=None):
        FakeTicker.made.append((sym, session))

    @staticmethod
    def _next(name):
        out = FakeTicker.script[name].pop(0)
        if isinstance(out, BaseException):
            raise out
        return out

    @property
    def fast_info(self):
        return self._next("fast_info")

    def history(self, **kw):
        return self._next("history")

    @property
    def info(self):
        return self._next("info")


@pytest.fixture
def fake(monkeypatch):
    FakeTicker.script, FakeTicker.made = {}, []
    monkeypatch.setattr(yf, "Ticker", FakeTicker)
    monkeypatch.setattr(yahoo, "_RETRY_DELAY_S", 0)
    return FakeTicker


def test_session_is_http11_chrome_and_shared_across_threads():
    s = yahoo._session()
    assert isinstance(s, curl_requests.Session)
    assert s.http_version == CurlHttpVersion.V1_1
    assert s.impersonate == "chrome"
    other: list = []
    t = threading.Thread(target=lambda: other.append(yahoo._session()))
    t.start()
    t.join()
    # yfinance keeps one process-wide YfData singleton: a per-thread session would keep
    # swapping its session (and cookie jar) under concurrent calls, so one is shared.
    assert other == [s] and yahoo._session() is s


def test_every_ticker_gets_the_shared_session(fake):
    fake.script = {"fast_info": [{"lastPrice": 1.0}], "history": [_df()], "info": [{"a": 1}]}
    y = _YF()
    y.fast_info("SPY")
    y.history("SPY", "1d", START, END)
    y.info("SPY")
    assert [s for _, s in fake.made] == [yahoo._session()] * 3


def test_history_retries_once_on_empty_then_succeeds(fake):
    fake.script = {"history": [pd.DataFrame(), _df()]}
    rows = _YF().history("SPY", "1d", START, END)
    assert len(rows) == 1 and rows[0]["close"] == 1.5
    assert len(fake.made) == 2  # a fresh Ticker: yfinance caches per Ticker object


def test_history_unknown_symbol_still_empty_after_one_retry(fake):
    fake.script = {"history": [pd.DataFrame(), pd.DataFrame()]}
    assert _YF().history("NOPE", "1d", START, END) == []
    assert len(fake.made) == 2


def test_fast_info_retries_once_when_price_missing(fake):
    fake.script = {"fast_info": [{"lastPrice": None}, {"lastPrice": 5.0}]}
    assert _YF().fast_info("SPY")["lastPrice"] == 5.0


def test_fast_info_unknown_symbol_returns_no_price(fake):
    fake.script = {"fast_info": [{"lastPrice": None}, {"lastPrice": None}]}
    assert _YF().fast_info("NOPE")["lastPrice"] is None
    assert len(fake.made) == 2


def test_info_retries_once_on_transient_curl_error(fake):
    fake.script = {"info": [_http2_error(), {"longName": "SPDR"}]}
    assert _YF().info("SPY") == {"longName": "SPDR"}


def test_transient_error_twice_is_raised(fake):
    fake.script = {"info": [_http2_error(), _http2_error()]}
    with pytest.raises(HTTPError):
        _YF().info("SPY")


def test_non_transient_errors_are_not_retried(fake):
    fake.script = {"info": [HTTPError("404 Not Found", 0), {"x": 1}]}
    with pytest.raises(HTTPError):
        _YF().info("NOPE")
    fake.script = {"info": [KeyError("x"), {"x": 1}]}
    with pytest.raises(KeyError):
        _YF().info("NOPE")
    assert len(fake.made) == 2


def test_search_and_screen_use_the_shared_session(monkeypatch):
    seen: dict = {}

    class FakeSearch:
        def __init__(self, q, session=None, **kw):
            seen["search"] = session
            self.quotes = []

    def fake_screen(query, size=None, session=None, **kw):
        seen["screen"] = session
        return {"quotes": []}

    monkeypatch.setattr(yf, "Search", FakeSearch)
    monkeypatch.setattr(yf, "screen", fake_screen)
    y = _YF()
    y.search("spy")
    y.screen({"query": {"operator": "EQ", "operands": ["region", "us"]}, "limit": 5})
    assert seen == {"search": yahoo._session(), "screen": yahoo._session()}


def test_session_does_not_enable_yfinance_sqlite_caches(monkeypatch):
    calls: list = []
    monkeypatch.setattr(yf, "set_tz_cache_location", lambda d: calls.append(d))
    monkeypatch.setattr(yahoo, "_shared_session", None)
    yahoo._session()
    assert calls == []  # sqlite caches lock up under concurrent threads ("database is locked")
