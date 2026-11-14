"""Telegram configuration in `app_settings["telegram"]`; the bot token is stored encrypted
(`common.crypto`, SMT_MASTER_KEY) and never returned, not even its last characters."""

import base64
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from smtrading.admin.common import MASK
from smtrading.common.config import get_settings
from smtrading.common.crypto import decrypt_json, encrypt_json
from smtrading.risk.models import AppSetting

SETTINGS_KEY = "telegram"


@dataclass
class TelegramConfig:
    bot_token: str | None = None
    chat_id: str | None = None
    enabled: bool = False
    paused: bool = False
    bot_username: str | None = None
    chat_title: str | None = None

    def __repr__(self) -> str:  # the token never reaches a log through a repr
        token = MASK if self.bot_token else None
        return (f"TelegramConfig(bot_token={token!r}, chat_id={self.chat_id!r}, "
                f"enabled={self.enabled!r}, paused={self.paused!r}, "
                f"bot_username={self.bot_username!r}, chat_title={self.chat_title!r})")


def _str_or_none(v: Any) -> str | None:
    return str(v) if v not in (None, "") else None


async def load_config(session: AsyncSession) -> TelegramConfig:
    raw = await session.scalar(select(AppSetting.value).where(AppSetting.key == SETTINGS_KEY))
    if not isinstance(raw, dict):
        return TelegramConfig()
    token = None
    if raw.get("token_enc"):
        blob = base64.b64decode(raw["token_enc"])
        token = decrypt_json(blob, get_settings().master_key).get("bot_token")
    return TelegramConfig(
        bot_token=token or None,
        chat_id=_str_or_none(raw.get("chat_id")),
        enabled=bool(raw.get("enabled")),
        paused=bool(raw.get("paused")),
        bot_username=_str_or_none(raw.get("bot_username")),
        chat_title=_str_or_none(raw.get("chat_title")),
    )


async def save_config(session: AsyncSession, cfg: TelegramConfig) -> None:
    token_enc = None
    if cfg.bot_token:
        blob = encrypt_json({"bot_token": cfg.bot_token}, get_settings().master_key)
        token_enc = base64.b64encode(blob).decode("ascii")
    value = {
        "token_enc": token_enc,
        "chat_id": cfg.chat_id,
        "enabled": cfg.enabled,
        "paused": cfg.paused,
        "bot_username": cfg.bot_username,
        "chat_title": cfg.chat_title,
    }
    stmt = insert(AppSetting).values(key=SETTINGS_KEY, value=value)
    await session.execute(stmt.on_conflict_do_update(index_elements=["key"], set_={"value": value}))


def config_dto(cfg: TelegramConfig) -> dict[str, Any]:
    return {
        "configured": bool(cfg.bot_token and cfg.chat_id),
        "enabled": cfg.enabled,
        "paused": cfg.paused,
        "bot_token_masked": MASK if cfg.bot_token else None,
        "bot_username": cfg.bot_username,
        "chat_id": cfg.chat_id,
        "chat_title": cfg.chat_title,
    }


def is_ready(cfg: TelegramConfig) -> bool:
    return bool(cfg.bot_token and cfg.chat_id and cfg.enabled)
