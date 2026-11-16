from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path

import httpx
import pytest
import respx

import smtrading.marketdata.providers  # noqa: F401
from smtrading.instruments.models import AssetClass
from smtrading.marketdata.provider import ProviderUnsupported
from smtrading.marketdata.providers.rssnews import (
    COINDESK_URL,
    COINTELEGRAPH_URL,
    MAX_FEED_BYTES,
    RssNewsProvider,
)
from smtrading.marketdata.registry import PROVIDERS
from smtrading.marketdata.types import Capability

FIX = Path(__file__).parents[2] / "fixtures" / "marketdata"
COINDESK = (FIX / "rss_coindesk.xml").read_bytes()
COINTELEGRAPH = (FIX / "rss_cointelegraph.xml").read_bytes()


def _feed(*items: str) -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8"?><rss version="2.0"><channel><title>x</title>'
        + "".join(items)
        + "</channel></rss>"
    )


def _item(title: str, link: str | None, pub: str | None, desc: str = "") -> str:
    parts = [f"<title>{title}</title>"]
    if link is not None:
        parts.append(f"<link>{link}</link>")
    if pub is not None:
        parts.append(f"<pubDate>{pub}</pubDate>")
    parts.append(f"<description><![CDATA[{desc}]]></description>")
    return "<item>" + "".join(parts) + "</item>"


@pytest.fixture
async def provider():
    async with httpx.AsyncClient() as http:
        yield RssNewsProvider({}, http)


@pytest.fixture
def feeds():
    with respx.mock(assert_all_called=False) as mock:
        cd = mock.get(COINDESK_URL).respond(200, content=COINDESK)
        ct = mock.get(COINTELEGRAPH_URL).respond(200, content=COINTELEGRAPH)
        yield mock, cd, ct


def test_registered_keyless_news_for_crypto_only():
    assert PROVIDERS["rssnews"] is RssNewsProvider
    assert RssNewsProvider.capabilities == frozenset({Capability.news})
    assert RssNewsProvider.asset_classes == frozenset({AssetClass.crypto})
    assert RssNewsProvider.requires_key is False
    assert RssNewsProvider.config_fields == ()


def test_map_symbol(provider):
    assert provider.map_symbol("BTC-USD", AssetClass.crypto, None) == "BTC"
    assert provider.map_symbol("eth-eur", AssetClass.crypto, None) == "ETH"
    assert provider.map_symbol("BTC-USD", AssetClass.crypto, "XBT") == "XBT"
    for sym, ac in (("AAPL", AssetClass.us_equity), ("EURUSD", AssetClass.forex), ("BTC", AssetClass.crypto)):
        with pytest.raises(ProviderUnsupported):
            provider.map_symbol(sym, ac, None)


async def test_bitcoin_news_from_both_feeds_newest_first(provider, feeds):
    news = await provider.get_news([("BTC-USD", "BTC")], 20)
    assert [(n.source, n.headline) for n in news] == [
        ("CoinDesk", "Bitcoin was supposed to gain as AI faltered, but it crashed. Here's why."),
        ("CoinDesk", "Bitcoin slips after rebounding from July 14 low that triggered $2 billion of liquidations"),
        ("Cointelegraph", "BTC price tags 18-week low into Nvidia earnings: Three things to know in Bitcoin this week"),
        ("Cointelegraph", "Harvard, Dartmouth keep Bitcoin ETF stakes steady as fund gains 42% in Q3"),
        ("CoinDesk", "A behind the scenes force may amplify bitcoin and ether price volatility"),
    ]
    assert all(n.symbols == ("BTC-USD",) for n in news)
    assert [n.ts for n in news] == sorted((n.ts for n in news), reverse=True)
    # the CoinDesk item whose only "bitcoin" is in the URL slug is not about BTC
    assert not any("OKX" in n.headline for n in news)


async def test_item_mapping(provider, feeds):
    news = await provider.get_news([("BTC-USD", "BTC")], 20)
    ct = next(n for n in news if n.source == "Cointelegraph")
    url = "https://cointelegraph.com/markets/btc-price-18-week-low-nvidia-three-things-know-bitcoin-this-week"
    assert ct.url == url  # tracking parameters dropped
    assert ct.id == sha256(url.encode()).hexdigest()[:32]
    assert ct.ts == datetime(2026, 11, 16, 10, 39, 5, tzinfo=UTC)
    # HTML stripped (the cover <img> and its alt text are gone), entities decoded
    assert ct.summary == (
        "Bitcoin traders awaited Nvidia Q3 earnings after BTC price selling pressure saw a dip "
        "below $62,000 at the weekend."
    )
    cd = next(n for n in news if n.source == "CoinDesk")
    assert cd.url == (
        "https://www.coindesk.com/daybook-us/2026/11/16/"
        "bitcoin-was-supposed-to-gain-as-ai-faltered-but-it-crashed-here-s-why"
    )
    assert cd.summary.startswith("The price action runs counter")


async def test_ether_terms_with_word_boundaries(provider, feeds):
    news = await provider.get_news([("ETH-USD", "ETH")], 20)
    assert [n.headline for n in news] == [
        "Harvard, Dartmouth keep Bitcoin ETF stakes steady as fund gains 42% in Q3",
        "Vitalik Buterin flips to ‘total bull’ on a faster route to quantum-safe Ethereum",
        "A behind the scenes force may amplify bitcoin and ether price volatility",
    ]  # «MegaETH» is not ETH


