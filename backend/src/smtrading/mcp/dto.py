"""JSON shapes of MCP results and parsing of MCP arguments.

Wire rules: Decimal -> plain-notation str (no exponent, trailing zeros stripped, "0" for
zero: `common.money.decimal_to_str`), datetime -> ISO 8601 UTC, date -> ISO, UUID -> str,
Enum -> its value. Each `*_dto` returns a fixed field set (the `*_FIELDS` tuples);
`to_jsonable` dispatches known ORM rows / dataclasses to them and converts the rest.
"""

import dataclasses
import uuid
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import Any

from smtrading.common.errors import ErrorCode, SMTError
from smtrading.common.money import decimal_to_str
from smtrading.engine.models import Fill, Order
from smtrading.instruments.models import Instrument
from smtrading.ledger.models import Journal, JournalLine
from smtrading.marketdata.types import Bar, Quote
from smtrading.positions.models import Position
from smtrading.risk.models import Notification
from smtrading.valuation.service import PortfolioValuation, PositionValuation

ORDER_FIELDS = (
    "id", "portfolio_id", "agent_id", "client_order_id", "symbol", "side", "type", "tif",
    "qty", "notional", "limit_price", "stop_price", "trail_pct", "trail_amount",
    "trail_ref_price", "filled_qty", "avg_fill_price", "status", "parent_id", "oco_group_id",
    "role", "reduce_only", "expires_at", "reject_code", "reject_message", "source",
    "created_at", "updated_at",
)
FILL_FIELDS = (
    "id", "order_id", "portfolio_id", "symbol", "side", "qty", "price", "fee_amount",
    "fee_currency", "quote_bid", "quote_ask", "quote_last", "quote_ts", "source",
    "journal_id", "created_at",
)
POSITION_FIELDS = ("symbol", "qty", "avg_price", "realized_pnl", "opened_at", "updated_at")
POSITION_VALUATION_FIELDS = (
    "symbol", "qty", "avg_price", "mark", "currency", "market_value", "market_value_eur",
    "unrealized_pnl", "unrealized_pnl_eur", "stale",
)
# display (*_eur, cents) values only; *_raw stay server-side
VALUATION_FIELDS = (
    "portfolio_id", "base_currency", "cash", "collateral", "margin_loan", "equity_eur",
    "cash_eur", "exposure_eur", "initial_req_eur", "maint_req_eur", "warn_req_eur",
    "buying_power_eur", "stale_symbols", "ts",
)
JOURNAL_FIELDS = (
    "id", "seq", "type", "order_id", "fill_id", "reversed_journal_id", "author_type",
    "author_id", "reason", "created_at",
)
JOURNAL_LINE_FIELDS = ("account_id", "quantity", "unit_price", "price_ccy")
QUOTE_FIELDS = ("symbol", "bid", "ask", "last", "ts", "provider")
BAR_FIELDS = ("ts", "open", "high", "low", "close", "volume")
INSTRUMENT_FIELDS = (
    "symbol", "name", "asset_class", "market", "currency", "lot_size", "tick_size",
    "shortable", "active",
)
NOTIFICATION_FIELDS = ("id", "portfolio_id", "kind", "payload", "created_at")


def _pick(obj: Any, fields: tuple[str, ...]) -> dict[str, Any]:
    return {f: to_jsonable(getattr(obj, f)) for f in fields}


def order_dto(o: Order) -> dict[str, Any]:
    return _pick(o, ORDER_FIELDS)


def fill_dto(f: Fill) -> dict[str, Any]:
    return _pick(f, FILL_FIELDS)


def position_dto(p: Position) -> dict[str, Any]:
    return _pick(p, POSITION_FIELDS)


def position_valuation_dto(p: PositionValuation) -> dict[str, Any]:
    return _pick(p, POSITION_VALUATION_FIELDS)


def valuation_dto(v: PortfolioValuation) -> dict[str, Any]:
    """VALUATION_FIELDS plus `margin_level` and `positions` (position_valuation_dto)."""
    out = _pick(v, VALUATION_FIELDS)
    out["margin_level"] = v.margin_level()
    out["positions"] = [position_valuation_dto(p) for p in v.positions]
    return out


