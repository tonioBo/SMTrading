"""Admin API «Notifiche»: Telegram configuration (token encrypted, never returned), test
message, pause, outbox and the agents' decisions log.

The bot token never reaches a response, an error, the audit or a log: Telegram failures
arrive as `TelegramError` (Italian, already redacted) and become 400 `INVALID_ORDER` with
`details.field`. `apply_config` is shared with the CLI (`telegram set`).
"""

import re
from datetime import UTC, datetime
from typing import Annotated, Any
from uuid import UUID

import httpx
from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from smtrading.activity.models import Decision, DecisionKind
from smtrading.activity.service import list_decisions
from smtrading.admin.auth import CurrentAdmin, DbSession, RedisClient, require_admin
from smtrading.admin.common import _invalid, _transaction
from smtrading.agents.models import Agent
from smtrading.agents.service import write_audit
from smtrading.common.errors import SMTError
from smtrading.mcp.dto import to_jsonable
from smtrading.portfolios.models import Portfolio
from smtrading.risk.models import AppSetting
from smtrading.telegram.client import TelegramClient, TelegramError
from smtrading.telegram.config import (
    SETTINGS_KEY,
    UNREADABLE_MESSAGE,
    TelegramConfig,
    config_dto,
    load_config,
    save_config,
)
from smtrading.telegram.models import OutboxStatus, TelegramOutbox
from smtrading.telegram.outbox import resume_paused, retry_outbox

router = APIRouter(prefix="/api", tags=["telegram"], dependencies=[Depends(require_admin)])

HTTP_TIMEOUT_S = 10.0
DEFAULT_LIMIT, MAX_LIMIT = 50, 200
TEST_TEXT = "🟢 Messaggio di prova da SMTrading"
_CHAT_ID = re.compile(r"-?\d{1,20}|@[A-Za-z][A-Za-z0-9_]{3,31}")
_CHAT_TYPES = ("group", "supergroup", "channel")
_UPDATE_KEYS = ("message", "edited_message", "channel_post", "edited_channel_post",
                "my_chat_member", "chat_member")


class TelegramConfigIn(BaseModel):
    """Only the fields present in the request are changed (`model_fields_set`)."""

    bot_token: str | None = None
    chat_id: str | int | None = None
    enabled: bool | None = None


class DetectChatsIn(BaseModel):
    bot_token: str | None = None


class PausedIn(BaseModel):
    paused: bool


# --- helpers shared with the CLI -----------------------------------------------------------

def clean_token(token: str) -> str:
    """Strip and drop every whitespace character (a pasted token often carries some)."""
    cleaned = "".join(token.split())
    if not cleaned:
        raise _invalid("bot_token", "inserisci il token del bot")
    return cleaned


def clean_chat_id(chat_id: str | int) -> str:
    text = str(chat_id).strip()
    if isinstance(chat_id, bool) or not _CHAT_ID.fullmatch(text):
        raise _invalid("chat_id", "ID del gruppo non valido (es. -1001234567890)")
    return text


def _client(token: str, http: httpx.AsyncClient) -> TelegramClient:
    try:
        return TelegramClient(token, http, timeout=HTTP_TIMEOUT_S)
    except TelegramError as e:
        raise _invalid("bot_token", e.message) from None


async def _lock_settings(session: AsyncSession) -> None:
    """Serialises concurrent writers of the telegram settings row (if it exists)."""
    await session.execute(
        select(AppSetting.key).where(AppSetting.key == SETTINGS_KEY).with_for_update()
    )


async def _audit(session: AsyncSession, method: str, by: str, **params: Any) -> None:
    await write_audit(session, agent_id=None, method=method, params={"by": by, **params},
                      outcome="ok", latency_ms=0)