async def test_other_coins_by_ticker_and_name(provider, feeds):
    sol = await provider.get_news([("SOL-USD", "SOL")], 20)
    assert [n.headline[:7] for n in sol] == ["Harvard"]  # «Solana» in the summary
    ada = await provider.get_news([("ADA-EUR", "ADA")], 20)
    assert [n.headline[:7] for n in ada] == ["Cardano"]
    xrp = await provider.get_news([("XRP-USD", "XRP")], 20)
    assert [n.headline[:3] for n in xrp] == ["XRP"]
    assert await provider.get_news([("PEPE-USD", "PEPE")], 20) == []


async def test_multiple_symbols_share_items_and_limit(provider, feeds):
    news = await provider.get_news([("BTC-USD", "BTC"), ("ETH-USD", "ETH")], 20)
    both = next(n for n in news if n.headline.startswith("A behind"))
    assert both.symbols == ("BTC-USD", "ETH-USD")
    assert len({n.id for n in news}) == len(news) == 6
    top = await provider.get_news([("BTC-USD", "BTC"), ("ETH-USD", "ETH")], 2)
    assert top == news[:2]


@respx.mock
async def test_duplicate_links_across_feeds_kept_once(provider):
    link = "https://example.com/a?utm_source=rss&amp;id=7"
    respx.get(COINDESK_URL).respond(200, text=_feed(
        _item("Bitcoin up", link + "#top", "Mon, 16 Nov 2026 10:00:00 +0000")))
    respx.get(COINTELEGRAPH_URL).respond(200, text=_feed(
        _item("Bitcoin up again", link + "&amp;utm_medium=x", "Mon, 16 Nov 2026 11:00:00 +0000")))
    news = await provider.get_news([("BTC-USD", "BTC")], 20)
    assert len(news) == 1
    assert news[0].url == "https://example.com/a?id=7"
    assert news[0].source == "CoinDesk"  # the first feed's copy


@respx.mock
async def test_items_without_date_link_or_title_are_skipped(provider):
    respx.get(COINDESK_URL).respond(200, text=_feed(
        _item("Bitcoin no date", "https://e.com/1", None),
        _item("Bitcoin bad date", "https://e.com/2", "yesterday"),
        _item("Bitcoin no link", None, "Mon, 16 Nov 2026 10:00:00 +0000"),
        _item("", "https://e.com/3", "Mon, 16 Nov 2026 10:00:00 +0000", "bitcoin"),
        _item("Bitcoin naive date", "https://e.com/4", "Mon, 16 Nov 2026 10:00:00 -0000"),
        _item("Bitcoin ok", "https://e.com/5", "Mon, 16 Nov 2026 09:00:00 +0100", "<b>hi</b> &amp; bye"),
    ))
    respx.get(COINTELEGRAPH_URL).respond(200, text=_feed())
    news = await provider.get_news([("BTC-USD", "BTC")], 20)
    assert [(n.headline, n.ts) for n in news] == [
        ("Bitcoin naive date", datetime(2026, 11, 16, 10, tzinfo=UTC)),
        ("Bitcoin ok", datetime(2026, 11, 16, 8, tzinfo=UTC)),
    ]
    assert news[1].summary == "hi & bye"
    assert news[0].summary is None


@pytest.mark.parametrize("bad", [
    {"text": "<rss><channel><item>"},  # malformed
    {"text": "<html><body>not a feed</body></html>"},  # not RSS
    {"status_code": 503},
    {"content": b"<rss>" + b" " * (MAX_FEED_BYTES + 1) + b"</rss>"},  # oversized
    {"text": '<?xml version="1.0"?><!DOCTYPE r [<!ENTITY a "bitcoin">]>'
             "<rss><channel><item><title>&a;</title></item></channel></rss>"},
])
@respx.mock
async def test_one_broken_feed_still_returns_the_other(provider, bad):
    respx.get(COINDESK_URL).respond(**({"status_code": 200} | bad))
    respx.get(COINTELEGRAPH_URL).respond(200, content=COINTELEGRAPH)
    news = await provider.get_news([("BTC-USD", "BTC")], 20)
    assert {n.source for n in news} == {"Cointelegraph"} and len(news) == 2


@respx.mock
async def test_all_feeds_broken_raises(provider):
    respx.get(COINDESK_URL).respond(500)
    respx.get(COINTELEGRAPH_URL).mock(side_effect=httpx.ConnectError("boom"))
    with pytest.raises(RuntimeError, match="rssnews"):
        await provider.get_news([("BTC-USD", "BTC")], 20)


async def test_health_check_fetches_one_feed(provider, feeds):
    _, cd, ct = feeds
    await provider.health_check()
    assert cd.call_count == 1 and ct.call_count == 0


@respx.mock
async def test_health_check_fails_on_bad_feed(provider):
    respx.get(COINDESK_URL).respond(200, text="<html/>")
    with pytest.raises(RuntimeError, match="rssnews"):
        await provider.health_check()
