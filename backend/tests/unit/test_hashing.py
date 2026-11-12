from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from uuid import UUID

import pytest

from smtrading.ledger.hashing import GENESIS_HASH, canonical_payload, compute_hash

J = UUID("00000000-0000-0000-0000-0000000000a0")
A = UUID("00000000-0000-0000-0000-0000000000a1")
B = UUID("00000000-0000-0000-0000-0000000000b2")
TS = datetime(2026, 1, 1, tzinfo=UTC)
BASE = {
    "journal_id": J, "type": "DEPOSIT", "created_at": TS, "author_type": "admin", "author_id": "1",
    "reason": None, "order_id": None, "fill_id": None, "reversed_journal_id": None,
}
LINES = [(A, Decimal(10)), (B, Decimal(-10))]


def test_payload_independent_of_line_order_and_trailing_zeros():
    p1 = canonical_payload(**BASE, lines=[(A, Decimal("10.0")), (B, Decimal(-10))])
    p2 = canonical_payload(**BASE, lines=[(B, Decimal("-10.000")), (A, Decimal(10))])
    assert p1 == p2


def test_hash_changes_with_prev_and_content():
    p = canonical_payload(**BASE, lines=LINES)
    assert compute_hash(GENESIS_HASH, p) != compute_hash("1" * 64, p)
    assert len(compute_hash(GENESIS_HASH, p)) == 64


def test_high_precision_quantity_preserved():
    p = canonical_payload(
        **BASE, lines=[(A, Decimal("0.123456789012345678")), (B, Decimal("-0.123456789012345678"))]
    )
    assert b"0.123456789012345678" in p


def test_differs_in_38th_significant_digit():
    q1 = Decimal("12345678901234567890.123456789012345678")
    q2 = Decimal("12345678901234567890.123456789012345679")
    p1 = canonical_payload(**BASE, lines=[(A, q1), (B, -q1)])
    p2 = canonical_payload(**BASE, lines=[(A, q2), (B, -q2)])
    assert p1 != p2


def test_created_at_utc_with_microseconds_and_offsets_normalized():
    p = canonical_payload(**BASE, lines=LINES)
    assert b'"created_at":"2026-01-01T00:00:00.000000+00:00"' in p
    rome = timezone(timedelta(hours=1))
    local = datetime(2026, 1, 1, 1, tzinfo=rome)
    assert canonical_payload(**{**BASE, "created_at": local}, lines=LINES) == p


def test_naive_created_at_rejected():
    with pytest.raises(ValueError):
        canonical_payload(**{**BASE, "created_at": datetime(2026, 1, 1)}, lines=LINES)


def test_compact_sorted_json():
    p = canonical_payload(**BASE, lines=LINES)
    assert b" " not in p
    assert p.startswith(b'{"author_id":"1"')
