"""Domain events (Redis pub/sub channel `events`) and persisted notifications."""

import json
from datetime import UTC, date, datetime
from decimal import Decimal
from enum import Enum
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from smtrading.risk.models import Notification

EVENTS_CHANNEL = "events"


def json_safe(obj: Any) -> Any:
    """Decimal -> str, UUID -> str, datetime/date -> ISO 8601 (datetimes in UTC)."""
    if isinstance(obj, Enum):
        return obj.value
    if isinstance(obj, (Decimal, UUID)):
        return str(obj)
    if isinstance(obj, datetime):
        return obj.astimezone(UTC).isoformat() if obj.tzinfo else obj.isoformat()
    if isinstance(obj, date):
        return obj.isoformat()
    if isinstance(obj, dict):
        return {str(k): json_safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set, frozenset)):
        return [json_safe(v) for v in obj]
    return obj


async def publish_event(redis, type: str, payload: dict) -> None:
    if redis is None:
        return
    message = {"type": type, "ts": datetime.now(UTC), **payload}
    await redis.publish(EVENTS_CHANNEL, json.dumps(json_safe(message)))


async def notify(
    session: AsyncSession,
    *,
    portfolio_id: UUID | None,
    kind: str,
    payload: dict,
    agent_id: str | None = None,
) -> Notification:
    n = Notification(
        portfolio_id=portfolio_id, agent_id=agent_id, kind=kind, payload=json_safe(payload)
    )
    session.add(n)
    await session.flush()
    return n