async def apply_config(
    session: AsyncSession, http: httpx.AsyncClient, *, fields: set[str],
    bot_token: str | None = None, chat_id: str | int | None = None,
    enabled: bool | None = None, verify: bool = True, by: str,
) -> TelegramConfig:
    """Validates, verifies with Telegram (getMe with the effective token, getChat with the
    effective chat) when the token or the chat changes, then saves and audits — all in the
    caller's transaction, which must be committed by the caller. Nothing is saved on error.
    While the stored token is unreadable every update must re-enter the token."""
    for name in fields & {"bot_token", "chat_id", "enabled"}:
        if {"bot_token": bot_token, "chat_id": chat_id, "enabled": enabled}[name] is None:
            raise _invalid(name, f"{name} non può essere nullo")
    new_token = clean_token(bot_token) if "bot_token" in fields else None
    new_chat = clean_chat_id(chat_id) if "chat_id" in fields else None

    current = await load_config(session)
    if current.unreadable and new_token is None:
        raise _invalid("bot_token", UNREADABLE_MESSAGE)
    token = new_token or current.bot_token
    chat = new_chat or current.chat_id
    token_changed = new_token is not None and new_token != current.bot_token
    chat_changed = new_chat is not None and new_chat != current.chat_id
    if new_chat is not None and token is None:
        raise _invalid("bot_token", "inserisci prima il token del bot")

    username, title = current.bot_username, current.chat_title
    if token_changed or chat_changed:
        username = None if token_changed else username
        title = None if (chat_changed or token_changed) else title
        if verify:
            client = _client(token, http)
            try:
                username = (await client.get_me()).get("username")
            except TelegramError as e:
                raise _invalid("bot_token", e.message) from None
            if chat is not None:
                try:
                    info = await client.get_chat(chat)
                except TelegramError as e:
                    raise _invalid("chat_id", e.message) from None
                title = info.get("title")
                if info.get("id") is not None:  # an @username resolves to the numeric id
                    chat = str(info["id"])

    await _lock_settings(session)
    fresh = await load_config(session)  # pause may have changed during the verification
    if new_chat is None and fresh.chat_id != current.chat_id:
        # the sender migrated the group to a supergroup meanwhile: keep its new id
        chat, title = fresh.chat_id, fresh.chat_title
    cfg = TelegramConfig(
        bot_token=token, chat_id=chat,
        enabled=fresh.enabled if enabled is None else enabled,
        paused=fresh.paused, bot_username=username, chat_title=title,
    )
    if cfg.enabled and not (cfg.bot_token and cfg.chat_id):
        raise _invalid("enabled", "per attivare le notifiche servono il token e il gruppo")
    changed = [name for name, yes in (
        ("bot_token", token_changed), ("chat_id", new_chat is not None and cfg.chat_id != current.chat_id),
        ("enabled", cfg.enabled != current.enabled),
    ) if yes]
    await save_config(session, cfg)
    await _audit(session, "admin:telegram.config", by, changed=changed,
                 chat_id=cfg.chat_id, enabled=cfg.enabled)
    return cfg


async def _telegram_dto(session: AsyncSession) -> dict[str, Any]:
    counts = dict((await session.execute(
        select(TelegramOutbox.status, func.count()).where(TelegramOutbox.status.in_(
            [OutboxStatus.pending, OutboxStatus.paused, OutboxStatus.failed]
        )).group_by(TelegramOutbox.status)
    )).all())
    return {**config_dto(await load_config(session)),
            "outbox": {s: counts.get(s, 0) for s in ("pending", "paused", "failed")}}


async def _agent_names(session: AsyncSession, ids: set[UUID]) -> dict[UUID, str]:
    if not ids:
        return {}
    return dict((await session.execute(select(Agent.id, Agent.name).where(Agent.id.in_(ids)))).all())


def _source_agent(source: str) -> UUID | None:
    if source.startswith("agent:"):
        try:
            return UUID(source.removeprefix("agent:"))
        except ValueError:
            return None
    return None


async def _outbox_dtos(session: AsyncSession, rows: list[TelegramOutbox]) -> list[dict]:
    names = await _agent_names(session, {a for r in rows if (a := _source_agent(r.source))})
    fields = ("id", "source", "category", "level", "text", "status", "attempts", "last_error",
              "created_at", "sent_at")
    return [
        {**{f: to_jsonable(getattr(r, f)) for f in fields},
         "agent": names.get(_source_agent(r.source))}  # type: ignore[arg-type]
        for r in rows
    ]


# --- routes ----------------------------------------------------------------------------------

@router.get("/telegram")
async def get_telegram(session: DbSession) -> dict:
    return await _telegram_dto(session)


@router.put("/telegram/config")
async def put_telegram_config(
    body: TelegramConfigIn, admin: CurrentAdmin, session: DbSession, redis: RedisClient
) -> dict:
    async with httpx.AsyncClient() as http, _transaction(session, redis):
        await apply_config(
            session, http, fields=set(body.model_fields_set), bot_token=body.bot_token,
            chat_id=body.chat_id, enabled=body.enabled, by=admin.email,
        )
    return await _telegram_dto(session)


@router.post("/telegram/detect-chats")
async def detect_chats(body: DetectChatsIn, session: DbSession) -> dict:
    """Groups, supergroups and channels seen in the bot's pending updates (read only: the
    updates are not consumed), newest first, one per id. Only the updates Telegram still
    holds are seen: if a group is missing, write `/start@<bot>` in it again."""
    if body.bot_token is not None:
        token = clean_token(body.bot_token)
    else:
        cfg = await load_config(session)
        if cfg.unreadable:
            raise _invalid("bot_token", UNREADABLE_MESSAGE)
        if not cfg.bot_token:
            raise _invalid("bot_token", "inserisci il token del bot")
        token = cfg.bot_token
    async with httpx.AsyncClient() as http:
        try:
            updates = await _client(token, http).get_updates(100)
        except TelegramError as e:
            raise _invalid("bot_token", e.message) from None
    chats: dict[Any, dict[str, Any]] = {}
    ordered = sorted((u for u in updates if isinstance(u, dict)),
                     key=lambda u: u.get("update_id") or 0, reverse=True)
    for update in ordered:
        for key in _UPDATE_KEYS:
            chat = (update.get(key) or {}).get("chat") if isinstance(update.get(key), dict) else None
            if not isinstance(chat, dict) or chat.get("type") not in _CHAT_TYPES:
                continue
            if chat.get("id") is not None and chat["id"] not in chats:
                chats[chat["id"]] = {"id": chat["id"], "type": chat["type"],
                                     "title": chat.get("title")}
    return {"chats": list(chats.values())}


