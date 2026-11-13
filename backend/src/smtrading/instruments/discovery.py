"""On-demand instruments: add a symbol an agent asks for when the providers know it.

`discover` canonicalises the symbol and returns the existing row when there is one (an
inactive row is never reactivated: None, so the caller's normal not-found path answers).
Otherwise it classifies the symbol, asks the market data router for its metadata (search)
and a live quote with a positive price, and creates the instrument with the catalog's
tick/lot defaults, `source=auto`, an audit row and an `instrument.added` notification.

Supported: NASDAQ/NYSE in USD, MTA (.MI) / XETRA (.DE) / EURONEXT_PARIS (.PA) in EUR,
crypto `XXX-USD`, forex `XXXYYY`. Anything else (OTC, LSE/GBX, funds, other exchanges)
is refused with INVALID_ORDER "Strumento non supportato: <motivo>"; a symbol no provider
knows is SYMBOL_NOT_FOUND, a provider outage PROVIDER_UNAVAILABLE.

Limits: the `instruments.auto_add` setting ({"on": bool}, default on) and at most
DAILY_LIMIT additions per agent per UTC day (Redis counter) → RISK_LIMIT_EXCEEDED.
"""

import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Protocol
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from smtrading.common.errors import ErrorCode, SMTError
from smtrading.instruments.models import AssetClass, Instrument, InstrumentSource
from smtrading.instruments.service import canonical_symbol
from smtrading.marketdata.types import InstrumentHit, Quote
from smtrading.risk.models import AppSetting

log = logging.getLogger(__name__)

AUTO_ADD_KEY = "instruments.auto_add"
DAILY_LIMIT = 25
# attempts per agent and day, failures included (each one reaches the providers)
DAILY_ATTEMPTS = 200
COUNTER_TTL_S = 2 * 86400
SEARCH_LIMIT = 10

_EU_SUFFIX = {"MI": "MTA", "DE": "XETRA", "PA": "EURONEXT_PARIS"}
_FX_CURRENCIES = frozenset({
    "EUR", "USD", "GBP", "JPY", "CHF", "AUD", "NZD", "CAD", "SEK", "NOK", "DKK", "PLN",
    "CZK", "HUF", "TRY", "ZAR", "MXN", "SGD", "HKD", "CNH", "CNY", "ILS", "INR", "BRL",
    "KRW", "THB", "RON",
})
# exchange (code or display name, upper case without spaces) -> market
_US_EXCHANGES = {
    **dict.fromkeys(
        ("NASDAQ", "NMS", "NGM", "NCM", "NAS", "NASDAQGS", "NASDAQGM", "NASDAQCM"), "NASDAQ"),
    **dict.fromkeys(
        ("NYSE", "NYQ", "NYSEARCA", "PCX", "NYSEAMERICAN", "NYSEMKT", "ASE", "AMEX", "BATS",
         "BTS", "CBOEUS", "CBOE"), "NYSE"),
}
_CRYPTO = re.compile(r"^([A-Z0-9]{2,15})-([A-Z]{3,4})$")
_FX = re.compile(r"^([A-Z]{3})([A-Z]{3})(=X)?$")
_SUFFIXED = re.compile(r"^([A-Z0-9][A-Z0-9\-]{0,15})\.([A-Z]{1,3})$")
_US = re.compile(r"^[A-Z][A-Z0-9]{0,5}(-[A-Z])?$")

# catalog defaults: never reject a valid price (sub-dollar stocks, fractional shares)
_SIZES = {
    AssetClass.us_equity: (Decimal("0.000001"), Decimal("0.0001"), True),
    AssetClass.eu_equity: (Decimal(1), Decimal("0.0001"), True),
    AssetClass.crypto: (Decimal("0.00000001"), Decimal("0.00000001"), False),
}