def journal_dto(j: Journal, lines: list[JournalLine] | None = None) -> dict[str, Any]:
    """JOURNAL_FIELDS; with `lines`, also `lines` as JOURNAL_LINE_FIELDS dicts."""
    out = _pick(j, JOURNAL_FIELDS)
    if lines is not None:
        out["lines"] = [_pick(line, JOURNAL_LINE_FIELDS) for line in lines]
    return out


def quote_dto(q: Quote) -> dict[str, Any]:
    return _pick(q, QUOTE_FIELDS)


def bar_dto(b: Bar) -> dict[str, Any]:
    return _pick(b, BAR_FIELDS)


def instrument_dto(i: Instrument) -> dict[str, Any]:
    return _pick(i, INSTRUMENT_FIELDS)


def notification_dto(n: Notification) -> dict[str, Any]:
    return _pick(n, NOTIFICATION_FIELDS)


_CONVERTERS: dict[type, Any] = {
    Order: order_dto,
    Fill: fill_dto,
    Position: position_dto,
    PositionValuation: position_valuation_dto,
    PortfolioValuation: valuation_dto,
    Journal: journal_dto,
    Quote: quote_dto,
    Bar: bar_dto,
    Instrument: instrument_dto,
    Notification: notification_dto,
}


def to_jsonable(obj: Any) -> Any:
    if obj is None or isinstance(obj, bool | int | float | str):
        return obj
    conv = _CONVERTERS.get(type(obj))
    if conv is not None:
        return conv(obj)
    if isinstance(obj, Enum):
        return obj.value
    if isinstance(obj, Decimal):
        return decimal_to_str(obj) if obj.is_finite() else str(obj)
    if isinstance(obj, uuid.UUID):
        return str(obj)
    if isinstance(obj, datetime):
        return obj.astimezone(UTC).isoformat() if obj.tzinfo else obj.isoformat()
    if isinstance(obj, date):
        return obj.isoformat()
    if isinstance(obj, dict):
        return {str(k): to_jsonable(v) for k, v in obj.items()}
    if isinstance(obj, list | tuple | set | frozenset):
        return [to_jsonable(v) for v in obj]
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return {f.name: to_jsonable(getattr(obj, f.name)) for f in dataclasses.fields(obj)}
    raise TypeError(f"not JSON-serialisable: {type(obj).__name__}")


def _invalid(field: str, message: str, **extra: Any) -> SMTError:
    return SMTError(ErrorCode.INVALID_ORDER, message, {"field": field, **extra})


def parse_decimal(v: str | float | None, field: str) -> Decimal | None:
    """None -> None; floats go through str() (0.1 -> Decimal('0.1')); finite only."""
    if v is None:
        return None
    if isinstance(v, bool) or not isinstance(v, str | int | float):
        raise _invalid(field, f"{field} must be a decimal number")
    try:
        d = Decimal(str(v).strip()) if not isinstance(v, int) else Decimal(v)
    except InvalidOperation:
        raise _invalid(field, f"{field} must be a decimal number") from None
    if not d.is_finite():
        raise _invalid(field, f"{field} must be finite")
    return d


def parse_uuid(v: str | None, field: str) -> uuid.UUID | None:
    if v is None:
        return None
    try:
        return uuid.UUID(str(v))
    except ValueError:
        raise _invalid(field, f"{field} must be a UUID") from None


def parse_enum[E: Enum](cls: type[E], v: str | None, field: str) -> E | None:
    if v is None:
        return None
    try:
        return cls(v)
    except ValueError:
        allowed = [m.value for m in cls]
        raise _invalid(field, f"{field} must be one of {allowed}", allowed=allowed) from None


def parse_ts(v: str | None, field: str) -> datetime | None:
    """ISO 8601; a naive timestamp is taken as UTC."""
    if v is None:
        return None
    try:
        ts = datetime.fromisoformat(str(v))
    except ValueError:
        raise _invalid(field, f"{field} must be an ISO 8601 timestamp") from None
    return ts if ts.tzinfo else ts.replace(tzinfo=UTC)


def parse_int(v: Any, field: str, lo: int, hi: int | None = None) -> int | None:
    """None -> None; an integral number (or numeric string) within [lo, hi]."""
    d = parse_decimal(v, field)
    if d is None:
        return None
    if d != d.to_integral_value() or d < lo or (hi is not None and d > hi):
        bound = f"between {lo} and {hi}" if hi is not None else f">= {lo}"
        raise _invalid(field, f"{field} must be an integer {bound}")
    return int(d)
