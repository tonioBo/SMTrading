"""Telegram settings service, shared by the admin API («Notifiche») and the CLI
(`telegram set`): validation, verification with Telegram, save and audit of the
configuration, pause/resume, and the release of the sender's chat-wide hold.

Everything runs in the caller's transaction, which the caller commits. Errors are
`SMTError(INVALID_ORDER)` with `details.field`; the bot token never reaches one.
"""

import re
from typing import Any

import httpx
from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from smtrading.agents.service import write_audit
from smtrading.common.errors import ErrorCode, SMTError
from smtrading.risk.models import AppSetting
from smtrading.telegram.client import TelegramClient, TelegramError
from smtrading.telegram.config import (
    SETTINGS_KEY,
    UNREADABLE_MESSAGE,
    TelegramConfig,
    load_config,
    save_config,
)
from smtrading.telegram.models import OutboxStatus, TelegramOutbox
from smtrading.telegram.outbox import resume_paused

HTTP_TIMEOUT_S = 10.0
_CHAT_ID = re.compile(r"-?\d{1,20}|@[A-Za-z][A-Za-z0-9_]{3,31}")


def invalid(field: str, message: str) -> SMTError:
    return SMTError(ErrorCode.INVALID_ORDER, message, {"field": field})


def clean_token(token: str) -> str:
    """Strip and drop every whitespace character (a pasted token often carries some)."""
    cleaned = "".join(token.split())
    if not cleaned:
        raise invalid("bot_token", "inserisci il token del bot")
    return cleaned


def clean_chat_id(chat_id: str | int) -> str:
    value = str(chat_id).strip()
    if isinstance(chat_id, bool) or not _CHAT_ID.fullmatch(value):
        raise invalid("chat_id", "ID del gruppo non valido (es. -1001234567890)")
    return value


def client_for(token: str, http: httpx.AsyncClient) -> TelegramClient:
    try:
        return TelegramClient(token, http, timeout=HTTP_TIMEOUT_S)
    except TelegramError as e:
        raise invalid("bot_token", e.message) from None


async def lock_settings(session: AsyncSession) -> None:
    """Serialises concurrent writers of the telegram settings row (if it exists)."""
    await session.execute(
        select(AppSetting.key).where(AppSetting.key == SETTINGS_KEY).with_for_update()
    )


async def audit(session: AsyncSession, method: str, by: str, **params: Any) -> None:
    await write_audit(session, agent_id=None, method=method, params={"by": by, **params},
                      outcome="ok", latency_ms=0)


async def release_hold(session: AsyncSession) -> None:
    """The configuration was fixed (new token or group), the queue resumed or the failed
    messages retried: the pending rows are due now and the sender drops its in-memory
    chat-wide hold (it watches `hold_version`) instead of waiting up to 900 s."""
    await session.execute(text(
        "update app_settings set value = jsonb_set(value, '{hold_version}', "
        "to_jsonb(coalesce((value->>'hold_version')::bigint, 0) + 1)) where key = :k"
    ), {"k": SETTINGS_KEY})
    await session.execute(
        update(TelegramOutbox)
        .where(TelegramOutbox.status == OutboxStatus.pending,
               TelegramOutbox.next_attempt_at > func.now())
        .values(next_attempt_at=func.now())
    )


