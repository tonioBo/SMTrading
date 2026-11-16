"""Paperclip configuration in `app_settings["paperclip"]` and the reader state in
`app_settings["paperclip_state"]`.

The board key is stored encrypted (`board_key_enc`, `aad=b"paperclip"`), registered with
`register_secret` at every read, and never returned: only `••••` plus its last 4
characters. Its `__repr__` never shows it either."""

import base64
import binascii
import copy
import logging
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from smtrading.admin.common import MASK, _invalid, mask_value
from smtrading.agents.service import write_audit
from smtrading.common.config import get_settings
from smtrading.common.crypto import decrypt_json, encrypt_json
from smtrading.common.errors import SMTError
from smtrading.common.logsetup import register_secret
from smtrading.risk.models import AppSetting

SETTINGS_KEY = "paperclip"
STATE_KEY = "paperclip_state"
AAD = b"paperclip"
KEY_PREFIX = "pcp_board_"
log = logging.getLogger(__name__)

DEFAULT_STATE: dict[str, Any] = {
    "last_ok_at": None,
    "last_error": None,
    "consecutive_errors": 0,
    "error_alerted": False,
    "baseline": None,
    "baseline_at": None,
    "isolation": "unknown",  # ok | changed | unknown | off
    "changes": {"removed": [], "changed": [], "added": []},
    "alerted_signature": None,
    "key_expires_at": None,  # ISO text, None = no expiry (or not known)
    "expiry_alerted": None,  # the key_expires_at value already notified
    # "config": the company or URL changed, the next snapshot is the new baseline (notified)
    "rebaseline_reason": None,
}
# reset when the company or the URL changes: the old baseline describes another company
_CONFIG_RESET = {
    "baseline": None, "baseline_at": None, "isolation": "unknown",
    "changes": {"removed": [], "changed": [], "added": []}, "alerted_signature": None,
    "consecutive_errors": 0, "error_alerted": False, "last_error": None,
    "key_expires_at": None, "expiry_alerted": None, "rebaseline_reason": "config",
}
# reset when the board key changes: the expiry was the old key's
_KEY_RESET = {"key_expires_at": None, "expiry_alerted": None}


@dataclass
class PaperclipConfig:
    base_url: str | None = None
    company_id: str | None = None
    board_key: str | None = None
    enabled: bool = False
    agent_map: dict[str, str] = field(default_factory=dict)  # paperclip id -> smt agent id
    unreadable: bool = False  # a stored key could not be decrypted (e.g. new master key)

    def __repr__(self) -> str:  # the key never reaches a log through a repr
        key = MASK if self.board_key else None
        return (f"PaperclipConfig(base_url={self.base_url!r}, company_id={self.company_id!r}, "
                f"board_key={key!r}, enabled={self.enabled!r}, agent_map={self.agent_map!r}, "
                f"unreadable={self.unreadable!r})")

    @property
    def complete(self) -> bool:
        return bool(self.base_url and self.company_id and self.board_key)


def _str_or_none(v: Any) -> str | None:
    return str(v) if v not in (None, "") else None


async def _raw(session: AsyncSession, key: str) -> Any:
    return await session.scalar(select(AppSetting.value).where(AppSetting.key == key))


def _decrypt_key(enc: Any) -> str | None:
    try:
        blob = base64.b64decode(str(enc), validate=True)
        key = decrypt_json(blob, get_settings().master_key, aad=AAD).get("board_key")
    except (SMTError, binascii.Error, ValueError, TypeError, AttributeError):
        return None
    return key if isinstance(key, str) and key else None


def _encrypt_key(key: str) -> str:
    blob = encrypt_json({"board_key": key}, get_settings().master_key, aad=AAD)
    return base64.b64encode(blob).decode("ascii")


NEW_HOST_NEEDS_KEY = "indirizzo cambiato: reinserisci la chiave board per il nuovo indirizzo"
_DEFAULT_PORTS = {"http": 80, "https": 443}


def _origin(url: str | None) -> tuple[str, str, int] | None:
    """(scheme, host, effective port) the board key is sent to; None without a usable
    URL (no http/https scheme, no host, a port that cannot be parsed). `https://h` and
    `https://h:443` are the same origin; `http://h` is another one (the key in clear)."""
    if not url:
        return None
    try:
        parts = urlsplit(url)
        scheme = parts.scheme.lower()
        port = parts.port or _DEFAULT_PORTS.get(scheme)
    except ValueError:
        return None
    if scheme not in _DEFAULT_PORTS or not parts.hostname or port is None:
        return None
    return (scheme, parts.hostname, port)


