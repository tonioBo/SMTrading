"""On-demand instruments (instruments.discovery) with a fake router."""

from datetime import UTC, datetime
from decimal import Decimal

import pytest
import redis.asyncio as aioredis
from sqlalchemy import select

from smtrading.agents.models import McpAuditLog
from smtrading.agents.service import create_agent
from smtrading.common.errors import ErrorCode, SMTError
from smtrading.instruments import discovery
from smtrading.instruments.discovery import (
    DAILY_LIMIT,
    Unsupported,
    classify_hit,
    classify_symbol,
    discover,
    set_auto_add,
)
from smtrading.instruments.models import AssetClass, Instrument, InstrumentSource
from smtrading.marketdata.types import InstrumentHit, Quote
from smtrading.risk.models import Notification

D = Decimal


class FakeRouter:
    def __init__(self, hits=(), prices=None):
        self.hits = list(hits)
        self.prices = dict(prices or {})
        self.probed: list[tuple[str, AssetClass]] = []

    async def search(self, query, limit=20):
        return [h for h in self.hits if query.lower() in h.symbol.lower()][:limit]

    async def probe_quote(self, symbol, asset_class, provider_symbols=None):
        self.probed.append((symbol, asset_class))
        if symbol not in self.prices:
            raise SMTError(ErrorCode.SYMBOL_NOT_FOUND, "no quote", {})
        return Quote(symbol, None, None, self.prices[symbol], datetime.now(UTC), "fake")


def hit(symbol, name, ac, exchange, currency=None):
    return InstrumentHit(symbol, name, ac, exchange, currency, "yahoo")


HITS = [
    hit("ZZNQ", "Zeta Nasdaq Inc.", AssetClass.us_equity, "NASDAQ"),
    hit("ZZNY", "Zeta NYSE Corp.", AssetClass.us_equity, "NYSE"),
    hit("ZZARC", "Zeta Arca ETF", AssetClass.us_equity, "NYSEArca"),
    hit("ZZOTC", "Zeta Pink", AssetClass.us_equity, "OTC Markets"),
    hit("ZZFND", "Zeta Fund", None, "NAS"),
    hit("ZZE.MI", "Zeta Italia", AssetClass.eu_equity, "Milan"),
    hit("ZZE.PA", "Zeta France", AssetClass.eu_equity, "Paris"),
    hit("ZZL.L", "Zeta London", AssetClass.eu_equity, "LSE"),
    hit("ZZC-USD", "ZetaCoin USD", AssetClass.crypto, "CCC"),
    hit("ZZZERO", "Zero Price", AssetClass.us_equity, "NYSE"),
]
PRICES = {"ZZNQ": D("12.5"), "ZZNY": D("0.0123"), "ZZARC": D(40), "ZZOTC": D(1),
          "ZZFND": D(10), "ZZE.MI": D("3.21"), "ZZE.PA": D(55), "ZZL.L": D(2),
          "ZZC-USD": D("0.00004321"), "EURSEK": D("11.2"), "USDJPY": D("150.1"),
          "ZZZERO": D(0)}


@pytest.fixture
async def redis(redis_url):
    r = aioredis.from_url(redis_url)

    async def clear():
        keys = [k async for k in r.scan_iter("instruments:auto_add:*")]
        if keys:
            await r.delete(*keys)

    await clear()
    yield r
    await clear()
    await r.aclose()


@pytest.fixture
async def agent(db_session):
    a, _ = await create_agent(db_session, "disc-agent")
    return a


@pytest.fixture
def router():
    return FakeRouter(HITS, PRICES)


async def _add(db_session, router, symbol, agent=None, redis=None):
    return await discover(
        db_session, router, symbol, actor="auto:disc-agent",
        agent_id=agent.id if agent else None, redis=redis,
    )


