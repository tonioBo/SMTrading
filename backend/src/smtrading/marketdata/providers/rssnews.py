"""Crypto news from the public CoinDesk and Cointelegraph RSS 2.0 feeds (no key).

The feeds are parsed with the stdlib: a document over MAX_FEED_BYTES or declaring XML
entities is refused before parsing (no entity expansion). Items are matched to symbols by
word over headline and summary: the ticker and the coin's name (BTC → bitcoin|btc,
ETH → ethereum|ether|eth, others → ticker and CoinGecko-style name when known).
"""

import asyncio
import re
import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from hashlib import sha256
from html.parser import HTMLParser
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx

from smtrading.instruments.models import AssetClass
from smtrading.marketdata.provider import DataProvider
from smtrading.marketdata.providers.coingecko import DEFAULT_IDS
from smtrading.marketdata.registry import register
from smtrading.marketdata.types import Capability, NewsItem

COINDESK_URL = "https://www.coindesk.com/arc/outboundfeeds/rss/"
COINTELEGRAPH_URL = "https://cointelegraph.com/rss"
FEEDS = (("CoinDesk", COINDESK_URL), ("Cointelegraph", COINTELEGRAPH_URL))
MAX_FEED_BYTES = 2 * 1024 * 1024
MAX_SUMMARY_CHARS = 1000

# names besides the CoinGecko id (spaces for dashes: "usd-coin" → "usd coin")
_EXTRA_NAMES = {"ETH": ("ether",)}


class _FeedError(Exception):
    pass


class _Text(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


def _strip_html(raw: str | None) -> str | None:
    if not raw:
        return None
    parser = _Text()
    parser.feed(raw)
    parser.close()
    text = " ".join("".join(parser.parts).split())
    if len(text) > MAX_SUMMARY_CHARS:
        text = text[: MAX_SUMMARY_CHARS - 1].rstrip() + "…"
    return text or None


def _clean_link(raw: str | None) -> str | None:
    """The article URL without fragment and `utm_*` tracking parameters: the same story
    linked from two feeds (or with different campaigns) dedupes to one item."""
    link = (raw or "").strip()
    if not link.startswith(("http://", "https://")):
        return None
    parts = urlsplit(link)
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
             if not k.lower().startswith("utm_")]
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), ""))


def _pub_ts(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        ts = parsedate_to_datetime(raw.strip())
    except (TypeError, ValueError, IndexError):
        return None
    if ts is None:
        return None
    return ts.replace(tzinfo=UTC) if ts.tzinfo is None else ts.astimezone(UTC)


def _terms(base: str) -> re.Pattern[str]:
    words = {base.lower()}
    if (coin_id := DEFAULT_IDS.get(base)) is not None:
        words.add(coin_id.replace("-", " "))
    words.update(_EXTRA_NAMES.get(base, ()))
    alt = "|".join(re.escape(w) for w in sorted(words, key=len, reverse=True))
    # whole words only («MegaETH» is not ETH); a plural or possessive still matches
    return re.compile(rf"(?<!\w)(?:{alt})s?(?!\w)", re.IGNORECASE)


@register
class RssNewsProvider(DataProvider):
    name = "rssnews"
    capabilities = frozenset({Capability.news})
    asset_classes = frozenset({AssetClass.crypto})
    requires_key = False
    default_rate_limit_per_min = 30

    def _default_symbol(self, symbol: str, asset_class: AssetClass) -> str:
        base, sep, quote = symbol.partition("-")
        if asset_class != AssetClass.crypto or not sep or not base or not quote:
            raise self._unsupported(f"symbol {symbol}")
        return base.upper()

    async def _download(self, url: str) -> bytes:
        try:
            async with self.http.stream("GET", url, follow_redirects=True) as resp:
                if resp.status_code != 200:
                    raise _FeedError(f"HTTP {resp.status_code}")
                body = bytearray()
                async for chunk in resp.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > MAX_FEED_BYTES:
                        raise _FeedError("feed too large")
                return bytes(body)
        except httpx.RequestError as exc:
            raise _FeedError(f"request failed: {type(exc).__name__}") from None

    @staticmethod
    def _parse(body: bytes) -> list[ET.Element]:
        if b"<!ENTITY" in body:
            raise _FeedError("entity declarations not allowed")
        try:
            root = ET.fromstring(body)
        except ET.ParseError:
            raise _FeedError("malformed XML") from None
        channel = root.find("channel") if root.tag == "rss" else None
        if channel is None:
            raise _FeedError("not an RSS 2.0 feed")
        return channel.findall("item")

    async def _feed(self, source: str, url: str) -> list[NewsItem]:
        items = []
        for el in self._parse(await self._download(url)):
            headline = " ".join((el.findtext("title") or "").split())
            link = _clean_link(el.findtext("link"))
            ts = _pub_ts(el.findtext("pubDate"))
            if not headline or link is None or ts is None:
                continue  # never fabricate an id or a timestamp
            items.append(NewsItem(
                id=sha256(link.encode()).hexdigest()[:32],
                ts=ts,
                headline=headline,
                summary=_strip_html(el.findtext("description")),
                url=link,
                source=source,
                symbols=(),
            ))
        return items

    async def _all_feeds(self) -> list[NewsItem]:
        results = await asyncio.gather(
            *(self._feed(source, url) for source, url in FEEDS), return_exceptions=True
        )
        items: list[NewsItem] = []
        errors = []
        for (source, _), res in zip(FEEDS, results, strict=True):
            if isinstance(res, _FeedError):
                errors.append(f"{source}: {res}")
            elif isinstance(res, BaseException):
                raise res
            else:
                items.extend(res)
        if len(errors) == len(FEEDS):
            raise RuntimeError(f"rssnews: {'; '.join(errors)}")
        return items

    async def get_news(self, symbols: list[tuple[str, str]], limit: int) -> list[NewsItem]:
        patterns = [(canonical, _terms(ps.upper())) for canonical, ps in symbols]
        out: dict[str, NewsItem] = {}
        seen: set[str] = set()
        for item in await self._all_feeds():
            if item.id in seen:
                continue  # the same link in both feeds: the first feed's copy
            seen.add(item.id)
            text = f"{item.headline}\n{item.summary or ''}"
            matched = tuple(c for c, pattern in patterns if pattern.search(text))
            if matched:
                out[item.id] = NewsItem(**{**item.__dict__, "symbols": matched})
        return sorted(out.values(), key=lambda n: n.ts, reverse=True)[:limit]

    async def health_check(self) -> None:
        source, url = FEEDS[0]
        try:
            self._parse(await self._download(url))
        except _FeedError as exc:
            raise RuntimeError(f"rssnews: {source}: {exc}") from None