async def load_config(session: AsyncSession) -> PaperclipConfig:
    raw = await _raw(session, SETTINGS_KEY)
    if not isinstance(raw, dict):
        return PaperclipConfig()
    key, unreadable = None, False
    if raw.get("board_key_enc"):
        key = _decrypt_key(raw["board_key_enc"])
        unreadable = key is None
        if key:
            register_secret(key)
        else:
            log.warning("paperclip configuration unreadable: re-enter the board key")
    agent_map = raw.get("agent_map") if isinstance(raw.get("agent_map"), dict) else {}
    return PaperclipConfig(
        base_url=_str_or_none(raw.get("base_url")),
        company_id=_str_or_none(raw.get("company_id")),
        board_key=key,
        enabled=bool(raw.get("enabled")),
        agent_map={str(k): str(v) for k, v in agent_map.items()},
        unreadable=unreadable,
    )


async def save_config(session: AsyncSession, *, base_url: str | None, company_id: str | None,
                      board_key: str | None, enabled: bool, agent_map: dict[str, str], by: str
                      ) -> PaperclipConfig:
    """`board_key=None` keeps the stored key, but only for the same origin: a `base_url`
    with another scheme, host or port (or one whose origin cannot be parsed) with a
    stored key and no new one is refused (field `board_key`), so the key never reaches a host it was not entered for. Writes the
    admin audit (never the key).

    In the same transaction: a change of company or URL of an existing configuration
    resets the reader state (the next snapshot is the new baseline, notified once); a new
    key forgets the old key's expiry."""
    raw = await _raw(session, SETTINGS_KEY)
    raw = raw if isinstance(raw, dict) else {}
    new_origin = _origin(base_url)
    # fail closed: a URL whose origin cannot be worked out never keeps the stored key
    if base_url and new_origin is None:
        if board_key is None and raw.get("board_key_enc"):
            raise _invalid("board_key", NEW_HOST_NEEDS_KEY)
        raise _invalid("base_url", "URL non valido")
    if (board_key is None and raw.get("board_key_enc") and new_origin is not None
            and new_origin != _origin(_str_or_none(raw.get("base_url")))):
        raise _invalid("board_key", NEW_HOST_NEEDS_KEY)
    baseline_reset = any(
        _str_or_none(raw.get(k)) is not None and _str_or_none(raw.get(k)) != new
        for k, new in (("company_id", company_id), ("base_url", base_url)))
    reset = ({**_CONFIG_RESET} if baseline_reset else {}) | (
        _KEY_RESET if board_key is not None else {})
    if reset:
        state = await load_state(session, for_update=True)
        state.update(copy.deepcopy(reset))
        await save_state(session, state)
    key_enc = raw.get("board_key_enc")
    if board_key is not None:
        register_secret(board_key)
        key_enc = _encrypt_key(board_key)
    value = {
        "base_url": base_url,
        "company_id": company_id,
        "board_key_enc": key_enc,
        "enabled": bool(enabled),
        "agent_map": dict(agent_map),
    }
    stmt = insert(AppSetting).values(key=SETTINGS_KEY, value=value)
    await session.execute(stmt.on_conflict_do_update(index_elements=["key"], set_={"value": value}))
    await session.flush()
    await write_audit(
        session, agent_id=None, method="admin:paperclip.config",
        params={"by": by, "base_url": base_url, "company_id": company_id,
                "enabled": bool(enabled), "agent_map": dict(agent_map),
                "board_key_changed": board_key is not None,
                "baseline_reset": baseline_reset},
        outcome="ok", latency_ms=0,
    )
    return await load_config(session)


def config_dto(cfg: PaperclipConfig) -> dict[str, Any]:
    return {
        "base_url": cfg.base_url,
        "company_id": cfg.company_id,
        "enabled": cfg.enabled,
        "board_key_masked": mask_value(cfg.board_key) if cfg.board_key else None,
        "agent_map": cfg.agent_map,
        "error": "configurazione illeggibile: reinserisci la chiave board"
                 if cfg.unreadable else None,
    }


async def load_state(session: AsyncSession, *, for_update: bool = False) -> dict[str, Any]:
    """The reader state with defaults. `for_update` creates the row if missing and locks
    it (serialises the reader and the admin «Riconferma»)."""
    if for_update:
        await session.execute(insert(AppSetting).values(key=STATE_KEY, value=DEFAULT_STATE)
                              .on_conflict_do_nothing(index_elements=["key"]))
        raw = await session.scalar(select(AppSetting.value).where(AppSetting.key == STATE_KEY)
                                   .with_for_update())
    else:
        raw = await _raw(session, STATE_KEY)
    state = copy.deepcopy(DEFAULT_STATE)
    if isinstance(raw, dict):
        state.update({k: v for k, v in raw.items() if k in DEFAULT_STATE})
    return state


async def save_state(session: AsyncSession, state: dict[str, Any]) -> None:
    value = {k: state.get(k, v) for k, v in DEFAULT_STATE.items()}
    stmt = insert(AppSetting).values(key=STATE_KEY, value=value)
    await session.execute(stmt.on_conflict_do_update(index_elements=["key"], set_={"value": value}))
    await session.flush()
