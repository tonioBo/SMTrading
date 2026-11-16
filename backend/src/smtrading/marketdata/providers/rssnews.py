"""Crypto news from the public CoinDesk and Cointelegraph RSS 2.0 feeds (no key).

The feeds are parsed with the stdlib: a document over MAX_FEED_BYTES, declaring XML
entities or carrying an internal DTD subset is refused before the tree is built (no
entity expansion): an expat pass whose declaration handlers refuse, whatever the
document's encoding. Items are matched to symbols by
word over headline and summary: the ticker and the coin's name (BTC → bitcoin|BTC,
ETH → ethereum|ether|ETH, others → ticker and CoinGecko-style name when known). Names
match in any case; tickers of up to 4 letters only in UPPERCASE.
"""

import asyncio
import re
import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from hashlib import sha256
from html.parser import HTMLParser
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from xml.parsers import expat

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
    """The text of an HTML fragment, without the content of <script> and <style>."""

    _HIDDEN = frozenset({"script", "style"})

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._hidden = 0

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag in self._HIDDEN:
            self._hidden += 1

    def handle_endtag(self, tag: str) -> None:
        if tag in self._HIDDEN and self._hidden:
            self._hidden -= 1

    def handle_data(self, data: str) -> None:
        if not self._hidden:
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
        if ts is None:
            return None
        return ts.replace(tzinfo=UTC) if ts.tzinfo is None else ts.astimezone(UTC)
    except (TypeError, ValueError, IndexError, OverflowError):
        return None  # the item is skipped, the feed is not


class _Declaration(Exception):
    pass


def _refuse_declarations(body: bytes) -> None:
    """An expat pass over the raw bytes (expat decodes them: UTF-16 included) refusing
    any entity declaration and any internal DTD subset (and malformed XML)."""

    def doctype(_name, _system_id, _public_id, has_internal_subset) -> None:
        if has_internal_subset:
            raise _Declaration("DTD internal subset not allowed")

    def entity(*_args) -> None:
        raise _Declaration("entity declarations not allowed")

    parser = expat.ParserCreate()
    parser.SetParamEntityParsing(expat.XML_PARAM_ENTITY_PARSING_NEVER)
    parser.StartDoctypeDeclHandler = doctype
    parser.EntityDeclHandler = entity
    parser.UnparsedEntityDeclHandler = entity
    try:
        parser.Parse(body, True)
    except _Declaration as exc:
        raise _FeedError(str(exc)) from None
    except expat.ExpatError:
        raise _FeedError("malformed XML") from None


# tickers this short are matched only as written in UPPERCASE («sol», «ada» are words)
SHORT_TICKER_LEN = 4


def _terms(base: str) -> re.Pattern[str]:
    names = set(_EXTRA_NAMES.get(base, ()))
    if (coin_id := DEFAULT_IDS.get(base)) is not None:
        names.add(coin_id.replace("-", " "))
    ticker = re.escape(base)
    if len(base) > SHORT_TICKER_LEN:
        names.add(base.lower())
        ticker = ""
    alts = []
    if names:
        alt = "|".join(re.escape(w) for w in sorted(names, key=len, reverse=True))
        alts.append(f"(?i:{alt})s?")
    if ticker:
        alts.append(f"{ticker}s?")
    # whole words only («MegaETH» is not ETH); a plural or possessive still matches
    return re.compile(rf"(?<!\w)(?:{'|'.join(alts)})(?!\w)")


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
        _refuse_declarations(body)
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
