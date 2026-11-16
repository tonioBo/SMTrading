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
        "d": "1.5",
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


@pytest.mark.parametrize("source", ["agent", "admin"])
def test_order_dto_is_truthful_agent_sources_remaps(source):
    """order_dto keeps the truth (the admin API reuses it: the portal «Origine» column);
    only the agent-facing MCP output reads a manual order as an agent's (plan 10, M2;
    review I2)."""
    o = Order(
        id=uuid.uuid4(), portfolio_id=uuid.uuid4(), symbol="AAPL", side=OrderSide.buy,
        type=OrderType.market, tif=TimeInForce.day, qty=Decimal(1), filled_qty=Decimal(0),
        status=OrderStatus.new, source=source, reduce_only=False,
        created_at=datetime(2026, 1, 1, tzinfo=UTC), updated_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    assert dto.order_dto(o)["source"] == source
    assert dto.to_jsonable(o)["source"] == source
    seen = dto.agent_sources({"orders": [dto.order_dto(o)], "order": dto.order_dto(o)})
    assert seen["orders"][0]["source"] == seen["order"]["source"] == "agent"


def test_order_dto_keeps_system_orders():
    o = Order(id=uuid.uuid4(), symbol="AAPL", source="system")
    assert dto.order_dto(o)["source"] == "system"
    assert dto.agent_sources(dto.order_dto(o))["source"] == "system"


def test_agent_sources_reads_admin_actors_as_the_system():
    """Plan 11: a portal admin's cancel, kill switch, halt or deposit reads to agents as
    the system's; agents and the system keep their names, free texts are untouched."""
    seen = dto.agent_sources({
        "items": [
            {"type": "order.canceled", "data": {"by": "admin", "symbol": "AAPL"}},
            {"type": "halt.set", "data": {"by": "admin", "reason": "admin"}},
            {"type": "order.canceled", "data": {"by": "agent"}},
            {"type": "order.canceled", "data": {"by": "system"}},
        ],
        "halt": {"set_by": "admin:boss@example.com", "cleared_by": "Orion"},
        "author_type": "admin", "author_id": "admin",
    })
    data = [i["data"] for i in seen["items"]]
    assert [d["by"] for d in data] == ["system", "system", "agent", "system"]
    assert data[1]["reason"] == "admin"  # a free text is not an actor
    assert seen["halt"] == {"set_by": "system", "cleared_by": "Orion"}
    assert (seen["author_type"], seen["author_id"]) == ("system", "system")


def test_agent_sources_reads_admin_watch_changes_neutrally():
    seen = dto.agent_sources({"alerts": [{"kind": "admin.watch_changed"}], "items": [
        {"type": "admin.watch_changed", "summary": "Notifica: admin.watch_changed"}]})
    assert seen == {"alerts": [{"kind": "watch_changed"}], "items": [
        {"type": "watch_changed", "summary": "Notifica: watch_changed"}]}


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


def test_decimal_wire_format_is_plain_without_trailing_zeros():
    o = Order(
        id=uuid.uuid4(), portfolio_id=uuid.uuid4(), symbol="BTC-USD", side=OrderSide.buy,
        type=OrderType.limit, tif=TimeInForce.gtc, qty=Decimal("5.000000000000000000"),
        limit_price=Decimal("1E-7"), stop_price=Decimal("1.2300E+4"),
        filled_qty=Decimal("0E-18"), status=OrderStatus.pending, source="agent",
        reduce_only=False,
    )
    d = dto.order_dto(o)
    assert d["filled_qty"] == "0"
    assert d["qty"] == "5"
    assert d["limit_price"] == "0.0000001"
    assert d["stop_price"] == "12300"
    assert dto.to_jsonable(Decimal("-0.50")) == "-0.5"


def test_parse_ts_and_int():
    assert dto.parse_ts(None, "since") is None
    assert dto.parse_ts("2026-01-01T00:00:00", "since") == datetime(2026, 1, 1, tzinfo=UTC)
    with pytest.raises(SMTError) as e:
        dto.parse_ts("yesterday", "since")
    assert e.value.details == {"field": "since"}
    assert dto.parse_int(None, "limit", 1, 10) is None
    assert dto.parse_int("7", "limit", 1, 10) == 7
    assert dto.parse_int(3.0, "limit", 1) == 3
    for bad in (0, 11, 1.5, "x"):
        with pytest.raises(SMTError) as e:
            dto.parse_int(bad, "limit", 1, 10)
        assert e.value.details["field"] == "limit"
