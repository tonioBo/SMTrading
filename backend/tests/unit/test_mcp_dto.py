import enum
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from smtrading.common.errors import ErrorCode, SMTError
from smtrading.engine.models import Order, OrderSide, OrderStatus, OrderType, TimeInForce
from smtrading.marketdata.types import Bar, Quote
from smtrading.mcp import dto


class Color(enum.Enum):
    red = "r"


@dataclass
class Thing:
    n: Decimal
    when: datetime
    tags: tuple


def test_to_jsonable_primitives_and_containers():
    uid = uuid.uuid4()
    cet = timezone(timedelta(hours=1))
    out = dto.to_jsonable(
        {
            "d": Decimal("1.50"),
            "u": uid,
            "t": datetime(2026, 1, 2, 13, 0, tzinfo=cet),
            "e": Color.red,
            "l": [Thing(Decimal(2), datetime(2026, 1, 1, tzinfo=UTC), (1, "a"))],
            "none": None,
        }
    )
    assert out == {
        "d": "1.50",
        "u": str(uid),
        "t": "2026-01-02T12:00:00+00:00",
        "e": "r",
        "l": [{"n": "2", "when": "2026-01-01T00:00:00+00:00", "tags": [1, "a"]}],
        "none": None,
    }


def test_order_dto_fixed_fields_and_dispatch():
    o = Order(
        id=uuid.uuid4(), portfolio_id=uuid.uuid4(), symbol="AAPL", side=OrderSide.buy,
        type=OrderType.limit, tif=TimeInForce.day, qty=Decimal(3), limit_price=Decimal("10.5"),
        filled_qty=Decimal(0), status=OrderStatus.new, source="agent", reduce_only=False,
        created_at=datetime(2026, 1, 1, tzinfo=UTC), updated_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    d = dto.order_dto(o)
    assert set(d) == set(dto.ORDER_FIELDS)
    assert d["qty"] == "3" and d["side"] == "buy" and d["limit_price"] == "10.5"
    assert d["notional"] is None
    assert dto.to_jsonable([o]) == [d]


def test_quote_and_bar_dto():
    ts = datetime(2026, 1, 1, tzinfo=UTC)
    q = Quote(symbol="X", bid=Decimal(1), ask=Decimal(2), last=None, ts=ts, provider="fake")
    assert dto.quote_dto(q) == {
        "symbol": "X", "bid": "1", "ask": "2", "last": None,
        "ts": "2026-01-01T00:00:00+00:00", "provider": "fake",
    }
    b = Bar(ts=ts, open=Decimal(1), high=Decimal(2), low=Decimal(0), close=Decimal(1),
            volume=Decimal(9))
    assert dto.bar_dto(b)["volume"] == "9"


@pytest.mark.parametrize(
    ("v", "expected"),
    [(None, None), ("1.25", Decimal("1.25")), (3, Decimal(3)), (0.1, Decimal("0.1"))],
)
def test_parse_decimal_ok(v, expected):
    assert dto.parse_decimal(v, "qty") == expected


@pytest.mark.parametrize("v", ["abc", "", "NaN", "Infinity", True, [1]])
def test_parse_decimal_invalid(v):
    with pytest.raises(SMTError) as e:
        dto.parse_decimal(v, "qty")
    assert e.value.code == ErrorCode.INVALID_ORDER
    assert e.value.details["field"] == "qty"


def test_parse_uuid_and_enum():
    uid = uuid.uuid4()
    assert dto.parse_uuid(str(uid), "portfolio_id") == uid
    assert dto.parse_uuid(None, "portfolio_id") is None
    with pytest.raises(SMTError) as e:
        dto.parse_uuid("nope", "portfolio_id")
    assert e.value.details == {"field": "portfolio_id"}
    assert dto.parse_enum(OrderSide, "buy", "side") is OrderSide.buy
    assert dto.parse_enum(OrderSide, None, "side") is None
    with pytest.raises(SMTError) as e:
        dto.parse_enum(OrderSide, "hold", "side")
    assert e.value.code == ErrorCode.INVALID_ORDER
    assert e.value.details["field"] == "side"
    assert e.value.details["allowed"] == ["buy", "sell"]
