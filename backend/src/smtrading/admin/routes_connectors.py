"""Admin API: market data connectors (masked config, health test) and provider priorities."""

import asyncio
from datetime import UTC, datetime
from typing import Any

import httpx
from fastapi import APIRouter, Depends
from pydantic import BaseModel

from smtrading.admin.auth import DbSession, RedisClient, require_access
from smtrading.admin.common import (
    _invalid,
    _transaction,
    is_secret,
    mask_config,
    merge_config,
    redact,
    secret_values,
)
from smtrading.common.errors import ErrorCode, SMTError
from smtrading.connectors.models import Connector
from smtrading.connectors.service import (
    get_connector_config,
    get_priorities,
    list_connectors,
    record_health,
    set_priorities,
    update_connector,
)
from smtrading.instruments.models import AssetClass
from smtrading.marketdata import router as _providers_loaded  # noqa: F401 - registers PROVIDERS
from smtrading.marketdata.provider import ProviderUnsupported
from smtrading.marketdata.registry import PROVIDERS
from smtrading.marketdata.types import Capability
from smtrading.mcp.dto import parse_enum, to_jsonable

router = APIRouter(prefix="/api", tags=["connectors"], dependencies=[Depends(require_access)])

HEALTH_TIMEOUT_S = 10.0
MAX_RATE_LIMIT = 1_000_000


class ConnectorPatchIn(BaseModel):
    """Only the fields present in the request are changed (`model_fields_set`)."""

    enabled: bool | None = None
    config: dict[str, Any] | None = None
    rate_limit_per_min: int | None = None


class PriorityIn(BaseModel):
    asset_class: str
    capability: str
    providers: list[str]


async def _connector(session: DbSession, name: str) -> Connector:
    c = await session.get(Connector, name)
    if c is None:
        raise SMTError(ErrorCode.NOT_FOUND, "connettore non trovato", {"name": name})
    return c


async def _dto(session: DbSession, c: Connector) -> dict[str, Any]:
    cls = PROVIDERS.get(c.name)
    config = mask_config(await get_connector_config(session, c.name))
    requires_key = bool(cls and cls.requires_key)
    # the fields the portal shows even when empty (requires_key adds api_key client-side)
    config_fields = list(getattr(cls, "config_fields", ())) if cls else []
    # the top-level config keys the web app treats as secrets (never re-derived client-side)
    secret_keys = sorted(
        {k for k in config if is_secret(k)}
        | ({"api_key"} if requires_key else set())
        | {k for k in config_fields if is_secret(k)}
    )
    return {
        "name": c.name,
        "enabled": c.enabled,
        "requires_key": requires_key,
        "capabilities": sorted(str(x) for x in cls.capabilities) if cls else [],
        "asset_classes": sorted(str(x) for x in cls.asset_classes) if cls else [],
        "implemented": cls is not None,
        "config_masked": config,
        "config_fields": config_fields,
        "secret_keys": secret_keys,
        "rate_limit_per_min": c.rate_limit_per_min,
        "health_status": c.health_status,
        "last_error": c.last_error,
        "last_checked_at": to_jsonable(c.last_checked_at),
    }


@router.get("/connectors")
async def get_connectors(session: DbSession) -> list[dict]:
    return [await _dto(session, c) for c in await list_connectors(session)]


@router.patch("/connectors/{name}")
async def patch_connector(
    name: str, body: ConnectorPatchIn, session: DbSession, redis: RedisClient
) -> dict:
    c = await _connector(session, name)
    fields = body.model_fields_set
    if "enabled" in fields and body.enabled is None:
        raise _invalid("enabled", "enabled non può essere nullo")
    if "config" in fields and body.config is None:
        raise _invalid("config", "config non può essere nullo")
    rate = body.rate_limit_per_min
    if rate is not None and not 1 <= rate <= MAX_RATE_LIMIT:
        raise _invalid("rate_limit_per_min", "limite di richieste fuori intervallo")
    async with _transaction(session, redis):
        config = None
        if body.config is not None:
            config = merge_config(body.config, await get_connector_config(session, name))
        await update_connector(
            session, name, enabled=body.enabled, config=config, rate_limit_per_min=rate
        )
    return await _dto(session, c)


@router.post("/connectors/{name}/test")
async def test_connector(name: str, session: DbSession, redis: RedisClient) -> dict:
    """Builds the provider with its stored config and runs its health check (10 s timeout)."""
    await _connector(session, name)
    cls = PROVIDERS.get(name)
    if cls is None:
        raise _invalid("name", "connettore non implementato")
    ok, error = True, None
    config = await get_connector_config(session, name)
    secrets = secret_values(config)
    try:
        async with httpx.AsyncClient() as http:
            await asyncio.wait_for(cls(config, http).health_check(), HEALTH_TIMEOUT_S)
    except ProviderUnsupported:
        return {"ok": False, "error": "health check non supportato dal connettore"}
    except TimeoutError:
        ok, error = False, f"timeout dopo {HEALTH_TIMEOUT_S:g} s"
    except Exception as e:  # noqa: BLE001 - reported to the admin and recorded
        ok, error = False, redact(f"{type(e).__name__}: {e}", secrets)[:500]
    async with _transaction(session, redis):
        await record_health(session, name, ok, error)
    return {"ok": ok, "error": error, "checked_at": datetime.now(UTC).isoformat()}


@router.get("/provider-priorities")
async def get_provider_priorities(session: DbSession) -> list[dict]:
    """Every (asset class, capability) pair with its ordered provider list."""
    return [
        {
            "asset_class": ac.value,
            "capability": cap.value,
            "providers": await get_priorities(session, ac, cap),
        }
        for ac in AssetClass
        for cap in Capability
    ]


@router.put("/provider-priorities")
async def put_provider_priorities(
    body: PriorityIn, session: DbSession, redis: RedisClient
) -> dict:
    asset_class = parse_enum(AssetClass, body.asset_class, "asset_class")
    capability = parse_enum(Capability, body.capability, "capability")
    if asset_class is None or capability is None:
        raise _invalid("asset_class", "asset_class e capability obbligatori")
    async with _transaction(session, redis):
        await set_priorities(session, asset_class, capability, body.providers)
    return {
        "asset_class": asset_class.value,
        "capability": capability.value,
        "providers": await get_priorities(session, asset_class, capability),
    }
