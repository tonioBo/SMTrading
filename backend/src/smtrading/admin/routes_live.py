"""Admin API of real money («denaro reale»): the link wizard, the real portfolio's TOTP
actions, the live audit (with its CSV) and the status for the banner.

    GET  /api/live-links                       the real-money links (masked)
    POST /api/live-links                       new link (keys) -> configured
    PUT  /api/live-links/{id}/keys             new keys -> configured (new test + TOTP)
    POST /api/live-links/{id}/test             read-only test (GET only)
    POST /api/live-links/{id}/dry-run          {on}
    POST /api/live-links/{id}/arm              {totp, confirm_name, accept_risks, ...}
    POST /api/live-links/{id}/disarm           {reason?}: one click, no TOTP
    PUT  /api/portfolios/{id}/live-link        {link_id|null, totp}: link (TOTP) / unlink
    POST /api/portfolios/{id}/live-resume      {totp}
    POST /api/portfolios/{id}/live-flatten     {totp}
    GET  /api/live-audit, /api/live-audit.csv  newest first, filters
    GET  /api/live/status                      counts, real portfolios, telegram_ok

Every route needs the admin session (and the CSRF token on writes); the service layer
(brokers.live.service) does the checks, the TOTP and the audit. Answers never contain a
key: only `key_masked` (`••••` + the key ID's last 4) and the masked account ID; broker
errors are redacted of the link's keys (and of the Trading 212 Basic credential).
`app.state.live_http_factory(link_id) -> LiveClient` (tests only) replaces the default
make_live_client: the live gate is always in front of the broker.
"""

import asyncio
import csv
import io
import json
from collections.abc import AsyncIterator, Callable
from datetime import timedelta
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from smtrading.admin.auth import (
    CurrentAdmin,
    CurrentUser,
    DbSession,
    RedisClient,
    require_access,
    require_user,
)
from smtrading.admin.common import MASK, Quotes, _invalid, _transaction, redact
from smtrading.admin.routes_portfolios import _csv_text, portfolio_dto
from smtrading.brokers.live import LIVE_KINDS
from smtrading.brokers.live.account import decrypt_live_config, key_masked, live_secrets
from smtrading.brokers.live.gate import LiveClient, make_live_client
from smtrading.brokers.live.models import LIVE_AUDIT_ACTIONS, LiveAudit, LiveState
from smtrading.brokers.live.service import (
    Actor,
    arm_live_link,
    disarm_live_link_now,
    flatten_live_portfolio,
    link_live_portfolio,
    resume_live_portfolio,
    save_live_keys,
    set_dry_run,
    test_live_link,
)
from smtrading.brokers.models import BrokerLink
from smtrading.common.errors import ErrorCode, SMTError
from smtrading.mcp.dto import parse_int, to_jsonable
from smtrading.portfolios.models import Portfolio
from smtrading.telegram.config import is_ready, load_config
from smtrading.telegram.models import OutboxStatus, TelegramOutbox

router = APIRouter(prefix="/api", tags=["live"], dependencies=[Depends(require_access)])
# protective actions every user may take, viewers included: «Disattiva denaro reale»
emergency_router = APIRouter(prefix="/api", tags=["live"], dependencies=[Depends(require_user)])

LIVE_TIMEOUT_S = 30.0
DEFAULT_AUDIT_LIMIT, MAX_AUDIT_LIMIT = 50, 200
CSV_BATCH = 500
CSV_HEADER = ("id", "ts", "collegamento", "portafoglio", "ordine", "attore", "azione", "ip",
              "dettagli")
DISARM_REASON = "disattivato dall'admin"
TELEGRAM_STUCK_AFTER = timedelta(minutes=5)  # a live message not sent by then: not ok
SNAPSHOT_FIELDS = ("currency", "cash_available", "positions_count", "open_orders_count",
                   "crypto_enabled", "order_types")
_KEY_FIELDS = ("key_id", "secret_key", "api_key", "api_secret", "user_key")


class LiveKeysIn(BaseModel):
    """Alpaca: key_id + secret_key (+ crypto_enabled); Trading 212: api_key + api_secret
    (+ order_types); eToro: api_key + user_key. `name`/`kind` are required for a new
    link."""

    name: str | None = None
    kind: str | None = None
    key_id: str | None = None
    secret_key: str | None = None
    api_key: str | None = None
    api_secret: str | None = None
    user_key: str | None = None
    crypto_enabled: bool | None = None
    order_types: list[str] | None = None

    def config(self) -> dict[str, Any]:
        fields = (*_KEY_FIELDS, "crypto_enabled", "order_types")
        return {f: getattr(self, f) for f in fields if getattr(self, f) is not None}