class DiscoveryRouter(Protocol):
    async def search(self, query: str, limit: int = 20) -> list[InstrumentHit]: ...

    async def probe_quote(
        self, symbol: str, asset_class: AssetClass, provider_symbols: dict | None = None
    ) -> Quote: ...


class Unsupported(Exception):
    """The symbol cannot be added; str(e) is the (Italian) reason."""


class Unrecognized(Unsupported):
    """The symbol matches no supported syntax: treated as unknown (SYMBOL_NOT_FOUND)."""


@dataclass(frozen=True)
class Shape:
    """What the symbol's syntax tells: market is None for a US ticker (NASDAQ or NYSE)."""

    symbol: str
    asset_class: AssetClass
    market: str | None
    currency: str


@dataclass(frozen=True)
class Candidate:
    symbol: str
    name: str
    asset_class: AssetClass
    market: str
    currency: str


def unsupported_error(symbol: str, reason: str) -> SMTError:
    return SMTError(
        ErrorCode.INVALID_ORDER, f"Strumento non supportato: {reason}",
        {"symbol": symbol, "reason": reason},
    )


def _unknown(symbol: str) -> SMTError:
    return SMTError(
        ErrorCode.SYMBOL_NOT_FOUND, "simbolo sconosciuto ai fornitori di dati", {"symbol": symbol}
    )


def _only_missing(e: SMTError) -> bool:
    """The router's error when every provider answered without this symbol."""
    errors = (e.details or {}).get("errors")
    return (
        e.code == ErrorCode.PROVIDER_UNAVAILABLE and isinstance(errors, dict) and bool(errors)
        and all(str(v).startswith("missing") for v in errors.values())
    )


def classify_symbol(raw: str) -> Shape:
    """Classify a canonical (or Yahoo-style) symbol by its syntax; raises Unsupported."""
    symbol = canonical_symbol(raw)
    if m := _FX.match(symbol):
        base, quote = m.group(1), m.group(2)
        if base in _FX_CURRENCIES and quote in _FX_CURRENCIES and base != quote:
            return Shape(base + quote, AssetClass.forex, "FX", quote)
    if m := _CRYPTO.match(symbol):
        if m.group(2) != "USD":
            raise Unsupported("solo coppie crypto in USD (es. BTC-USD)")
        return Shape(symbol, AssetClass.crypto, "CRYPTO", "USD")
    if m := _SUFFIXED.match(symbol):
        market = _EU_SUFFIX.get(m.group(2))
        if market is None:
            raise Unsupported(f"borsa .{m.group(2)} non supportata (solo .MI, .DE, .PA)")
        return Shape(symbol, AssetClass.eu_equity, market, "EUR")
    if _US.match(symbol):
        return Shape(symbol, AssetClass.us_equity, None, "USD")
    raise Unrecognized("simbolo non riconosciuto")


def _exchange_key(exchange: str | None) -> str:
    return re.sub(r"\s+", "", exchange or "").upper()


def classify_hit(hit: InstrumentHit) -> Candidate:
    """The instrument a provider search hit would become; raises Unsupported."""
    shape = classify_symbol(hit.symbol)
    if hit.currency and hit.currency.upper() != shape.currency:
        raise Unsupported(f"valuta {hit.currency} non supportata per {shape.symbol}")
    if hit.asset_class is not None and hit.asset_class != shape.asset_class:
        raise Unsupported(f"tipo di strumento non coerente ({hit.asset_class})")
    if shape.asset_class in (AssetClass.us_equity, AssetClass.eu_equity) and (
        hit.asset_class is None
    ):
        raise Unsupported("tipo di strumento non supportato (solo azioni ed ETF)")
    market = shape.market
    if market is None:  # US ticker: the exchange decides, OTC/pink sheets are refused
        market = _US_EXCHANGES.get(_exchange_key(hit.exchange))
        if market is None:
            raise Unsupported(f"borsa {hit.exchange or 'sconosciuta'} non supportata")
    name = (hit.name or "").strip()[:256] or shape.symbol
    return Candidate(shape.symbol, name, shape.asset_class, market, shape.currency)


