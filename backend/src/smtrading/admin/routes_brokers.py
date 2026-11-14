"""Admin API: broker links (paper/demo accounts only), connection test, portfolio link,
reconciliations and broker positions.

Configs are stored encrypted and go out masked (admin.common helpers); the secret keys
of each kind are fixed here. The base URL is fixed per kind: a config naming any other
URL is refused with FORBIDDEN by the adapter constructor, and the URL is never stored.
Broker I/O happens outside any transaction (Ruling R9) with a timeout; error texts are
redacted of the link's secrets.
"""

import asyncio
import base64
from datetime import UTC, datetime
from decimal import Decimal
from typing import Annotated, Any
from uuid import UUID

import httpx
from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from smtrading.admin.auth import DbSession, RedisClient, require_admin
from smtrading.admin.common import (
    _invalid,
    _transaction,
    mask_config,
    merge_config,
    redact,
    secret_values,
)
from smtrading.admin.idempotency import Idempotency, idempotent
from smtrading.admin.routes_portfolios import _portfolio_dto
from smtrading.brokers.alpaca_paper import PAPER_URL
from smtrading.brokers.models import BrokerLink, Reconciliation
from smtrading.brokers.registry import adapter_class
from smtrading.brokers.service import (
    create_link,
    get_link_config,
    link_portfolio,
    list_links,
    open_adapter,
    update_link,
)
from smtrading.brokers.sync import reconcile
from smtrading.brokers.t212_demo import DEMO_URL
from smtrading.common.errors import ErrorCode, SMTError
from smtrading.mcp.dto import parse_int, to_jsonable
from smtrading.portfolios.models import Portfolio

router = APIRouter(prefix="/api", tags=["brokers"], dependencies=[Depends(require_admin)])

BROKER_TIMEOUT_S = 30.0
MAX_NAME_LEN = 128
DEFAULT_RECONCILIATIONS = 50
MAX_RECONCILIATIONS = 500
# kind -> (fixed base URL, config keys holding secrets)
KINDS: dict[str, tuple[str, tuple[str, ...]]] = {
    "alpaca_paper": (PAPER_URL, ("key_id", "secret_key")),
    "t212_demo": (DEMO_URL, ("api_key", "api_secret")),
}

BrokerIdem = Annotated[Idempotency, Depends(idempotent("broker"))]


class BrokerLinkIn(BaseModel):
    name: str
    kind: str
    config: dict[str, Any] = {}


class BrokerLinkPatchIn(BaseModel):
    """Only the fields present in the request are changed (`model_fields_set`)."""

    name: str | None = None
    enabled: bool | None = None
    config: dict[str, Any] | None = None


class PortfolioLinkIn(BaseModel):
    broker_link_id: UUID | None


def _name(value: str) -> str:
    name = value.strip()
    if not 1 <= len(name) <= MAX_NAME_LEN:
        raise _invalid("name", f"nome obbligatorio (max {MAX_NAME_LEN} caratteri)")
    return name


def _clean_config(kind: str, config: dict[str, Any]) -> dict[str, Any]:
    """The config to store: known keys only, string values, no base URL. A base URL other
    than the kind's paper/demo one raises FORBIDDEN (adapter constructor)."""
    _, secret_keys = KINDS[kind]
    unknown = sorted(set(config) - set(secret_keys) - {"base_url"})
    if unknown:
        raise _invalid("config", f"chiavi non ammesse: {', '.join(unknown)}")
    for k, v in config.items():
        if v is not None and not isinstance(v, str):
            raise _invalid("config", f"{k} deve essere un testo")
    adapter_class(kind)(config, None)  # FORBIDDEN for a non-paper/demo base URL
    return {k: v for k, v in config.items() if k != "base_url" and v not in (None, "")}


async def _link(session: AsyncSession, link_id: UUID) -> BrokerLink:
    link = await session.get(BrokerLink, link_id)
    if link is None:
        raise SMTError(ErrorCode.NOT_FOUND, "collegamento broker non trovato",
                       {"broker_link_id": str(link_id)})
    return link


async def _dto(session: AsyncSession, link: BrokerLink) -> dict[str, Any]:
    base_url, secret_keys = KINDS.get(link.kind, ("", ()))
    portfolios = (await session.execute(
        select(Portfolio.id, Portfolio.name)
        .where(Portfolio.broker_link_id == link.id).order_by(Portfolio.name)
    )).all()
    return {
        "id": str(link.id),
        "name": link.name,
        "kind": link.kind,
        "enabled": link.enabled,
        "status": link.status,
        "last_error": link.last_error,
        "base_url": base_url,
        "config_masked": mask_config(await get_link_config(session, link.id)),
        "secret_keys": list(secret_keys),
        "portfolios": [{"id": str(pid), "name": name} for pid, name in portfolios],
        "created_at": to_jsonable(link.created_at),
    }