class DryRunIn(BaseModel):
    on: bool


class ArmIn(BaseModel):
    totp: str | None = None
    confirm_name: str = ""
    accept_risks: bool = False
    dedicated_account: bool = False
    limits_set: bool = False


class LiveLinkIn(BaseModel):
    link_id: UUID | None
    totp: str | None = None


class TotpIn(BaseModel):
    totp: str | None = None


# --- shared helpers (also used by admin.routes_brokers for live links) -------------------


def live_http_factory(request: Request) -> Callable[[UUID], LiveClient]:
    """`link_id -> LiveClient` (the live gate in front of the broker)."""
    factory = getattr(request.app.state, "live_http_factory", None)
    if factory is not None:
        return factory
    sessionmaker = request.app.state.sessionmaker
    return lambda link_id: make_live_client(sessionmaker, link_id)


def request_actor(request: Request, admin) -> Actor:
    return Actor(email=admin.email, ip=request.client.host if request.client else None,
                 user_agent=request.headers.get("user-agent"))


async def run_live_test(request: Request, link_id: UUID, actor: Actor) -> dict[str, Any]:
    """The read-only test of the live flow on the link's gated client (own sessions)."""
    async with live_http_factory(request)(link_id) as http:
        return await test_live_link(request.app.state.sessionmaker, link_id, actor=actor,
                                    http=http)


def _link_secrets(link: BrokerLink) -> list[str]:
    try:
        return live_secrets(link.kind, decrypt_live_config(link))
    except SMTError:
        return []


def _error_text(e: Exception, secrets: list[str]) -> str:
    if isinstance(e, TimeoutError):
        return f"timeout dopo {LIVE_TIMEOUT_S:g} s"
    return redact(f"{type(e).__name__}: {e}", secrets)[:300]


def _redacted(e: SMTError, secrets: list[str]) -> SMTError:
    return SMTError(e.code, redact(e.message, secrets),
                    {k: redact(v, secrets) if isinstance(v, str) else v
                     for k, v in (e.details or {}).items()} or None)


async def _live_link(session: AsyncSession, link_id: UUID) -> BrokerLink:
    link = await session.get(BrokerLink, link_id)
    if link is None or link.kind not in LIVE_KINDS:
        raise SMTError(ErrorCode.NOT_FOUND, "collegamento reale non trovato",
                       {"link_id": str(link_id)})
    return link


async def _live_dto(session: AsyncSession, link: BrokerLink) -> dict[str, Any]:
    try:
        masked = key_masked(link.kind, decrypt_live_config(link))
    except SMTError:  # keys that cannot be read (another master key): nothing shown
        masked = MASK
    snap = link.last_account_snapshot if isinstance(link.last_account_snapshot, dict) else None
    pf = (await session.execute(
        select(Portfolio.id, Portfolio.name).where(Portfolio.broker_link_id == link.id)
        .order_by(Portfolio.name).limit(1))).first()
    return {
        "id": str(link.id),
        "name": link.name,
        "kind": link.kind,
        "live_state": link.live_state,
        "armed_at": to_jsonable(link.armed_at),
        "armed_by": link.armed_by,
        "disarmed_at": to_jsonable(link.disarmed_at),
        "disarmed_reason": link.disarmed_reason,
        "key_masked": masked,
        "account_masked": snap.get("account_masked") if snap else None,
        "snapshot": {f: snap.get(f) for f in SNAPSHOT_FIELDS} if snap else None,
        "last_test_at": to_jsonable(link.last_test_at),
        "health_failures": link.health_failures or 0,
        "portfolio": {"id": str(pf.id), "name": pf.name} if pf else None,
    }


async def _fresh_dto(session: AsyncSession, link_id: UUID) -> dict[str, Any]:
    session.expire_all()
    return await _live_dto(session, await _live_link(session, link_id))


# --- links ----------------------------------------------------------------------------