def _fx_name(symbol: str) -> str:
    return f"{symbol[:3]} / {symbol[3:]}"


async def _search_candidate(router: DiscoveryRouter, shape: Shape) -> Candidate:
    """The provider hit whose symbol is this one; FX and crypto need none."""
    if shape.asset_class == AssetClass.forex:
        return Candidate(shape.symbol, _fx_name(shape.symbol), shape.asset_class, "FX",
                         shape.currency)
    try:
        hits = await router.search(shape.symbol, SEARCH_LIMIT)
    except SMTError:
        if shape.asset_class != AssetClass.crypto:
            raise
        hits = []
    reasons: list[str] = []
    for hit in hits:
        try:
            if classify_symbol(hit.symbol).symbol != shape.symbol:
                continue
            return classify_hit(hit)
        except Unsupported as e:
            reasons.append(str(e))
    if reasons:
        raise Unsupported(reasons[0])
    if shape.asset_class == AssetClass.crypto:
        base = shape.symbol.split("-")[0]
        return Candidate(shape.symbol, base, shape.asset_class, "CRYPTO", "USD")
    raise _unknown(shape.symbol)


def _positive_price(q: Quote) -> Decimal | None:
    if q.last is not None and q.last > 0:
        return q.last
    if q.bid is not None and q.ask is not None and q.bid > 0 and q.ask > 0:
        return (q.bid + q.ask) / 2
    return None


async def auto_add_enabled(session: AsyncSession) -> bool:
    value = await session.scalar(select(AppSetting.value).where(AppSetting.key == AUTO_ADD_KEY))
    return not isinstance(value, dict) or bool(value.get("on", True))


async def set_auto_add(session: AsyncSession, on: bool) -> None:
    stmt = insert(AppSetting).values(key=AUTO_ADD_KEY, value={"on": bool(on)})
    await session.execute(
        stmt.on_conflict_do_update(index_elements=["key"], set_={"value": {"on": bool(on)}})
    )


def _counter_key(agent_id: UUID) -> str:
    return f"instruments:auto_add:{agent_id}:{datetime.now(UTC).date().isoformat()}"


def _attempts_key(agent_id: UUID) -> str:
    return f"instruments:auto_add_attempts:{agent_id}:{datetime.now(UTC).date().isoformat()}"


async def auto_adds_today(redis: Any, agent_id: UUID) -> int:
    return int(await redis.get(_counter_key(agent_id)) or 0)


def sizes(asset_class: AssetClass, currency: str) -> tuple[Decimal, Decimal, bool]:
    """(lot_size, tick_size, shortable) defaults of the catalog."""
    if asset_class == AssetClass.forex:
        return Decimal(1), Decimal("0.001") if currency == "JPY" else Decimal("0.00001"), False
    return _SIZES[asset_class]