async def apply_config(
    session: AsyncSession, http: httpx.AsyncClient, *, fields: set[str],
    bot_token: str | None = None, chat_id: str | int | None = None,
    enabled: bool | None = None, verify: bool = True, by: str,
) -> TelegramConfig:
    """Validates, verifies with Telegram (getMe with the effective token, getChat with the
    effective chat) when the token or the chat changes, then saves and audits — all in the
    caller's transaction, which must be committed by the caller. Nothing is saved on error.
    While the stored token is unreadable every update must re-enter the token. A new token
    or group releases the sender's hold."""
    for name in fields & {"bot_token", "chat_id", "enabled"}:
        if {"bot_token": bot_token, "chat_id": chat_id, "enabled": enabled}[name] is None:
            raise invalid(name, f"{name} non può essere nullo")
    new_token = clean_token(bot_token) if "bot_token" in fields else None
    new_chat = clean_chat_id(chat_id) if "chat_id" in fields else None

    current = await load_config(session)
    if current.unreadable and new_token is None:
        raise invalid("bot_token", UNREADABLE_MESSAGE)
    token = new_token or current.bot_token
    chat = new_chat or current.chat_id
    token_changed = new_token is not None and new_token != current.bot_token
    chat_changed = new_chat is not None and new_chat != current.chat_id
    if new_chat is not None and token is None:
        raise invalid("bot_token", "inserisci prima il token del bot")

    username, title = current.bot_username, current.chat_title
    if token_changed or chat_changed:
        username = None if token_changed else username
        title = None if (chat_changed or token_changed) else title
        if verify:
            client = client_for(token, http)
            try:
                username = (await client.get_me()).get("username")
            except TelegramError as e:
                raise invalid("bot_token", e.message) from None
            if chat is not None:
                try:
                    info = await client.get_chat(chat)
                except TelegramError as e:
                    raise invalid("chat_id", e.message) from None
                title = info.get("title")
                if info.get("id") is not None:  # an @username resolves to the numeric id
                    chat = str(info["id"])

    await lock_settings(session)
    fresh = await load_config(session)  # pause may have changed during the verification
    if new_chat is None and fresh.chat_id != current.chat_id:
        # the sender migrated the group to a supergroup meanwhile: keep its new id
        chat, title = fresh.chat_id, fresh.chat_title
    cfg = TelegramConfig(
        bot_token=token, chat_id=chat,
        enabled=fresh.enabled if enabled is None else enabled,
        paused=fresh.paused, bot_username=username, chat_title=title,
        hold_version=fresh.hold_version,
    )
    if cfg.enabled and not (cfg.bot_token and cfg.chat_id):
        raise invalid("enabled", "per attivare le notifiche servono il token e il gruppo")
    chat_saved = new_chat is not None and cfg.chat_id != current.chat_id
    changed = [name for name, yes in (
        ("bot_token", token_changed), ("chat_id", chat_saved),
        ("enabled", cfg.enabled != current.enabled),
    ) if yes]
    await save_config(session, cfg)
    if token_changed or chat_saved:
        await release_hold(session)
    await audit(session, "admin:telegram.config", by, changed=changed,
                chat_id=cfg.chat_id, enabled=cfg.enabled)
    return cfg


async def set_paused(session: AsyncSession, paused: bool, *, by: str) -> int:
    """Changes only the `paused` flag of the stored settings (an unreadable token stays as
    it is); on resume the paused rows go back to pending (old non-critical ones are
    skipped, see `resume_paused`) and the sender's hold is released. Returns the rows
    resumed."""
    await lock_settings(session)
    raw = await session.get(AppSetting, SETTINGS_KEY, populate_existing=True)
    if raw is None or not isinstance(raw.value, dict):
        await save_config(session, TelegramConfig(paused=paused))
    else:
        raw.value = {**raw.value, "paused": paused}
        await session.flush()
    resumed = 0
    if not paused:
        resumed = await resume_paused(session)
        await release_hold(session)
    await audit(session, "admin:telegram.pause", by, paused=paused, resumed=resumed)
    return resumed


async def retry_all_failed(session: AsyncSession, *, by: str) -> int:
    """«Riprova tutti i falliti»: every failed message (test messages excepted) goes back
    to pending with its attempts reset, and the sender's hold is released."""
    result = await session.execute(
        update(TelegramOutbox)
        .where(TelegramOutbox.status == OutboxStatus.failed,
               TelegramOutbox.category != "test")
        .values(status=OutboxStatus.pending, attempts=0, last_error=None,
                next_attempt_at=func.now())
    )
    retried = result.rowcount or 0
    await release_hold(session)
    await audit(session, "admin:telegram.retry_failed", by, retried=retried)
    return retried
