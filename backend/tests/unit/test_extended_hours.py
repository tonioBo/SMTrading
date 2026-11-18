"""extended_hours: US pre-market / after-hours prices, information only."""

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from smtrading.common.errors import ErrorCode, SMTError
from smtrading.instruments.models import AssetClass
from smtrading.marketdata.extended import extended_hours
from smtrading.marketdata.types import ExtendedQuote

D = Decimal
TUE_PRE = datetime(2026, 11, 17, 12, 0, tzinfo=UTC)  # 07:00 New York
TUE_OPEN = datetime(2026, 11, 17, 15, 0, tzinfo=UTC)  # 10:00 New York
TUE_POST = datetime(2026, 11, 17, 22, 0, tzinfo=UTC)  # 17:00 New York
SAT = datetime(2026, 11, 21, 12, 0, tzinfo=UTC)


@dataclass
class Instr:
    symbol: str
    asset_class: AssetClass
    market: str


AAPL = Instr("AAPL", AssetClass.us_equity, "NASDAQ")
ENI = Instr("ENI.MI", AssetClass.eu_equity, "MTA")
BTC = Instr("BTC-USD", AssetClass.crypto, "CRYPTO")


class Router:
    def __init__(self, ext=None, error=False):
        self.ext, self.error, self.calls = ext, error, []

    async def get_extended_quote(self, symbol):
        self.calls.append(symbol)
        if self.error:
            raise SMTError(ErrorCode.PROVIDER_UNAVAILABLE, "down")
        return self.ext


def _ext(session, price, ts):
    return ExtendedQuote("AAPL", session, D(price), D("257.46"), ts, "yahoo")


async def test_pre_market():
    ts = datetime(2026, 11, 17, 11, 45, tzinfo=UTC)
    r = Router(_ext("pre", "259.99", ts))
    out = await extended_hours(r, [AAPL], TUE_PRE)
    assert out == {"AAPL": {"session": "pre", "price": D("259.99"),
                            "change_pct_vs_close": D("0.98"), "ts": ts}}


async def test_after_hours():
    ts = datetime(2026, 11, 17, 21, 45, tzinfo=UTC)
    out = await extended_hours(Router(_ext("post", "255.4", ts)), [AAPL], TUE_POST)
    assert out["AAPL"]["session"] == "post"
    assert out["AAPL"]["change_pct_vs_close"] == D("-0.80")


async def test_weekend_shows_fridays_after_hours():
    ts = datetime(2026, 11, 21, 0, 30, tzinfo=UTC)  # Friday 19:30 New York
    out = await extended_hours(Router(_ext("post", "255.4", ts)), [AAPL], SAT)
    assert out["AAPL"]["session"] == "post" and out["AAPL"]["ts"] == ts


async def test_regular_session_open_is_null_without_a_lookup():
    r = Router(_ext("pre", "259.99", TUE_PRE))
    assert await extended_hours(r, [AAPL], TUE_OPEN) == {"AAPL": None}
    assert r.calls == []


async def test_no_data_or_failure_is_null():
    assert await extended_hours(Router(None), [AAPL], TUE_POST) == {"AAPL": None}
    assert await extended_hours(Router(error=True), [AAPL], TUE_POST) == {"AAPL": None}


async def test_non_us_left_out():
    r = Router(_ext("post", "255.4", TUE_POST))
    assert await extended_hours(r, [ENI, BTC], TUE_POST) == {}
    assert r.calls == []


async def test_router_without_extended_hours():
    assert await extended_hours(object(), [AAPL, BTC], TUE_POST) == {"AAPL": None}