@pytest.mark.parametrize(("symbol", "market", "ac", "ccy", "tick", "lot"), [
    ("zznq", "NASDAQ", AssetClass.us_equity, "USD", "0.0001", "0.000001"),
    ("ZZNY", "NYSE", AssetClass.us_equity, "USD", "0.0001", "0.000001"),
    ("ZZARC", "NYSE", AssetClass.us_equity, "USD", "0.0001", "0.000001"),
    ("ZZE.MI", "MTA", AssetClass.eu_equity, "EUR", "0.0001", "1"),
    ("ZZE.PA", "EURONEXT_PARIS", AssetClass.eu_equity, "EUR", "0.0001", "1"),
    ("ZZC-USD", "CRYPTO", AssetClass.crypto, "USD", "0.00000001", "0.00000001"),
    ("EURSEK", "FX", AssetClass.forex, "SEK", "0.00001", "1"),
    ("USDJPY=X", "FX", AssetClass.forex, "JPY", "0.001", "1"),
])
async def test_discover_supported(db_session, router, agent, redis, symbol, market, ac, ccy,
                                  tick, lot):
    inst = await _add(db_session, router, symbol, agent, redis)
    assert inst is not None and inst.active
    assert (inst.market, inst.asset_class, inst.currency) == (market, ac, ccy)
    assert (inst.tick_size, inst.lot_size) == (D(tick), D(lot))
    assert inst.shortable is (ac in (AssetClass.us_equity, AssetClass.eu_equity))
    assert inst.source == InstrumentSource.auto and inst.added_by == "auto:disc-agent"
    audit = await db_session.scalar(
        select(McpAuditLog).where(McpAuditLog.method == "instrument.auto_add"))
    assert audit.agent_id == agent.id and audit.params["symbol"] == inst.symbol
    note = await db_session.scalar(
        select(Notification).where(Notification.kind == "instrument.added"))
    assert note.payload["symbol"] == inst.symbol and note.payload["market"] == market
    assert await discovery.auto_adds_today(redis, agent.id) == 1
    # a second call returns the row, without a new addition
    assert (await _add(db_session, router, symbol, agent, redis)).symbol == inst.symbol
    assert await discovery.auto_adds_today(redis, agent.id) == 1


async def test_discover_names(db_session, router):
    assert (await _add(db_session, router, "ZZC-USD")).name == "ZetaCoin USD"
    assert (await _add(db_session, router, "EURSEK")).name == "EUR / SEK"
    assert (await _add(db_session, router, "ZZE.MI")).name == "Zeta Italia"


@pytest.mark.parametrize(("symbol", "reason"), [
    ("ZZOTC", "borsa OTC Markets non supportata"),
    ("ZZFND", "tipo di strumento non supportato"),
    ("ZZL.L", "borsa .L non supportata"),
    ("ZZZERO", "nessuna quotazione con prezzo positivo"),
    ("BTC-EUR", "solo coppie crypto in USD"),
])
async def test_discover_rejects(db_session, router, agent, redis, symbol, reason):
    with pytest.raises(SMTError) as ei:
        await _add(db_session, router, symbol, agent, redis)
    assert ei.value.code == ErrorCode.INVALID_ORDER
    assert ei.value.message.startswith("Strumento non supportato: ")
    assert reason in ei.value.message
    assert await db_session.get(Instrument, symbol.upper()) is None
    assert await discovery.auto_adds_today(redis, agent.id) == 0  # refusals are not counted


@pytest.mark.parametrize("symbol", ["ZZNONE", "ZZNOQ-USD", "A B", "TOOLONGSYM"])
async def test_discover_unknown_symbol(db_session, router, agent, redis, symbol):
    with pytest.raises(SMTError) as ei:
        await _add(db_session, router, symbol, agent, redis)
    assert ei.value.code == ErrorCode.SYMBOL_NOT_FOUND
    assert await discovery.auto_adds_today(redis, agent.id) == 0


