"""The live audit trail: one append-only row per step of a real-money link."""

import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from smtrading.admin.common import MASK, is_secret
from smtrading.brokers.live.models import LiveAudit
from smtrading.events import json_safe


def _redact(value: Any, key: str | None = None) -> Any:
    """Secret leaves (admin.common.is_secret keys) become '••••' at any depth; a value that
    is already masked ('••••1234') is kept."""
    if isinstance(value, dict):
        return {k: _redact(v, k) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact(v, key) for v in value]
    if key is not None and is_secret(key) and value not in (None, ""):
        return value if isinstance(value, str) and value.startswith(MASK) else MASK
    return value


async def record_live_audit(
    session: AsyncSession,
    *,
    action: str,
    link_id: uuid.UUID,
    actor: str,
    portfolio_id: uuid.UUID | None = None,
    order_id: uuid.UUID | None = None,
    ip: str | None = None,
    user_agent: str | None = None,
    details: dict | None = None,
) -> LiveAudit:
    row = LiveAudit(
        link_id=link_id, portfolio_id=portfolio_id, order_id=order_id, actor=actor[:128],
        action=action, ip=ip[:64] if ip else None,
        user_agent=user_agent[:300] if user_agent else None,
        details=_redact(json_safe(details or {})),
    )
    session.add(row)
    await session.flush()
    return row