async def _record(session: AsyncSession, redis, link_id: UUID, error: str | None) -> None:
    async with _transaction(session, redis):
        link = await _link(session, link_id)
        link.status, link.last_error = ("ok", None) if error is None else ("error", error)


def link_secrets(kind: str, config: dict[str, Any]) -> list[str]:
    """The texts to mask in broker errors: every secret of the config and, for T212, the
    Base64 Basic credential the adapter sends (longest first)."""
    found = secret_values(config)
    if kind == "t212_demo" and config.get("api_key"):
        cred = f"{config.get('api_key', '')}:{config.get('api_secret', '')}"
        found.append(base64.b64encode(cred.encode()).decode())
    return sorted(set(found), key=len, reverse=True)


async def _secrets(session: AsyncSession, link: BrokerLink) -> list[str]:
    return link_secrets(link.kind, await get_link_config(session, link.id))


def _error_text(e: Exception, secrets: list[str]) -> str:
    if isinstance(e, TimeoutError):
        return f"timeout dopo {BROKER_TIMEOUT_S:g} s"
    if isinstance(e, SMTError):
        return redact(e.message, secrets)[:500]
    return redact(f"{type(e).__name__}: {e}", secrets)[:500]


def _redacted_smterror(e: SMTError, secrets: list[str]) -> SMTError:
    return SMTError(e.code, _error_text(e, secrets),
                    {k: redact(v, secrets) if isinstance(v, str) else v
                     for k, v in (e.details or {}).items()} or None)


# --- links ---------------------------------------------------------------------------


@router.get("/broker-links")
async def get_links(session: DbSession) -> list[dict]:
    return [await _dto(session, link) for link in await list_links(session)]


@router.post("/broker-links")
async def post_link(
    body: BrokerLinkIn, session: DbSession, redis: RedisClient, idem: BrokerIdem
):
    async def run() -> dict:
        if body.kind not in KINDS:
            raise _invalid("kind", "tipo di broker non supportato (alpaca_paper, t212_demo)")
        name = _name(body.name)
        config = _clean_config(body.kind, body.config)
        async with _transaction(session, redis):
            link = await create_link(session, name, body.kind, config)
        return await _dto(session, link)

    return await idem.run(run)


@router.patch("/broker-links/{link_id}")
async def patch_link(
    link_id: UUID, body: BrokerLinkPatchIn, session: DbSession, redis: RedisClient
) -> dict:
    fields = body.model_fields_set
    for f in ("name", "enabled", "config"):
        if f in fields and getattr(body, f) is None:
            raise _invalid(f, f"{f} non può essere nullo")
    async with _transaction(session, redis):
        link = await _link(session, link_id)
        name = None
        if body.name is not None:
            name = _name(body.name)
            taken = await session.scalar(
                select(BrokerLink.id).where(BrokerLink.name == name, BrokerLink.id != link_id)
            )
            if taken is not None:
                raise _invalid("name", "esiste già un collegamento con questo nome")
        config = None
        if body.config is not None:
            stored = await get_link_config(session, link_id)
            config = _clean_config(link.kind, merge_config(body.config, stored))
        await update_link(session, link_id, name=name, enabled=body.enabled, config=config)
    return await _dto(session, link)


@router.post("/broker-links/{link_id}/test")
async def test_link(
    link_id: UUID, session: DbSession, redis: RedisClient, idem: BrokerIdem
):
    """The adapter's health check with the stored config; the outcome goes to the link."""

    async def run() -> dict:
        secrets = await _secrets(session, await _link(session, link_id))
        error = None
        try:
            async with httpx.AsyncClient() as http, open_adapter(session, link_id, http) as adapter:
                await session.commit()  # no transaction across the network call
                await asyncio.wait_for(adapter.health_check(), BROKER_TIMEOUT_S)
        except Exception as e:  # noqa: BLE001 - reported to the admin and recorded
            error = _error_text(e, secrets)
        await _record(session, redis, link_id, error)
        return {"ok": error is None, "error": error, "checked_at": datetime.now(UTC).isoformat()}

    return await idem.run(run)


