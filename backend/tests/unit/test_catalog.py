import csv
import re
from decimal import Decimal

import pytest

from smtrading.instruments.catalog import CATALOG_DIR, load_catalog, parse_row
from smtrading.instruments.market_hours import MARKETS
from smtrading.instruments.models import AssetClass
from smtrading.instruments.taxonomy import EQUITY_SECTORS, ETF_TYPES, INDICES, SECTORS

ROWS = load_catalog()
_SUFFIX_MARKET = {".MI": "MTA", ".DE": "XETRA", ".PA": "EURONEXT_PARIS"}


def test_catalog_size_and_unique_symbols():
    assert len(ROWS) >= 900
    assert len({r.symbol for r in ROWS}) == len(ROWS)
    by_class = {ac: sum(r.asset_class == ac for r in ROWS) for ac in AssetClass}
    assert by_class[AssetClass.us_equity] >= 580 and by_class[AssetClass.eu_equity] >= 220
    assert by_class[AssetClass.crypto] >= 60 and by_class[AssetClass.forex] >= 20
    by_market = {m: sum(r.market == m for r in ROWS) for m in MARKETS}
    # S&P 500 + Nasdaq-100 + ETFs; FTSE MIB, DAX 40 + MDAX majors + UCITS ETFs, CAC 40
    assert by_market["NYSE"] + by_market["NASDAQ"] >= 580
    assert by_market["MTA"] >= 50 and by_market["XETRA"] >= 115
    assert by_market["EURONEXT_PARIS"] >= 48


@pytest.mark.parametrize("path", sorted(CATALOG_DIR.glob("*.csv")), ids=lambda p: p.name)
def test_catalog_files_sorted_by_symbol(path):
    with path.open(newline="", encoding="utf-8") as f:
        symbols = [r["symbol"] for r in csv.DictReader(f)]
    assert symbols == sorted(symbols)


def test_catalog_covers_indices_and_etfs():
    symbols = {r.symbol for r in ROWS}
    sample = {
        "BRK-B", "BF-B", "JPM", "WMT", "NVDA", "COST",  # S&P 500 / Nasdaq-100
        "IVV", "VTV", "SCHD", "AGG", "BND", "TQQQ", "SQQQ", "GLD",  # US ETFs
        "SWDA.MI", "CSSPX.MI", "CW8.PA", "MEUD.PA",  # UCITS ETFs
        "BMPS.MI", "HEI.DE", "SY1.DE", "LHA.DE", "MC.PA", "ENX.PA",  # FTSE MIB/DAX/MDAX/CAC
        "BTC-USD", "HYPE-USD", "ONDO-USD",  # crypto
    }
    assert sample <= symbols, sorted(sample - symbols)


def test_catalog_provider_overrides():
    for row in ROWS:
        if row.asset_class == AssetClass.us_equity and "-" in row.symbol:  # share classes
            dotted = row.symbol.replace("-", ".")
            assert {row.provider_symbols.get(p) for p in ("alpaca", "finnhub", "twelvedata")} == {
                dotted}, row.symbol
        if row.asset_class == AssetClass.crypto:  # CoinGecko needs the coin id
            assert row.provider_symbols.get("coingecko", "").endswith(":usd"), row.symbol


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
                       ("provider_symbols", "alpaca"), ("sector", "Technology"),
                       ("indices", "SP500;XX"), ("indices", "DAX;DAX"),
                       ("industry", "x" * 129)]:
        with pytest.raises(ValueError):
            parse_row({**good, field: bad}, "t")


def test_load_catalog_rejects_duplicates(tmp_path):
    header = ("symbol,name,market,asset_class,currency,lot_size,tick_size,shortable,"
              "provider_symbols,sector,industry,indices")
    line = "AAPL,Apple,NASDAQ,us_equity,USD,1,0.01,true,,Tecnologia,,SP500;NDX"
    (tmp_path / "a.csv").write_text(f"{header}\n{line}\n")
    (tmp_path / "b.csv").write_text(f"{header}\n{line}\n")
    with pytest.raises(ValueError, match="duplicate"):
        load_catalog.__wrapped__(tmp_path)
    (tmp_path / "b.csv").write_text("symbol,name\nX,Y\n")
    with pytest.raises(ValueError, match="columns"):
        load_catalog.__wrapped__(tmp_path)


def test_parse_row_metadata():
    good = {
        "symbol": "AAPL", "name": "Apple", "market": "NASDAQ", "asset_class": "us_equity",
        "currency": "USD", "lot_size": "1", "tick_size": "0.01", "shortable": "true",
        "provider_symbols": "", "sector": "Tecnologia", "industry": "Consumer Electronics",
        "indices": "SP500;NDX",
    }
    row = parse_row(good, "t")
    assert (row.sector, row.industry, row.indices) == (
        "Tecnologia", "Consumer Electronics", ("SP500", "NDX"))
    assert row.values()["indices"] == ["SP500", "NDX"]
    bare = parse_row({**good, "sector": "", "industry": "", "indices": ""}, "t")
    assert (bare.sector, bare.industry, bare.indices) == (None, None, ())


def test_catalog_sectors_and_indices():
    """Every row has a sector; stocks a GICS-like one and an industry, ETFs an asset
    type; the index lists are complete (S&P 500 has 503 lines: share classes)."""
    for r in ROWS:
        assert r.sector in SECTORS, r.symbol
        if r.asset_class == AssetClass.crypto:
            assert r.sector == "Crypto"
        elif r.asset_class == AssetClass.forex:
            assert r.sector == "Cambi"
        elif r.sector == "ETF":
            assert r.industry in ETF_TYPES, r.symbol
            assert not r.indices, r.symbol
        else:
            assert r.sector in EQUITY_SECTORS and r.industry, r.symbol
    count = {c: sum(c in r.indices for r in ROWS) for c in INDICES}
    assert count["SP500"] >= 500 and count["NDX"] >= 100
    assert count["FTSEMIB"] == 40 and count["DAX"] == 40 and count["CAC40"] >= 39
    assert count["MDAX"] >= 40
    by = {r.symbol: r for r in ROWS}
    assert by["AAPL"].indices == ("SP500", "NDX") and by["AAPL"].sector == "Tecnologia"
    assert by["ISP.MI"].indices == ("FTSEMIB",) and by["ISP.MI"].sector == "Finanza"
    assert by["SAP.DE"].indices == ("DAX",) and by["MC.PA"].indices == ("CAC40",)
    assert by["TQQQ"].industry == "Leva e inverso" and by["GLD"].industry == "Materie prime"
    assert by["AGG"].industry == "Obbligazionario" and by["SWDA.MI"].industry == "Azionario"
