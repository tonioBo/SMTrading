"""The single source of truth for a real-money link's keys and account identity.

- `LIVE_AAD`: the encryption context of every live config (`aad=b"broker_live"`).
- `decrypt_live_config` registers the keys with `register_secret` at every decrypt
  (masked in every log text of the process); `register_live_secrets` does it for every
  live link at process start (API and worker).
- `account_fingerprint`: sha256 of "kind:account_id", the identity of the account
  behind the keys.
- `snapshot_record`: what is stored of the account (`BrokerLink.last_account_snapshot`):
  never the broker's `raw` answer, never the full account ID (`account_masked`).

Used by the activation service, BrokerSync and the live reconciliation alike.
"""

import base64
import hashlib
import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from smtrading.admin.common import MASK, mask_value
from smtrading.brokers.base import AccountSnapshot
from smtrading.brokers.live import LIVE_KINDS
from smtrading.brokers.models import BrokerLink
from smtrading.common.config import get_settings
from smtrading.common.crypto import decrypt_json, encrypt_json
from smtrading.common.errors import SMTError
from smtrading.common.logsetup import register_secret
from smtrading.events import json_safe

log = logging.getLogger(__name__)

LIVE_AAD = b"broker_live"

# per kind: (key ID field, secret field, other allowed fields)
LIVE_FIELDS: dict[str, tuple[str, str, frozenset[str]]] = {
    "alpaca_live": ("key_id", "secret_key", frozenset({"crypto_enabled"})),
    "t212_live": ("api_key", "api_secret", frozenset({"order_types"})),
}


def live_secrets(kind: str, config: dict) -> list[str]:
    """The texts to mask for a live config: both keys and, for Trading 212, the Base64
    Basic credential the adapter sends (longest first)."""
    key_field, secret_field, _ = LIVE_FIELDS.get(kind, ("", "", frozenset()))
    found = [str(v) for f in (key_field, secret_field)
             if isinstance(v := config.get(f), str) and v]
    if kind == "t212_live" and config.get("api_key"):
        cred = f"{config.get('api_key', '')}:{config.get('api_secret', '')}"
        found.append(base64.b64encode(cred.encode()).decode())
    return sorted(set(found), key=len, reverse=True)


def register_config_secrets(kind: str, config: dict) -> None:
    for value in live_secrets(kind, config):
        register_secret(value)


def encrypt_live_config(config: dict) -> bytes:
    return encrypt_json(config, get_settings().master_key, aad=LIVE_AAD)


def decrypt_live_config(link: BrokerLink) -> dict:
    """The decrypted config of a live link; its keys are registered for log masking."""
    if link.config_encrypted is None:
        return {}
    config = decrypt_json(link.config_encrypted, get_settings().master_key, aad=LIVE_AAD)
    register_config_secrets(link.kind, config)
    return config


async def register_live_secrets(session: AsyncSession) -> int:
    """At process start: registers the keys of every live link (log masking). Returns how
    many links; a config that cannot be read is skipped (logged without details)."""
    count = 0
    for link in await session.scalars(select(BrokerLink).where(BrokerLink.kind.in_(LIVE_KINDS))):
        try:
            decrypt_live_config(link)
            count += 1
        except SMTError:
            log.warning("live link %s: keys cannot be decrypted", link.id)
    return count


def key_masked(kind: str, config: dict) -> str:
    """'••••' + the last 4 characters of the key ID."""
    key_field = LIVE_FIELDS[kind][0]
    value = config.get(key_field)
    return mask_value(value) if value else MASK


def account_masked(account_id: str | None) -> str | None:
    return mask_value(account_id) if account_id else None


def account_fingerprint(kind: str, account_id: str) -> str:
    return hashlib.sha256(f"{kind}:{account_id}".encode()).hexdigest()


def snapshot_record(snap: AccountSnapshot) -> dict[str, Any]:
    """The stored `BrokerLink.last_account_snapshot` (never `raw`, never the full ID)."""
    return json_safe({
        "account_masked": account_masked(snap.account_id),
        "account_type": snap.account_type,
        "currency": snap.currency,
        "cash_available": snap.cash_available,
        "positions_count": snap.positions_count,
        "open_orders_count": snap.open_orders_count,
        "crypto_enabled": snap.crypto_enabled,
        "order_types": sorted(str(t) for t in snap.order_types)
        if snap.order_types is not None else None,
    })
