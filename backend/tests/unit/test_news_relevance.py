"""news_relevance: does a news text name the instrument its (bare) ticker tag points at?
Cases are real items Alpaca's feed (fiscal.ai) tagged with a catalog ticker, 2026-11."""

import pytest

from smtrading.marketdata.news_relevance import mentions, name_key


@pytest.mark.parametrize(
    ("name", "key"),
    [
        ("Invesco QQQ Trust", ("invesco", "qqq")),
        ("Amazon.com, Inc.", ("amazon",)),
        ("The Walt Disney Company", ("walt",)),
        ("Bank of America Corporation", ("bank", "of", "america")),
        ("State Street Consumer Discretionary Select Sector SPDR ETF", ("state", "street")),
        ("AT&T Inc.", ("at&t",)),
        ("GE Aerospace", ("ge", "aerospace")),
        ("Mondelēz International, Inc.", ("mondelez",)),
        ("Inc.", ()),
    ],
)
def test_name_key(name, key):
    assert name_key(name) == key


@pytest.mark.parametrize(
    ("symbol", "name", "text"),
    [
        # the Invesco QQQ ETF itself, by ticker or by fund name
        ("QQQ", "Invesco QQQ Trust", "Invesco QQQ Is 14% Below Its October Record"),
        ("QQQ", "Invesco QQQ Trust", "$1 Million In QQQ Spent 503 Days Underwater"),
        ("XLY", "State Street Consumer Discretionary Select Sector SPDR ETF",
         "Flows into XLY rise as retail rebounds"),
        ("AMZN", "Amazon.com, Inc.", "Teamsters Rally Following Amazon's Termination"),
        ("BMY", "Bristol-Myers Squibb Company", "Mubadala Dissolves Stake in Bristol Myers Squibb"),
        ("MDLZ", "Mondelez International, Inc.", "Mondelēz International Announces Ritz Return"),
        ("XOM", "ExxonMobil Holdings Corporation", "DZ Bank Downgrades Exxon Mobil to Sell"),
        ("UNH", "UnitedHealth Group Incorporated", "UnitedHealthcare Expands Program"),
        ("BAC", "Bank of America Corporation", "Bank of America Releases Business Owner Report"),
        ("GOOG", "Alphabet Inc.", "Google DeepMind Introduces Gemini 4"),
        ("T", "AT&T Inc.", "AT&T expands fiber footprint"),
        ("T", "AT&T Inc.", "Verizon and AT&T (T) shares slip"),
        ("BRK-B", "Berkshire Hathaway Inc.", "BRK.B hits a record"),
    ],
)
def test_about_the_instrument(symbol, name, text):
    assert mentions(text, symbol, name)


@pytest.mark.parametrize(
    ("symbol", "name", "text"),
    [
        # same bare ticker, another listing (CSE / TSX / LSE / ...)
        ("QQQ", "Invesco QQQ Trust",
         ("Questcorp Mining and Riverside Resources Commence Phase 2B Exploration at La Union "
          "Project Questcorp Mining Inc. and its joint venture partner have initiated the Phase "
          "2B exploration program")),
        ("T", "AT&T Inc.", "TELUS Confirms Receipt of Unsolicited Acquisition Proposal"),
        ("XLY", "State Street Consumer Discretionary Select Sector SPDR ETF",
         "Auxly Cannabis Group Reports Third Quarter 2026 Financial Results"),
        ("APP", "AppLovin Corporation", "Global Compliance Applications Corp. Closes Tranche"),
        ("ROP", "Roper Technologies, Inc.", "Roche Receives FDA Approval for Gazyva"),
        ("DIA", "State Street SPDR Dow Jones Industrial Average ETF Trust",
         "Deutsche Bank Raises Target Price on DiaSorin"),
        ("META", "Meta Platforms, Inc.", "Inderes Lowers Price Target for Metacon"),
        # a lowercase word or a one-letter word is not the ticker
        ("QQQ", "Invesco QQQ Trust", "qqq"),
        ("T", "AT&T Inc.", "Series T notes priced"),
        ("X", "", "anything"),
    ],
)
def test_about_another_company(symbol, name, text):
    assert not mentions(text, symbol, name)
