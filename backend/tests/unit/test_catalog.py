import re
from decimal import Decimal

import pytest

from smtrading.instruments.catalog import load_catalog, parse_row
from smtrading.instruments.market_hours import MARKETS
from smtrading.instruments.models import AssetClass

ROWS = load_catalog()
_SUFFIX_MARKET = {".MI": "MTA", ".DE": "XETRA", ".PA": "EURONEXT_PARIS"}


def test_catalog_size_and_unique_symbols():
    assert len(ROWS) >= 380
    assert len({r.symbol for r in ROWS}) == len(ROWS)
    by_class = {ac: sum(r.asset_class == ac for r in ROWS) for ac in AssetClass}
    assert by_class[AssetClass.us_equity] >= 180 and by_class[AssetClass.eu_equity] >= 120
    assert by_class[AssetClass.crypto] >= 25 and by_class[AssetClass.forex] >= 20


@pytest.mark.parametrize("row", ROWS, ids=lambda r: r.symbol)
def test_catalog_row_is_consistent(row):
    assert row.market in MARKETS
    assert row.lot_size > 0 and row.tick_size > 0
    assert re.fullmatch(r"[A-Z0-9][A-Z0-9.\-]{0,63}", row.symbol)
    if row.asset_class == AssetClass.us_equity:
        assert row.market in ("NASDAQ", "NYSE") and row.currency == "USD" and row.shortable
        assert (row.tick_size, row.lot_size) == (Decimal("0.0001"), Decimal("0.000001"))
    elif row.asset_class == AssetClass.eu_equity:
        assert _SUFFIX_MARKET[row.symbol[-3:]] == row.market and row.currency == "EUR"
        assert (row.tick_size, row.lot_size, row.shortable) == (Decimal("0.0001"), 1, True)
    elif row.asset_class == AssetClass.crypto:
        assert row.market == "CRYPTO" and row.symbol.endswith("-USD") and row.currency == "USD"
        assert row.tick_size == row.lot_size == Decimal("0.00000001") and not row.shortable
        assert not row.symbol.startswith(("USDT-", "USDC-", "DAI-"))  # no stablecoins
    else:
        assert row.market == "FX" and re.fullmatch(r"[A-Z]{6}", row.symbol)
        assert row.currency == row.symbol[3:] and row.lot_size == 1 and not row.shortable
        assert row.tick_size == (Decimal("0.001") if row.currency == "JPY" else Decimal("0.00001"))


def test_parse_row_rejects_bad_values():
    good = {
        "symbol": "AAPL", "name": "Apple", "market": "NASDAQ", "asset_class": "us_equity",
        "currency": "USD", "lot_size": "1", "tick_size": "0.01", "shortable": "true",
        "provider_symbols": "alpaca=AAPL;finnhub=AAPL",
    }
    assert parse_row(good, "t").provider_symbols == {"alpaca": "AAPL", "finnhub": "AAPL"}
    for field, bad in [("market", "LSE"), ("asset_class", "bond"), ("currency", "usd"),
                       ("lot_size", "0"), ("tick_size", "-1"), ("tick_size", "x"),
                       ("shortable", "yes"), ("symbol", "aapl"), ("name", ""),
                       ("provider_symbols", "alpaca")]:
        with pytest.raises(ValueError):
            parse_row({**good, field: bad}, "t")


def test_load_catalog_rejects_duplicates(tmp_path):
    header = "symbol,name,market,asset_class,currency,lot_size,tick_size,shortable,provider_symbols"
    line = "AAPL,Apple,NASDAQ,us_equity,USD,1,0.01,true,"
    (tmp_path / "a.csv").write_text(f"{header}\n{line}\n")
    (tmp_path / "b.csv").write_text(f"{header}\n{line}\n")
    with pytest.raises(ValueError, match="duplicate"):
        load_catalog.__wrapped__(tmp_path)
    (tmp_path / "b.csv").write_text("symbol,name\nX,Y\n")
    with pytest.raises(ValueError, match="columns"):
        load_catalog.__wrapped__(tmp_path)