@router.get("/live-links")
async def get_live_links(session: DbSession) -> list[dict]:
    links = await session.scalars(
        select(BrokerLink).where(BrokerLink.kind.in_(LIVE_KINDS)).order_by(BrokerLink.name))
    return [await _live_dto(session, link) for link in links]


@router.post("/live-links")
async def post_live_link(body: LiveKeysIn, request: Request, admin: CurrentAdmin,
                         session: DbSession, redis: RedisClient) -> dict:
    if not body.kind or body.kind not in LIVE_KINDS:
        raise _invalid("kind", "tipo di collegamento reale: " + ", ".join(sorted(LIVE_KINDS)))
    async with _transaction(session, redis):
        link = await save_live_keys(session, name=body.name or "", kind=body.kind,
                                    config=body.config(), actor=request_actor(request, admin))
        link_id = link.id
    return await _fresh_dto(session, link_id)


@router.put("/live-links/{link_id}/keys")
async def put_live_keys(link_id: UUID, body: LiveKeysIn, request: Request,
                        admin: CurrentAdmin, session: DbSession, redis: RedisClient) -> dict:
    """New keys: the link goes back to `configured` (disarmed first if it was armed)."""
    async with _transaction(session, redis):
        link = await _live_link(session, link_id)
        await save_live_keys(session, name=body.name or link.name, kind=link.kind,
                             config=body.config(), actor=request_actor(request, admin),
                             link_id=link_id)
    return await _fresh_dto(session, link_id)


@router.post("/live-links/{link_id}/test")
async def post_live_test(link_id: UUID, request: Request, admin: CurrentAdmin,
                         session: DbSession) -> dict:
    """The read-only test (GET only): the account snapshot, `empty` (R8.3)."""
    link = await _live_link(session, link_id)
    secrets = _link_secrets(link)
    await session.commit()  # no transaction across the network call
    try:
        return await asyncio.wait_for(
            run_live_test(request, link_id, request_actor(request, admin)), LIVE_TIMEOUT_S)
    except SMTError as e:
        raise _redacted(e, secrets) from None
    except Exception as e:  # noqa: BLE001 - answered redacted, never with the keys
        raise SMTError(ErrorCode.PROVIDER_UNAVAILABLE,
                       f"test del collegamento non riuscito: {_error_text(e, secrets)}",
                       {"link_id": str(link_id)}) from None


@router.post("/live-links/{link_id}/dry-run")
async def post_dry_run(link_id: UUID, body: DryRunIn, request: Request, admin: CurrentAdmin,
                       session: DbSession, redis: RedisClient) -> dict:
    async with _transaction(session, redis):
        await set_dry_run(session, link_id, body.on, actor=request_actor(request, admin))
    return await _fresh_dto(session, link_id)


@router.post("/live-links/{link_id}/arm")
async def post_arm(link_id: UUID, body: ArmIn, request: Request, admin: CurrentAdmin,
                   session: DbSession, redis: RedisClient, qs: Quotes) -> dict:
    """«Attiva denaro reale»: TOTP, the three confirmations and the link's name."""
    async with _transaction(session, redis):
        await arm_live_link(
            session, redis, link_id, admin=admin, totp=body.totp,
            confirm_name=body.confirm_name, accept_risks=body.accept_risks,
            dedicated_account=body.dedicated_account, limits_set=body.limits_set,
            actor=request_actor(request, admin), qs=qs)
    return await _fresh_dto(session, link_id)


@emergency_router.post("/live-links/{link_id}/disarm")
async def post_disarm(link_id: UUID, request: Request, user: CurrentUser,
                      session: DbSession, redis: RedisClient) -> dict:
    """«Disattiva denaro reale»: one click, no TOTP, any body (an optional JSON
    `{"reason": "..."}`: a reason that is not a text is ignored). Two phases (R8.12): the
    link is `configured` once this answers, `settled` false if its orders could not be
    settled (alerted; disarm again to retry)."""
    reason = await _disarm_reason(request)
    await _live_link(session, link_id)  # 404 before anything changes
    await session.commit()
    orders = await disarm_live_link_now(request.app.state.sessionmaker, link_id,
                                        actor=request_actor(request, user), reason=reason,
                                        redis=redis)
    return {**await _fresh_dto(session, link_id), "orders": orders,
            "settled": orders is not None}