@router.get("/broker-links/{link_id}/positions")
async def get_broker_positions(link_id: UUID, session: DbSession, redis: RedisClient) -> dict:
    """The outcome is recorded on the link: a failure (answered redacted) or ok."""
    secrets = await _secrets(session, await _link(session, link_id))
    try:
        async with httpx.AsyncClient() as http, open_adapter(session, link_id, http) as adapter:
            await session.commit()
            await asyncio.wait_for(adapter.prepare(), BROKER_TIMEOUT_S)
            positions = await asyncio.wait_for(adapter.get_positions(), BROKER_TIMEOUT_S)
    except SMTError as e:
        await session.rollback()
        await _record(session, redis, link_id, _error_text(e, secrets))
        raise _redacted_smterror(e, secrets) from None
    except Exception as e:  # noqa: BLE001 - the broker could not be read
        await session.rollback()
        error = _error_text(e, secrets)
        await _record(session, redis, link_id, error)
        raise SMTError(ErrorCode.PROVIDER_UNAVAILABLE, error) from None
    await _record(session, redis, link_id, None)  # the broker answered: the link is ok
    return {"positions": [
        {"symbol": s, "qty": to_jsonable(Decimal(q))} for s, q in sorted(positions.items())
    ]}


# --- portfolio link ------------------------------------------------------------------


@router.put("/portfolios/{portfolio_id}/broker-link")
async def put_portfolio_link(
    portfolio_id: UUID, body: PortfolioLinkIn, session: DbSession, redis: RedisClient
) -> dict:
    """Link (or unlink, null) a portfolio. The service refuses the change while an order
    of the portfolio is (or may be) at the broker (broker_live: cancel it and wait for the
    confirmation); routed orders not sent yet go back to the simulator on unlink."""
    async with _transaction(session, redis):
        if body.broker_link_id is not None:
            link = await _link(session, body.broker_link_id)
            if not link.enabled:
                raise _invalid("broker_link_id", "collegamento broker disattivato")
        portfolio = await link_portfolio(session, portfolio_id, body.broker_link_id)
        out = _portfolio_dto(portfolio)
    return out


# --- reconciliations -----------------------------------------------------------------


def _reconciliation_dto(r: Reconciliation, portfolio_name: str | None) -> dict[str, Any]:
    return {
        "id": str(r.id),
        "broker_link_id": str(r.broker_link_id),
        "portfolio_id": str(r.portfolio_id),
        "portfolio_name": portfolio_name,
        "ts": to_jsonable(r.ts),
        "ok": r.ok,
        "diffs": r.diffs,
    }


@router.get("/broker-links/{link_id}/reconciliations")
async def get_reconciliations(
    link_id: UUID, session: DbSession, limit: int = DEFAULT_RECONCILIATIONS
) -> list[dict]:
    """Newest first."""
    n = parse_int(limit, "limit", 1, MAX_RECONCILIATIONS)
    await _link(session, link_id)
    rows = (await session.execute(
        select(Reconciliation, Portfolio.name)
        .join(Portfolio, Portfolio.id == Reconciliation.portfolio_id)
        .where(Reconciliation.broker_link_id == link_id)
        .order_by(Reconciliation.ts.desc(), Reconciliation.id)
        .limit(n)
    )).all()
    return [_reconciliation_dto(r, name) for r, name in rows]


@router.post("/broker-links/{link_id}/reconcile")
async def post_reconcile(
    link_id: UUID, request: Request, session: DbSession, redis: RedisClient, idem: BrokerIdem
):
    """Reconcile now (one row per linked portfolio); the outcome goes to the link status.
    A broker failure is recorded and answered with PROVIDER_UNAVAILABLE."""

    async def run() -> dict:
        secrets = await _secrets(session, await _link(session, link_id))
        try:
            await session.commit()  # reconcile uses its own sessions (no tx across I/O)
            async with httpx.AsyncClient() as http:
                rows = await asyncio.wait_for(
                    reconcile(request.app.state.sessionmaker, link_id, http), BROKER_TIMEOUT_S
                )
            names = dict((await session.execute(
                select(Portfolio.id, Portfolio.name)
                .where(Portfolio.id.in_([r.portfolio_id for r in rows]))
            )).all())
            out = {"reconciliations": [_reconciliation_dto(r, names.get(r.portfolio_id))
                                       for r in rows]}
            link = await _link(session, link_id)
            link.status, link.last_error = "ok", None
            await session.commit()
        except SMTError as e:
            await session.rollback()
            await _record(session, redis, link_id, _error_text(e, secrets))
            raise _redacted_smterror(e, secrets) from None
        except Exception as e:  # noqa: BLE001 - recorded on the link, reported redacted
            await session.rollback()
            error = _error_text(e, secrets)
            await _record(session, redis, link_id, error)
            raise SMTError(ErrorCode.PROVIDER_UNAVAILABLE, error) from None
        return out

    return await idem.run(run)
