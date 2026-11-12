from datetime import UTC, datetime

import pytest

from smtrading.common.errors import ErrorCode, SMTError
from smtrading.instruments.market_hours import all_market_status, market_status


def utc(*args) -> datetime:
    return datetime(*args, tzinfo=UTC)


def test_xnas_open_midweek():
    s = market_status("NASDAQ", utc(2026, 11, 11, 15, 0))
    assert s.is_open
    assert s.next_close == utc(2026, 11, 11, 21, 0)


def test_xnas_saturday_closed_next_open_monday():
    s = market_status("NASDAQ", utc(2026, 11, 14, 12, 0))
    assert not s.is_open
    assert s.next_open == utc(2026, 11, 16, 14, 30)


def test_xnas_thanksgiving_closed():
    assert not market_status("NYSE", utc(2026, 11, 26, 16, 0)).is_open


def test_xmil_christmas_closed():
    assert not market_status("MTA", utc(2026, 12, 25, 10, 0)).is_open


def test_xetra_open():
    assert market_status("XETRA", utc(2026, 11, 11, 10, 0)).is_open


def test_fx_saturday_closed_sunday_evening_open():
    closed = market_status("FX", utc(2026, 11, 14, 12, 0))
    assert not closed.is_open
    assert closed.next_open == utc(2026, 11, 15, 22, 0)
    assert market_status("FX", utc(2026, 11, 15, 23, 0)).is_open
    assert not market_status("FX", utc(2026, 11, 13, 22, 0)).is_open
    assert market_status("FX", utc(2026, 11, 13, 21, 59)).is_open
    assert market_status("FX", utc(2026, 11, 11, 12, 0)).next_close == utc(2026, 11, 13, 22, 0)


def test_crypto_always_open():
    s = market_status("CRYPTO", utc(2026, 11, 14, 3, 0))
    assert s.is_open and s.next_open is None and s.next_close is None


def test_unknown_market():
    with pytest.raises(SMTError) as e:
        market_status("FOO", utc(2026, 11, 11, 15, 0))
    assert e.value.code == ErrorCode.NOT_FOUND


def test_naive_datetime_rejected():
    with pytest.raises(ValueError):
        market_status("FX", datetime(2026, 11, 11, 12, 0))


def test_all_market_status():
    names = {s.market for s in all_market_status(utc(2026, 11, 11, 15, 0))}
    assert names == {"NASDAQ", "NYSE", "MTA", "XETRA", "CRYPTO", "FX"}