async def _disarm_reason(request: Request) -> str:
    try:
        data = json.loads(await request.body() or b"null")
    except (ValueError, UnicodeDecodeError):
        data = None
    reason = data.get("reason") if isinstance(data, dict) else None
    return (reason.strip() if isinstance(reason, str) else "") or DISARM_REASON


# --- the real portfolio ------------------------------------------------------------------


@router.put("/portfolios/{portfolio_id}/live-link")
async def put_live_link(portfolio_id: UUID, body: LiveLinkIn, request: Request,
                        admin: CurrentAdmin, session: DbSession, redis: RedisClient,
                        qs: Quotes) -> dict:
    """Link to a real-money link (`dry_run`/`armed`, TOTP) or, with null, unlink from it
    (no TOTP: refused while real positions are open)."""
    from smtrading.brokers.service import link_portfolio

    actor = request_actor(request, admin)
    async with _transaction(session, redis):
        if body.link_id is not None:
            pf = await link_live_portfolio(session, redis, portfolio_id, body.link_id,
                                           admin=admin, totp=body.totp, actor=actor, qs=qs)
        else:
            kind = await session.scalar(
                select(BrokerLink.kind)
                .join(Portfolio, Portfolio.broker_link_id == BrokerLink.id)
                .where(Portfolio.id == portfolio_id))
            if kind not in LIVE_KINDS:
                raise _invalid("link_id", "il portafoglio non è collegato a un conto reale")
            pf = await link_portfolio(session, portfolio_id, None, actor=actor)
        out = await portfolio_dto(session, pf)
    return out


@router.post("/portfolios/{portfolio_id}/live-resume")
async def post_live_resume(portfolio_id: UUID, body: TotpIn, request: Request,
                           admin: CurrentAdmin, session: DbSession, redis: RedisClient) -> dict:
    """«Ho verificato, riprendi» (TOTP)."""
    async with _transaction(session, redis):
        pf = await resume_live_portfolio(session, redis, portfolio_id, admin=admin,
                                         totp=body.totp, actor=request_actor(request, admin))
        out = await portfolio_dto(session, pf)
    return out


@router.post("/portfolios/{portfolio_id}/live-flatten")
async def post_live_flatten(portfolio_id: UUID, body: TotpIn, request: Request,
                            admin: CurrentAdmin, session: DbSession, redis: RedisClient,
                            qs: Quotes) -> dict:
    """«Chiudi tutte le posizioni reali» (TOTP): the ids of the orders placed."""
    async with _transaction(session, redis):
        orders = await flatten_live_portfolio(session, redis, qs, portfolio_id, admin=admin,
                                              totp=body.totp,
                                              actor=request_actor(request, admin))
    return {"orders": [str(o) for o in orders]}


# --- live audit -------------------------------------------------------------------------


def _audit_query(link_id: UUID | None, portfolio_id: UUID | None,
                 action: str | None) -> Select:
    if action is not None and action not in LIVE_AUDIT_ACTIONS:
        raise _invalid("action", f"azione sconosciuta: {action}")
    q = (select(LiveAudit, BrokerLink, Portfolio.name)
         .join(BrokerLink, BrokerLink.id == LiveAudit.link_id)
         .outerjoin(Portfolio, Portfolio.id == LiveAudit.portfolio_id)
         .order_by(LiveAudit.id.desc()))
    if link_id is not None:
        q = q.where(LiveAudit.link_id == link_id)
    if portfolio_id is not None:
        q = q.where(LiveAudit.portfolio_id == portfolio_id)
    if action is not None:
        q = q.where(LiveAudit.action == action)
    return q


class _Scrubber:
    """The audit details are stored redacted (secret-named fields); as a second line,
    any value equal to a key of the row's link is masked too (one decrypt per link)."""

    def __init__(self) -> None:
        self._secrets: dict[UUID, list[str]] = {}

    def details(self, row: LiveAudit, link: BrokerLink) -> Any:
        if link.id not in self._secrets:
            self._secrets[link.id] = _link_secrets(link)
        secrets = self._secrets[link.id]
        if not secrets:
            return row.details
        return json.loads(redact(json.dumps(row.details, ensure_ascii=False), secrets))


