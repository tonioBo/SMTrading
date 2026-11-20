import pytest
from sqlalchemy import select, text

from smtrading.common.errors import ErrorCode, SMTError
from smtrading.connectors.models import Connector, ProviderPriority
from smtrading.connectors.service import (
    DEFAULT_ENABLED,
    DEFAULT_PRIORITIES,
    get_connector_config,
    get_priorities,
    list_connectors,
    record_health,
    seed_connectors,
    set_priorities,
    update_connector,
)
from smtrading.instruments.models import AssetClass
from smtrading.marketdata.types import Capability


@pytest.fixture(autouse=True)
def _master_key(monkeypatch, pg_url, redis_url):
    monkeypatch.setenv("SMT_DATABASE_URL", pg_url)
    monkeypatch.setenv("SMT_REDIS_URL", redis_url)
    monkeypatch.setenv("SMT_MASTER_KEY", "test-master-key")
    monkeypatch.setenv("SMT_ADMIN_EMAIL", "admin@example.com")
    monkeypatch.setenv("SMT_ADMIN_PASSWORD", "test-password")


async def test_seed_idempotent_and_keeps_admin_edits(db_session):
    await seed_connectors(db_session)
    n1 = len(await list_connectors(db_session))
    prios1 = len((await db_session.execute(select(ProviderPriority))).scalars().all())
    await update_connector(db_session, "alpaca", enabled=True)
    await set_priorities(db_session, AssetClass.crypto, Capability.quote, ["binance"])
    await seed_connectors(db_session)
    conns = {c.name: c for c in await list_connectors(db_session)}
    assert len(conns) == n1
    assert len((await db_session.execute(select(ProviderPriority))).scalars().all()) == prios1
    assert "revolutx" in conns
    assert conns["alpaca"].enabled is True
    assert conns["yahoo"].enabled is True
    assert {n for n, c in conns.items() if c.enabled} == DEFAULT_ENABLED | {"alpaca"}
    assert await get_priorities(db_session, AssetClass.crypto, Capability.quote) == ["binance"]


async def test_config_roundtrip_is_encrypted(db_session):
    await seed_connectors(db_session)
    assert await get_connector_config(db_session, "alpaca") == {}
    await update_connector(
        db_session, "alpaca", config={"key": "K", "secret": "S"}, rate_limit_per_min=200
    )
    assert await get_connector_config(db_session, "alpaca") == {"key": "K", "secret": "S"}
    raw = (
        await db_session.execute(
            text("select config_encrypted from connectors where name='alpaca'")
        )
    ).scalar_one()
    assert isinstance(raw, bytes)
    assert b"secret" not in raw and b"K" != raw
    c = (await list_connectors(db_session))[0]
    assert isinstance(c, Connector)


async def test_unknown_connector(db_session):
    await seed_connectors(db_session)
    with pytest.raises(SMTError) as e:
        await get_connector_config(db_session, "nope")
    assert e.value.code == ErrorCode.NOT_FOUND
    with pytest.raises(SMTError) as e:
        await update_connector(db_session, "nope", enabled=True)
    assert e.value.code == ErrorCode.NOT_FOUND


async def test_record_health(db_session):
    await seed_connectors(db_session)
    await record_health(db_session, "yahoo", False, "boom")
    y = next(c for c in await list_connectors(db_session) if c.name == "yahoo")
    assert (y.health_status, y.last_error) == ("error", "boom")
    assert y.last_checked_at is not None
    await record_health(db_session, "yahoo", True, None)
    await db_session.refresh(y)
    assert (y.health_status, y.last_error) == ("ok", None)


async def test_record_health_quota_is_limited_not_error(db_session):
    """A 429 (out of quota) is shown as «limited», not as a failure."""
    await seed_connectors(db_session)
    await record_health(db_session, "twelvedata", False, "RuntimeError: twelvedata: HTTP 429")
    t = next(c for c in await list_connectors(db_session) if c.name == "twelvedata")
    assert t.health_status == "limited"
    assert t.last_error == "RuntimeError: twelvedata: HTTP 429"


async def test_priorities_default_and_missing(db_session):
    await seed_connectors(db_session)
    assert await get_priorities(db_session, AssetClass.crypto, Capability.quote) == [
        "kraken", "binance", "coingecko", "yahoo", "etoro",
    ]
    assert await get_priorities(db_session, AssetClass.forex, Capability.news) == []
    assert (AssetClass.forex, Capability.news) not in DEFAULT_PRIORITIES


async def test_priorities_fallback_without_rows(db_session):
    assert await get_priorities(db_session, AssetClass.us_equity, Capability.stream) == [
        "alpaca", "finnhub",
    ]


async def test_set_priorities_unknown_provider(db_session):
    await seed_connectors(db_session)
    with pytest.raises(SMTError) as e:
        await set_priorities(db_session, AssetClass.crypto, Capability.quote, ["kraken", "zzz"])
    assert e.value.code == ErrorCode.NOT_FOUND
    await set_priorities(db_session, AssetClass.crypto, Capability.quote, ["yahoo", "kraken"])
    assert await get_priorities(db_session, AssetClass.crypto, Capability.quote) == [
        "yahoo", "kraken",
    ]


async def test_etoro_seeded_off_and_last_in_its_priorities(db_session):
    """Plan 9 Task 14: the eToro data source is born off and last; nothing changes until
    the admin turns it on."""
    await seed_connectors(db_session)
    conns = {c.name: c for c in await list_connectors(db_session)}
    assert "etoro" in conns and conns["etoro"].enabled is False
    assert "etoro" not in DEFAULT_ENABLED
    pairs = {(AssetClass.us_equity, Capability.quote), (AssetClass.us_equity, Capability.bars),
             (AssetClass.crypto, Capability.quote), (AssetClass.crypto, Capability.bars)}
    for (ac, cap), providers in DEFAULT_PRIORITIES.items():
        assert ("etoro" in providers) == ((ac, cap) in pairs), (ac, cap)
    for ac, cap in pairs:
        providers = await get_priorities(db_session, ac, cap)
        assert providers[-1] == "etoro" and providers.count("etoro") == 1


async def test_rssnews_seeded_on_and_first_for_crypto_news(db_session):
    """Plan 10 Task B: the keyless CoinDesk/Cointelegraph news source is born on and leads
    crypto news only."""
    await seed_connectors(db_session)
    conns = {c.name: c for c in await list_connectors(db_session)}
    assert conns["rssnews"].enabled is True
    assert "rssnews" in DEFAULT_ENABLED
    for (ac, cap), providers in DEFAULT_PRIORITIES.items():
        assert ("rssnews" in providers) == ((ac, cap) == (AssetClass.crypto, Capability.news))
    assert await get_priorities(db_session, AssetClass.crypto, Capability.news) == [
        "rssnews", "yahoo",
    ]