@router.post("/telegram/test")
async def send_test(admin: CurrentAdmin, session: DbSession, redis: RedisClient) -> dict:
    """Sends now with the client (also while paused or disabled) and records the outcome as
    an outbox row (`category="test"`, `sent` or `failed`)."""
    cfg = await load_config(session)
    if cfg.unreadable:
        raise _invalid("bot_token", UNREADABLE_MESSAGE)
    if not cfg.bot_token:
        raise _invalid("bot_token", "configura prima il token del bot")
    if not cfg.chat_id:
        raise _invalid("chat_id", "configura prima l'ID del gruppo")
    error, message_id = None, None
    async with httpx.AsyncClient() as http:
        try:
            message_id = await _client(cfg.bot_token, http).send_message(cfg.chat_id, TEST_TEXT)
        except (TelegramError, SMTError) as e:
            error = e.message
    async with _transaction(session, redis):
        session.add(TelegramOutbox(
            source="system", category="test", level="info", text=TEST_TEXT, attempts=1,
            status=OutboxStatus.failed if error else OutboxStatus.sent,
            last_error=error[:500] if error else None, telegram_message_id=message_id,
            sent_at=None if error else datetime.now(UTC),
        ))
        await _audit(session, "admin:telegram.test", admin.email, ok=error is None)
    return {"ok": True} if error is None else {"ok": False, "error": error}


@router.put("/telegram/paused")
async def put_paused(
    body: PausedIn, admin: CurrentAdmin, session: DbSession, redis: RedisClient
) -> dict:
    """Changes only the `paused` flag of the stored settings (an unreadable token stays as
    it is); on resume the paused rows go back to pending."""
    async with _transaction(session, redis):
        await _lock_settings(session)
        raw = await session.get(AppSetting, SETTINGS_KEY)
        if raw is None or not isinstance(raw.value, dict):
            await save_config(session, TelegramConfig(paused=body.paused))
        else:
            raw.value = {**raw.value, "paused": body.paused}
            await session.flush()
        resumed = 0 if body.paused else await resume_paused(session)
        await _audit(session, "admin:telegram.pause", admin.email, paused=body.paused,
                     resumed=resumed)
    return await _telegram_dto(session)


@router.get("/telegram/outbox")
async def get_outbox(
    session: DbSession,
    status: str | None = None,
    category: str | None = None,
    before_id: int | None = None,
    limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = DEFAULT_LIMIT,
) -> dict:
    stmt = select(TelegramOutbox)
    if status:
        if status not in OutboxStatus.__members__:
            raise _invalid("status", "stato non valido: usa uno tra " + ", ".join(OutboxStatus))
        stmt = stmt.where(TelegramOutbox.status == status)
    if category:
        stmt = stmt.where(TelegramOutbox.category == category)
    if before_id is not None:
        stmt = stmt.where(TelegramOutbox.id < before_id)
    rows = list(await session.scalars(stmt.order_by(TelegramOutbox.id.desc()).limit(limit)))
    return {"items": await _outbox_dtos(session, rows)}


@router.post("/telegram/outbox/{outbox_id}/retry")
async def retry(outbox_id: int, admin: CurrentAdmin, session: DbSession,
                redis: RedisClient) -> dict:
    async with _transaction(session, redis):
        row = await retry_outbox(session, outbox_id)
        await _audit(session, "admin:telegram.retry", admin.email, outbox_id=outbox_id)
    return (await _outbox_dtos(session, [row]))[0]


@router.get("/decisions")
async def get_decisions(
    session: DbSession,
    agent_id: UUID | None = None,
    portfolio_id: UUID | None = None,
    kind: str | None = None,
    before_id: int | None = None,
    limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = DEFAULT_LIMIT,
) -> dict:
    if kind and kind not in DecisionKind.__members__:
        raise _invalid("kind", "tipo non valido: usa uno tra " + ", ".join(DecisionKind))
    rows: list[Decision] = await list_decisions(
        session, viewer_agent_id=None, agent_id=agent_id, portfolio_id=portfolio_id,
        kind=kind or None, before_id=before_id, limit=limit,
    )
    agents = await _agent_names(session, {d.agent_id for d in rows})
    pids = {d.portfolio_id for d in rows if d.portfolio_id}
    portfolios = dict((await session.execute(
        select(Portfolio.id, Portfolio.name).where(Portfolio.id.in_(pids))
    )).all()) if pids else {}
    fields = ("id", "agent_id", "portfolio_id", "kind", "summary", "symbols", "details",
              "created_at")
    return {"items": [
        {**{f: to_jsonable(getattr(d, f)) for f in fields},
         "agent": agents.get(d.agent_id), "portfolio": portfolios.get(d.portfolio_id)}
        for d in rows
    ]}