async def test_provider_outage_is_not_a_refusal(db_session, router):
    async def down(*a, **k):
        raise SMTError(ErrorCode.PROVIDER_UNAVAILABLE, "down", {"errors": {"yahoo": "Timeout"}})

    router.probe_quote = down
    with pytest.raises(SMTError) as ei:
        await _add(db_session, router, "ZZNQ")
    assert ei.value.code == ErrorCode.PROVIDER_UNAVAILABLE

    async def missing(*a, **k):
        raise SMTError(ErrorCode.PROVIDER_UNAVAILABLE, "x", {"errors": {"yahoo": "missing: ZZNQ"}})

    router.probe_quote = missing
    with pytest.raises(SMTError) as ei:
        await _add(db_session, router, "ZZNQ")
    assert ei.value.code == ErrorCode.SYMBOL_NOT_FOUND


async def test_inactive_existing_is_not_reactivated(db_session, router):
    db_session.add(Instrument(symbol="ZZNQ", name="Old", asset_class=AssetClass.us_equity,
                              market="NASDAQ", currency="USD", active=False))
    await db_session.flush()
    assert await _add(db_session, router, "ZZNQ") is None
    assert (await db_session.get(Instrument, "ZZNQ")).active is False
    assert router.probed == []


async def test_existing_active_returned_without_providers(db_session, router):
    db_session.add(Instrument(symbol="ZZNY", name="Manual", asset_class=AssetClass.us_equity,
                              market="NYSE", currency="USD"))
    await db_session.flush()
    inst = await _add(db_session, router, "zzny")
    assert inst.name == "Manual" and inst.source == InstrumentSource.manual
    assert router.probed == []


async def test_toggle_off(db_session, router):
    await set_auto_add(db_session, False)
    assert await discovery.auto_add_enabled(db_session) is False
    with pytest.raises(SMTError) as ei:
        await _add(db_session, router, "ZZNQ")
    assert ei.value.code == ErrorCode.SYMBOL_NOT_FOUND
    await set_auto_add(db_session, True)
    assert (await _add(db_session, router, "ZZNQ")).symbol == "ZZNQ"


async def test_daily_limit_per_agent(db_session, router, agent, redis):
    await redis.set(discovery._counter_key(agent.id), DAILY_LIMIT - 1)
    assert (await _add(db_session, router, "ZZNQ", agent, redis)).symbol == "ZZNQ"
    with pytest.raises(SMTError) as ei:
        await _add(db_session, router, "ZZNY", agent, redis)
    assert ei.value.code == ErrorCode.RISK_LIMIT_EXCEEDED
    assert await discovery.auto_adds_today(redis, agent.id) == DAILY_LIMIT
    assert await db_session.get(Instrument, "ZZNY") is None
    other, _ = await create_agent(db_session, "disc-other")
    assert (await _add(db_session, router, "ZZNY", other, redis)).symbol == "ZZNY"


def test_classify_hit_for_search():
    assert classify_hit(HITS[0]).market == "NASDAQ"
    assert classify_hit(hit("EURUSD=X", "EUR/USD", AssetClass.forex, "CCY")).symbol == "EURUSD"
    with pytest.raises(Unsupported):
        classify_hit(hit("ZZE.MI", "x", AssetClass.eu_equity, "Milan", "USD"))
    assert classify_symbol("BRK-B").asset_class == AssetClass.us_equity


async def test_failed_attempts_are_capped(db_session, router, agent, redis):
    """M13a: failures give the daily slot back, but attempts are capped too (RATE_LIMITED)."""
    calls = []

    async def down(*a, **k):
        calls.append(a)
        raise SMTError(ErrorCode.PROVIDER_UNAVAILABLE, "down", {"errors": {"yahoo": "Timeout"}})

    router.probe_quote = down
    await redis.set(discovery._attempts_key(agent.id), discovery.DAILY_ATTEMPTS - 1)
    with pytest.raises(SMTError) as ei:
        await _add(db_session, router, "ZZNQ", agent, redis)
    assert ei.value.code == ErrorCode.PROVIDER_UNAVAILABLE
    assert len(calls) == 1
    with pytest.raises(SMTError) as ei:
        await _add(db_session, router, "ZZNY", agent, redis)
    assert ei.value.code == ErrorCode.RATE_LIMITED
    assert len(calls) == 1  # no provider call once capped
    assert await discovery.auto_adds_today(redis, agent.id) == 0
