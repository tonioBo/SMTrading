import hashlib
import json
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

from smtrading.common.money import decimal_to_str

GENESIS_HASH = "0" * 64


def _opt(value: UUID | None) -> str | None:
    return None if value is None else str(value)


def canonical_payload(
    *,
    journal_id: UUID,
    type: str,
    created_at: datetime,
    author_type: str,
    author_id: str | None,
    reason: str | None,
    order_id: UUID | None,
    fill_id: UUID | None,
    reversed_journal_id: UUID | None,
    lines: list[tuple[UUID, Decimal]],
) -> bytes:
    if created_at.tzinfo is None or created_at.utcoffset() is None:
        raise ValueError("created_at must be timezone-aware")
    ts = created_at.astimezone(UTC).isoformat(timespec="microseconds")
    norm_lines = sorted((str(acc), decimal_to_str(qty)) for acc, qty in lines)
    doc = {
        "journal_id": str(journal_id),
        "type": type,
        "created_at": ts,
        "author_type": author_type,
        "author_id": author_id,
        "reason": reason,
        "order_id": _opt(order_id),
        "fill_id": _opt(fill_id),
        "reversed_journal_id": _opt(reversed_journal_id),
        "lines": [{"account_id": a, "quantity": q} for a, q in norm_lines],
    }
    return json.dumps(doc, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def compute_hash(prev_hash: str, payload: bytes) -> str:
    return hashlib.sha256(prev_hash.encode() + payload).hexdigest()