async def discover(
    session: AsyncSession,
    router: DiscoveryRouter,
    symbol: str,
    *,
    actor: str,
    agent_id: UUID | None = None,
    redis: Any = None,
) -> Instrument | None:
    """The instrument for `symbol`, created from the providers when missing (see module).

    `actor` is recorded as `added_by` ("auto:<agent name>" or "admin:<email>"); with
    `agent_id` (and `redis`) the agent's daily limit applies. Does not commit."""
    canonical = canonical_symbol(symbol)
    existing = await session.get(Instrument, canonical)
    if existing is not None:
        return existing if existing.active else None
    try:
        shape = classify_symbol(canonical)
    except Unrecognized:
        raise _unknown(canonical) from None
    except Unsupported as e:
        raise unsupported_error(canonical, str(e)) from None
    if shape.symbol != canonical:  # "EURUSD=X" -> "EURUSD"
        return await discover(
            session, router, shape.symbol, actor=actor, agent_id=agent_id, redis=redis)
    if not await auto_add_enabled(session):
        raise SMTError(
            ErrorCode.SYMBOL_NOT_FOUND,
            "simbolo non in catalogo (aggiunta automatica disattivata)",
            {"symbol": canonical},
        )

    counter = _counter_key(agent_id) if agent_id is not None and redis is not None else None
    if counter is not None:
        used = await redis.incr(counter)
        await redis.expire(counter, COUNTER_TTL_S)
        if used > DAILY_LIMIT:
            await redis.decr(counter)
            raise SMTError(
                ErrorCode.RISK_LIMIT_EXCEEDED,
                f"limite di {DAILY_LIMIT} strumenti aggiunti automaticamente al giorno raggiunto",
                {"symbol": canonical, "limit": DAILY_LIMIT},
            )
    if counter is not None:
        attempts_key = _attempts_key(agent_id)
        attempts = await redis.incr(attempts_key)
        await redis.expire(attempts_key, COUNTER_TTL_S)
        if attempts > DAILY_ATTEMPTS:
            await redis.decr(counter)
            raise SMTError(
                ErrorCode.RATE_LIMITED,
                f"troppi tentativi di aggiunta automatica oggi (max {DAILY_ATTEMPTS})",
                {"symbol": canonical, "limit": DAILY_ATTEMPTS},
            )
    try:
        instrument = await _create(session, router, shape, actor)
    except BaseException:
        if counter is not None:
            await redis.decr(counter)
        raise
    if instrument is None:  # a concurrent call created it first
        if counter is not None:
            await redis.decr(counter)
        existing = await session.get(Instrument, canonical, populate_existing=True)
        return existing if existing is not None and existing.active else None
    await _record(session, instrument, agent_id)
    return instrument


async def _create(
    session: AsyncSession, router: DiscoveryRouter, shape: Shape, actor: str
) -> Instrument | None:
    try:
        candidate = await _search_candidate(router, shape)
    except Unsupported as e:
        raise unsupported_error(shape.symbol, str(e)) from None
    try:
        quote = await router.probe_quote(candidate.symbol, candidate.asset_class, {})
    except SMTError as e:
        if e.code == ErrorCode.SYMBOL_NOT_FOUND or _only_missing(e):
            raise _unknown(candidate.symbol) from None
        raise  # an outage stays PROVIDER_UNAVAILABLE: the agent may retry
    if _positive_price(quote) is None:
        raise unsupported_error(candidate.symbol, "nessuna quotazione con prezzo positivo")
    lot, tick, shortable = sizes(candidate.asset_class, candidate.currency)
    row = await session.scalar(
        insert(Instrument)
        .values(
            symbol=candidate.symbol, name=candidate.name, asset_class=candidate.asset_class,
            market=candidate.market, currency=candidate.currency, lot_size=lot,
            tick_size=tick, shortable=shortable, active=True, provider_symbols={},
            source=InstrumentSource.auto, added_by=actor[:128],
        )
        .on_conflict_do_nothing(index_elements=["symbol"])
        .returning(Instrument.symbol)
    )
    if row is None:
        return None
    return await session.get(Instrument, candidate.symbol)


async def _record(session: AsyncSession, instrument: Instrument, agent_id: UUID | None) -> None:
    from smtrading.agents.service import write_audit
    from smtrading.events import notify

    payload = {
        "symbol": instrument.symbol, "name": instrument.name, "market": instrument.market,
        "asset_class": str(instrument.asset_class), "currency": instrument.currency,
        "added_by": instrument.added_by,
    }
    await write_audit(
        session, agent_id=agent_id, method="instrument.auto_add", params=payload,
        outcome="ok", latency_ms=0,
    )
    await notify(session, portfolio_id=None, kind="instrument.added", payload=payload)
    log.info("instrument %s added on demand by %s", instrument.symbol, instrument.added_by)
