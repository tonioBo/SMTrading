"""JSON shapes of MCP results and parsing of MCP arguments.

Wire rules: Decimal -> plain-notation str (no exponent, trailing zeros stripped, "0" for
zero: `common.money.decimal_to_str`), datetime -> ISO 8601 UTC, date -> ISO, UUID -> str,
Enum -> its value. Each `*_dto` returns a fixed field set (the `*_FIELDS` tuples);
`to_jsonable` dispatches known ORM rows / dataclasses to them and converts the rest.
"""

import dataclasses
import functools
import json
import uuid
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import Any

from smtrading.brokers.base import BROKER_REJECTED_MESSAGE
from smtrading.brokers.live.protect import order_summary
from smtrading.common.errors import ErrorCode, SMTError
from smtrading.common.money import decimal_to_str
from smtrading.engine.models import Fill, Order
from smtrading.instruments.models import Instrument
from smtrading.ledger.models import Journal, JournalLine
from smtrading.marketdata.types import Bar, Quote
from smtrading.positions.models import Position
from smtrading.risk.models import Notification
from smtrading.valuation.service import PortfolioValuation, PositionValuation
from smtrading.watch.models import Alert

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
DECISION_FIELDS = (
    "id", "agent", "agent_id", "portfolio", "portfolio_id", "kind", "summary", "symbols",
    "details", "outcome", "reason", "subject", "created_at",
)
ALERT_FIELDS = (
    "id", "symbol", "condition", "value", "ref_price", "status", "note", "created_at",
    "triggered_at", "triggered_price",
)


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


def alert_dto(a: Alert) -> dict[str, Any]:
    return _pick(a, ALERT_FIELDS)


def decision_dto(d: Any, agent: str | None, portfolio: str | None) -> dict[str, Any]:
    """A `Decision` row with the names of its agent and portfolio (DECISION_FIELDS)."""
    names = {"agent": agent, "portfolio": portfolio}
    return {f: to_jsonable(names[f] if f in names else getattr(d, f)) for f in DECISION_FIELDS}


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
    Alert: alert_dto,
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


# --- agent-safe results (R8.11, amended) -----------------------------------------------
# With mcp_hide_mode on, agents must not learn that a real-money portfolio is in a dry
# run: an order rejected by it reaches them exactly like an order the broker refused
# without a reason (BrokerSync._placed: PROVIDER_UNAVAILABLE «rejected by the broker»,
# a `live.order.rejected` activity); humans, in the portal and on Telegram, keep
# LIVE_DRY_RUN.
AGENT_REJECT_CODE = ErrorCode.PROVIDER_UNAVAILABLE
AGENT_REJECT_MESSAGE = BROKER_REJECTED_MESSAGE
_DRY_CODE = str(ErrorCode.LIVE_DRY_RUN)
_DRY_TYPE = "live.order.dry_run"
_DRY_MARKERS = (_DRY_CODE, "dry_run", "prova a secco", "(prova)")
_DRY_TEXTS = (
    ("prova a secco: ordine non inviato", AGENT_REJECT_MESSAGE),
    ("Ordine non inviato (prova a secco)", AGENT_REJECT_MESSAGE),
    ("prova a secco", AGENT_REJECT_MESSAGE),
)


def mentions_dry_run(result: Any) -> bool:
    """Cheap pre-check: does a JSON-safe result mention the dry run at all?"""
    text = json.dumps(result, ensure_ascii=False, default=str)
    return any(m in text for m in _DRY_MARKERS)


def _agent_text(text: str) -> str:
    for old, new in _DRY_TEXTS:
        text = text.replace(old, new)
    return text


def agent_safe(value: Any) -> Any:
    """A JSON-safe result with every trace of a dry run replaced by what a real broker
    refusal looks like: reject codes LIVE_DRY_RUN -> PROVIDER_UNAVAILABLE «rejected by the
    broker», `live.order.dry_run` activity items -> `live.order.rejected` (level warning,
    summary rebuilt as for a rejection, request payload dropped), the dry-run texts in any
    string."""
    if isinstance(value, list):
        return [agent_safe(v) for v in value]
    if isinstance(value, str):
        return _agent_text(value)
    if not isinstance(value, dict):
        return value
    dry_item = value.get("type") == _DRY_TYPE
    dry = dry_item or value.get("phase") == "dry_run" or _DRY_CODE in (
        value.get("code"), value.get("reject_code"))
    out: dict[str, Any] = {}
    for k, v in value.items():
        if dry and k in ("payload", "dry_run"):
            continue
        out[k] = agent_safe(v)
    if not dry:
        return out
    for code_key, message_key in (("code", "message"), ("reject_code", "reject_message")):
        if out.get(code_key) == _DRY_CODE:
            out[code_key] = str(AGENT_REJECT_CODE)
            out[message_key] = AGENT_REJECT_MESSAGE
    if out.get("phase") == "dry_run":
        out["phase"] = "rejected"
        if "message" in out:
            out["message"] = AGENT_REJECT_MESSAGE
    if dry_item:
        out["type"] = "live.order.rejected"
        if "level" in out:
            out["level"] = "warning"
        data = out.get("data") if isinstance(out.get("data"), dict) else {}
        out["summary"] = order_summary(data, "rejected")
    return out


# --- no simulation fingerprints (mcp_hide_mode) ------------------------------------
# Agents must believe every portfolio is real. Under hide mode every result and error
# goes through `agent_view`: a fill's provenance (`source`: "sim" for the simulator,
# "broker" for a broker fill) is dropped wherever it appears (fills of get_order /
# list_orders, order.filled / order.partially_filled activity data), so simulated and
# real fills look the same; paper/demo broker hosts and kinds in any text (e.g. a
# transport error kept as the reason of an unknown outcome) read as the live ones; the
# dry-run masking of `agent_safe` follows. Humans (portal, Telegram) keep everything.
_FILL_SOURCES = frozenset({"sim", "broker"})


@functools.cache
def _broker_texts() -> tuple[tuple[str, str], ...]:
    """(paper/demo text, live text): the hosts come from the adapters' BASE_URL (only
    brokers/live spells a live host), loaded on first use (no import cycle)."""
    from smtrading.brokers.alpaca_paper import AlpacaPaperAdapter
    from smtrading.brokers.live import hostname
    from smtrading.brokers.live.alpaca_live import AlpacaLiveAdapter
    from smtrading.brokers.live.t212_live import T212LiveAdapter
    from smtrading.brokers.t212_demo import T212DemoAdapter

    pairs = ((AlpacaPaperAdapter, AlpacaLiveAdapter), (T212DemoAdapter, T212LiveAdapter))
    hosts = tuple((hostname(p.BASE_URL), hostname(live.BASE_URL)) for p, live in pairs)
    kinds = tuple((p.kind, live.kind) for p, live in pairs)
    return tuple((a, b) for a, b in hosts + kinds if a and b)


def _scrub(value: Any) -> Any:
    if isinstance(value, str):
        for old, new in _broker_texts():
            if old in value:
                value = value.replace(old, new)
        return value
    if isinstance(value, list):
        return [_scrub(v) for v in value]
    if isinstance(value, dict):
        return {k: _scrub(v) for k, v in value.items()
                if not (k == "source" and v in _FILL_SOURCES)}
    return value


def agent_view(value: Any) -> Any:
    """A JSON-safe result as agents see it under mcp_hide_mode: no fill provenance, no
    paper/demo broker names, dry runs masked as real broker refusals (`agent_safe`)."""
    value = _scrub(value)
    return agent_safe(value) if mentions_dry_run(value) else value


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
