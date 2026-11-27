"""The broker's own fill time of an automatic order (mode B), kept for the Tracker.

BrokerSync applies a broker fill in its own transaction and keeps no copy of the broker's
answer, so the fill time the broker reports (`raw["filled_at"]`, Alpaca) is written here,
in that same transaction (no network I/O): on the trade of an automatic entry
(`auto_trades.filled_at`, `fill_ts_source = "broker"`) and on the trade an automatic exit
closes (`exit_filled_at`: the rule's own exit `smt-autox-{trade}` or a bracket child of
its entry). The first time is kept. Orders that are neither (every order of the
campaign) are never looked at: `is_automatic` is false for them."""
import re
from datetime import datetime

from sqlalchemy import or_, update
from sqlalchemy.ext.asyncio import AsyncSession

from smtrading.autorules.tables import OPEN, SENT, AutoTrade
from smtrading.engine.models import Order

AUTO_EXIT_PREFIX = "smt-autox-"
_FRACTION = re.compile(r"(\.\d{6})\d+")


def auto_exit_client_id(trade_id: int, attempt: int = 0) -> str:
    """`smt-autox-{trade}`, then `smt-autox-{trade}-{n}` for the n-th replacement."""
    base = f"{AUTO_EXIT_PREFIX}{trade_id}"
    return base if not attempt else f"{base}-{attempt}"


def is_automatic(order: Order) -> bool:
    return order.auto_rule_id is not None or (order.client_order_id or "").startswith(
        AUTO_EXIT_PREFIX)


def parse_broker_ts(value) -> datetime | None:
    """An ISO 8601 time with its zone (`Z` or an offset; nanoseconds cut to
    microseconds); None for anything else."""
    if not isinstance(value, str) or not value:
        return None
    try:
        ts = datetime.fromisoformat(_FRACTION.sub(r"\1", value.replace("Z", "+00:00")))
    except ValueError:
        return None
    return ts if ts.tzinfo is not None else None


async def record_broker_fill_time(session: AsyncSession, order: Order, raw: dict) -> None:
    """The broker's `filled_at` of an automatic order on its trade, if not set yet."""
    ts = parse_broker_ts((raw or {}).get("filled_at"))
    if ts is None:
        return
    live = AutoTrade.status.in_((SENT, OPEN))
    if order.role == "entry":
        await session.execute(
            update(AutoTrade)
            .where(AutoTrade.entry_order_id == order.id, AutoTrade.filled_at.is_(None), live)
            .values(filled_at=ts, fill_ts_source="broker"))
        return
    match = [AutoTrade.exit_order_id == order.id]
    if order.parent_id is not None:
        match.append(AutoTrade.entry_order_id == order.parent_id)
    await session.execute(
        update(AutoTrade)
        .where(or_(*match), AutoTrade.exit_filled_at.is_(None), live)
        .values(exit_filled_at=ts))