def _audit_dto(row: LiveAudit, link: BrokerLink, portfolio_name: str | None,
               scrub: _Scrubber) -> dict[str, Any]:
    return {
        "id": row.id, "ts": to_jsonable(row.ts),
        "link_id": str(row.link_id), "link_name": link.name,
        "portfolio_id": to_jsonable(row.portfolio_id), "portfolio_name": portfolio_name,
        "order_id": to_jsonable(row.order_id), "actor": row.actor, "action": row.action,
        "ip": row.ip, "user_agent": row.user_agent, "details": scrub.details(row, link),
    }


@router.get("/live-audit")
async def get_live_audit(
    session: DbSession, link_id: UUID | None = None, portfolio_id: UUID | None = None,
    action: str | None = None, before_id: int | None = None, limit: int = DEFAULT_AUDIT_LIMIT,
) -> list[dict]:
    """Newest first; `before_id` pages back."""
    n = parse_int(limit, "limit", 1, MAX_AUDIT_LIMIT)
    before = parse_int(before_id, "before_id", 1)
    q = _audit_query(link_id, portfolio_id, action)
    if before is not None:
        q = q.where(LiveAudit.id < before)
    scrub = _Scrubber()
    rows = (await session.execute(q.limit(n))).all()
    return [_audit_dto(r, link, name, scrub) for r, link, name in rows]


@router.get("/live-audit.csv")
async def get_live_audit_csv(
    request: Request, session: DbSession, link_id: UUID | None = None,
    portfolio_id: UUID | None = None, action: str | None = None,
) -> StreamingResponse:
    """Every row matching the filters, newest first: `;` separated, UTF-8 with BOM, the
    details as JSON (redacted like the API)."""
    query = _audit_query(link_id, portfolio_id, action)
    sessionmaker = request.app.state.sessionmaker

    def encode(rows: list[tuple]) -> str:
        buf = io.StringIO()
        csv.writer(buf, delimiter=";", lineterminator="\r\n").writerows(rows)
        return buf.getvalue()

    async def generate() -> AsyncIterator[bytes]:
        yield ("﻿" + encode([CSV_HEADER])).encode()
        scrub = _Scrubber()
        before: int | None = None
        async with sessionmaker() as s:  # the request session may be closed by now
            while True:
                q = query if before is None else query.where(LiveAudit.id < before)
                batch = (await s.execute(q.limit(CSV_BATCH))).all()
                if not batch:
                    return
                yield encode([
                    (r.id, to_jsonable(r.ts), _csv_text(link.name), _csv_text(name),
                     to_jsonable(r.order_id) or "", _csv_text(r.actor), r.action,
                     _csv_text(r.ip),
                     _csv_text(json.dumps(scrub.details(r, link), ensure_ascii=False,
                                          sort_keys=True)))
                    for r, link, name in batch
                ]).encode()
                before = batch[-1][0].id

    return StreamingResponse(
        generate(), media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="registro-denaro-reale.csv"'},
    )


# --- status -------------------------------------------------------------------------------


async def _telegram_ok(session: AsyncSession) -> bool:
    """Telegram configured, enabled, not paused, and no real-money message stuck."""
    cfg = await load_config(session)
    if not is_ready(cfg) or cfg.paused or cfg.unreadable:
        return False
    stuck = await session.scalar(
        select(TelegramOutbox.id).where(
            TelegramOutbox.category == "live",
            TelegramOutbox.status.in_((OutboxStatus.pending, OutboxStatus.failed)),
            TelegramOutbox.created_at < func.now() - TELEGRAM_STUCK_AFTER,
        ).limit(1))
    return stuck is None


@router.get("/live/status")
async def get_live_status(session: DbSession) -> dict:
    """For the banner and the fixed Telegram warning."""
    counts = dict((await session.execute(
        select(BrokerLink.live_state, func.count())
        .where(BrokerLink.kind.in_(LIVE_KINDS)).group_by(BrokerLink.live_state))).all())
    portfolios = (await session.execute(
        select(Portfolio.id, Portfolio.name, BrokerLink.live_state)
        .join(BrokerLink, BrokerLink.id == Portfolio.broker_link_id)
        .where(BrokerLink.kind.in_(LIVE_KINDS)).order_by(Portfolio.name))).all()
    return {
        "armed_count": counts.get(LiveState.armed, 0),
        "dry_run_count": counts.get(LiveState.dry_run, 0),
        "portfolios": [{"id": str(pid), "name": name, "live_state": state}
                       for pid, name, state in portfolios],
        "telegram_ok": await _telegram_ok(session),
    }
