from smtrading.instruments.taxonomy import (
    EQUITY_SECTORS,
    SECTORS,
    index_codes_matching,
    italian_sector,
)


def test_sectors():
    assert len(EQUITY_SECTORS) == 11 and SECTORS[-3:] == ("ETF", "Crypto", "Cambi")
    # GICS (S&P 500 list) and Yahoo names land on the same Italian sector
    assert italian_sector("Information Technology") == italian_sector("Technology") == \
        "Tecnologia"
    assert italian_sector("Consumer Discretionary") == italian_sector("Consumer Cyclical") == \
        "Beni voluttuari"
    assert italian_sector("Consumer Staples") == italian_sector("Consumer Defensive") == \
        "Beni di prima necessità"
    assert italian_sector(" healthcare ") == "Sanità"
    assert italian_sector("Financial Services") == "Finanza"
    assert italian_sector(None) is None and italian_sector("Bonds") is None


def test_index_codes_matching():
    assert index_codes_matching("s&p") == ["SP500"]
    assert index_codes_matching("S&P 500") == ["SP500"]
    assert index_codes_matching("nasdaq") == ["NDX"]
    assert index_codes_matching("mib") == ["FTSEMIB"]
    assert index_codes_matching("dax") == ["DAX", "MDAX"]
    assert index_codes_matching("cac") == ["CAC40"]
    assert index_codes_matching("a") == [] and index_codes_matching("apple") == []
