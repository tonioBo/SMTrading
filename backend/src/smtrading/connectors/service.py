from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from smtrading.common.config import get_settings
from smtrading.common.crypto import decrypt_json, encrypt_json
from smtrading.common.errors import ErrorCode, SMTError
from smtrading.connectors.models import Connector, ProviderPriority
from smtrading.instruments.models import AssetClass
from smtrading.marketdata.types import Capability

_US, _EU, _CR, _FX = (
    AssetClass.us_equity,
    AssetClass.eu_equity,
    AssetClass.crypto,
    AssetClass.forex,
)
_C = Capability

# Combinations absent from this table have no providers. `etoro` (plan 9) is last and off
# by default: migration 0028 appends it to the existing rows too (0044: EU stocks, whose
# Twelve Data / Finnhub plans do not cover EU listings). `rssnews` (plan 10, the
# keyless CoinDesk/Cointelegraph feeds) leads crypto news and is on: migration 0029.
DEFAULT_PRIORITIES: dict[tuple[AssetClass, Capability], list[str]] = {
    (_US, _C.quote): ["alpaca", "finnhub", "yahoo", "etoro"],
    (_EU, _C.quote): ["twelvedata", "finnhub", "yahoo", "etoro"],
    (_CR, _C.quote): ["kraken", "binance", "coingecko", "yahoo", "etoro"],
    (_FX, _C.quote): ["twelvedata", "yahoo", "frankfurter"],
    (_US, _C.bars): ["alpaca", "yahoo", "etoro"],
    (_EU, _C.bars): ["twelvedata", "yahoo", "etoro"],
    (_CR, _C.bars): ["kraken", "binance", "yahoo", "etoro"],
    (_FX, _C.bars): ["twelvedata", "yahoo"],
    (_US, _C.stream): ["alpaca", "finnhub"],
    (_EU, _C.stream): ["twelvedata", "finnhub"],
    (_CR, _C.stream): ["kraken", "binance"],
    (_FX, _C.stream): ["twelvedata"],
    (_US, _C.news): ["alpaca", "finnhub", "yahoo"],
    (_EU, _C.news): ["finnhub", "yahoo"],
    (_CR, _C.news): ["rssnews", "yahoo"],
    (_US, _C.fundamentals): ["finnhub", "yahoo"],
    (_EU, _C.fundamentals): ["finnhub", "yahoo"],
    (_CR, _C.fundamentals): ["coingecko"],
    (_US, _C.calendar): ["finnhub", "yahoo"],
    (_EU, _C.calendar): ["finnhub", "yahoo"],
    (_FX, _C.fx): ["frankfurter", "twelvedata", "yahoo"],
    (_US, _C.search): ["yahoo"],
    (_EU, _C.search): ["yahoo"],
    (_CR, _C.search): ["coingecko", "yahoo"],
    (_FX, _C.search): ["yahoo"],
    (_US, _C.screener): ["yahoo"],
    (_EU, _C.screener): ["yahoo"],
}

DEFAULT_ENABLED = {"yahoo", "kraken", "binance", "coingecko", "frankfurter", "rssnews"}

_ALL_CONNECTORS = sorted({p for ps in DEFAULT_PRIORITIES.values() for p in ps} | {"revolutx"})


async def seed_connectors(session: AsyncSession) -> None:
    """Insert missing connector and priority rows; never overwrites existing ones."""
    await session.execute(
        insert(Connector)
        .values([{"name": n, "enabled": n in DEFAULT_ENABLED} for n in _ALL_CONNECTORS])
        .on_conflict_do_nothing(index_elements=["name"])
    )
    await session.execute(
        insert(ProviderPriority)
        .values(
            [
                {"asset_class": ac, "capability": cap, "providers": list(ps)}
                for (ac, cap), ps in DEFAULT_PRIORITIES.items()
            ]
        )
        .on_conflict_do_nothing(index_elements=["asset_class", "capability"])
    )
    await session.flush()


async def list_connectors(session: AsyncSession) -> list[Connector]:
    return list((await session.execute(select(Connector).order_by(Connector.name))).scalars())


async def _get(session: AsyncSession, name: str) -> Connector:
    c = await session.get(Connector, name)
    if c is None:
        raise SMTError(ErrorCode.NOT_FOUND, f"connector {name} not found")
    return c


async def get_connector_config(session: AsyncSession, name: str) -> dict:
    c = await _get(session, name)
    if c.config_encrypted is None:
        return {}
    return decrypt_json(c.config_encrypted, get_settings().master_key)


async def update_connector(
    session: AsyncSession,
    name: str,
    *,
    enabled: bool | None = None,
    config: dict | None = None,
    rate_limit_per_min: int | None = None,
) -> Connector:
    c = await _get(session, name)
    if enabled is not None:
        c.enabled = enabled
    if config is not None:
        c.config_encrypted = encrypt_json(config, get_settings().master_key)
    if rate_limit_per_min is not None:
        c.rate_limit_per_min = rate_limit_per_min
    await session.flush()
    return c


def _is_quota_error(error: str | None) -> bool:
    """A provider that answered «too many requests» (HTTP 429) is reachable but out of
    quota: the router already moves it to the end of the list until the reset."""
    return bool(error) and ("429" in error or "too many requests" in error.lower())


async def record_health(session: AsyncSession, name: str, ok: bool, error: str | None) -> None:
    c = await _get(session, name)
    c.health_status = "ok" if ok else ("limited" if _is_quota_error(error) else "error")
    c.last_error = None if ok else error
    c.last_checked_at = datetime.now(UTC)
    await session.flush()


async def get_priorities(
    session: AsyncSession, asset_class: AssetClass, capability: Capability
) -> list[str]:
    row = await session.get(ProviderPriority, (asset_class, capability))
    if row is not None:
        return list(row.providers)
    return list(DEFAULT_PRIORITIES.get((asset_class, capability), []))


async def set_priorities(
    session: AsyncSession,
    asset_class: AssetClass,
    capability: Capability,
    providers: list[str],
) -> None:
    known = set((await session.execute(select(Connector.name))).scalars())
    unknown = [p for p in providers if p not in known]
    if unknown:
        raise SMTError(ErrorCode.NOT_FOUND, f"unknown providers: {', '.join(unknown)}")
    row = await session.get(ProviderPriority, (asset_class, capability))
    if row is None:
        session.add(
            ProviderPriority(
                asset_class=asset_class, capability=capability, providers=list(providers)
            )
        )
    else:
        row.providers = list(providers)
    await session.flush()
