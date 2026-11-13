"""Watchlists and price alerts of an agent.

Limits (per agent): MAX_WATCHLISTS watchlists, MAX_WATCHLIST_SYMBOLS symbols per watchlist,
MAX_ACTIVE_ALERTS active alerts; beyond them RISK_LIMIT_EXCEEDED. The agent row is locked
FOR UPDATE before a limit is checked, so concurrent calls of one agent cannot overshoot.

Symbols must be active instruments (canonical form). Streaming: a symbol added to a
watchlist is tracked with source "watchlists" and untracked once no watchlist holds it;
an alert's symbol is tracked with source "alerts" and untracked once no active alert
references it. Both are queued post-commit effects (events.queue_track/queue_untrack).

Another agent's watchlist or alert is FORBIDDEN; an unknown (or deleted) one NOT_FOUND.
Alerts are soft-deleted (status "deleted") and hidden from list_alerts.
"""

from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from smtrading.agents.models import Agent
from smtrading.common.errors import ErrorCode, SMTError
from smtrading.engine.quotesource import QuoteSource
from smtrading.events import queue_track, queue_untrack
from smtrading.instruments.service import canonical_symbol, get_instrument
from smtrading.marketdata.types import Quote
from smtrading.watch.models import Alert, AlertCondition, Watchlist, WatchlistItem

MAX_WATCHLISTS = 20
MAX_WATCHLIST_SYMBOLS = 200
MAX_ACTIVE_ALERTS = 100
MAX_NAME_LEN = 128
MAX_NOTE_LEN = 512

ACTIVE = "active"
TRIGGERED = "triggered"
DELETED = "deleted"
LISTABLE_STATUSES = (ACTIVE, TRIGGERED)

_PRICE_CONDITIONS = (AlertCondition.price_above, AlertCondition.price_below)


def _invalid(field: str, message: str) -> SMTError:
    return SMTError(ErrorCode.INVALID_ORDER, message, {"field": field})


def _limit(message: str, limit: int) -> SMTError:
    return SMTError(ErrorCode.RISK_LIMIT_EXCEEDED, message, {"limit": limit})


def reference_price(quote: Quote) -> Decimal | None:
    """`last`, else the bid/ask mid, else None."""
    if quote.last is not None:
        return quote.last
    if quote.bid is not None and quote.ask is not None:
        return (quote.bid + quote.ask) / 2
    return None


async def _lock_agent(session: AsyncSession, agent_id: UUID) -> None:
    found = await session.scalar(select(Agent.id).where(Agent.id == agent_id).with_for_update())
    if found is None:
        raise SMTError(ErrorCode.FORBIDDEN, "unknown agent")


async def _symbols(session: AsyncSession, symbols: list[str]) -> list[str]:
    """Canonical symbols of active instruments, deduplicated in order (SYMBOL_NOT_FOUND)."""
    if not isinstance(symbols, list) or not symbols:
        raise _invalid("symbols", "symbols must be a non-empty list of symbols")
    out: list[str] = []
    for raw in symbols:
        if not isinstance(raw, str) or not raw.strip():
            raise _invalid("symbols", "symbols must be non-empty strings")
        symbol = (await get_instrument(session, raw)).symbol
        if symbol not in out:
            out.append(symbol)
    return out


# -- watchlists ------------------------------------------------------------------------


async def _items(session: AsyncSession, watchlist_id: UUID) -> list[str]:
    return list(await session.scalars(
        select(WatchlistItem.symbol).where(WatchlistItem.watchlist_id == watchlist_id)
        .order_by(WatchlistItem.symbol)
    ))


async def _watchlist_dto(session: AsyncSession, wl: Watchlist) -> dict[str, Any]:
    return {"id": wl.id, "name": wl.name, "symbols": await _items(session, wl.id),
            "created_at": wl.created_at}


async def _own_watchlist(session: AsyncSession, agent_id: UUID, watchlist_id: UUID) -> Watchlist:
    wl = await session.scalar(
        select(Watchlist).where(Watchlist.id == watchlist_id).with_for_update()
    )
    if wl is None:
        raise SMTError(ErrorCode.NOT_FOUND, "watchlist not found", {"watchlist_id": str(watchlist_id)})
    if wl.agent_id != agent_id:
        raise SMTError(ErrorCode.FORBIDDEN, "watchlist of another agent")
    return wl


async def _untrack_unwatched(session: AsyncSession, symbols: list[str]) -> None:
    await session.flush()
    for symbol in symbols:
        still = await session.scalar(
            select(WatchlistItem.symbol).where(WatchlistItem.symbol == symbol).limit(1)
        )
        if still is None:
            queue_untrack(session, symbol, "watchlists")


async def create_watchlist(session: AsyncSession, agent_id: UUID, name: str) -> dict[str, Any]:
    if not isinstance(name, str) or not name.strip():
        raise _invalid("name", "name must be a non-empty string")
    name = name.strip()
    if len(name) > MAX_NAME_LEN:
        raise _invalid("name", f"name must be at most {MAX_NAME_LEN} characters")
    await _lock_agent(session, agent_id)
    count = await session.scalar(
        select(func.count()).select_from(Watchlist).where(Watchlist.agent_id == agent_id)
    )
    if count >= MAX_WATCHLISTS:
        raise _limit(f"at most {MAX_WATCHLISTS} watchlists per agent", MAX_WATCHLISTS)
    exists = await session.scalar(
        select(Watchlist.id).where(Watchlist.agent_id == agent_id, Watchlist.name == name)
    )
    if exists is not None:
        raise _invalid("name", "a watchlist with this name already exists")
    wl = Watchlist(agent_id=agent_id, name=name)
    session.add(wl)
    await session.flush()
    await session.refresh(wl)
    return await _watchlist_dto(session, wl)


async def add_to_watchlist(
    session: AsyncSession, agent_id: UUID, watchlist_id: UUID, symbols: list[str]
) -> dict[str, Any]:
    wl = await _own_watchlist(session, agent_id, watchlist_id)
    wanted = await _symbols(session, symbols)
    present = set(await _items(session, wl.id))
    new = [s for s in wanted if s not in present]
    if len(present) + len(new) > MAX_WATCHLIST_SYMBOLS:
        raise _limit(f"at most {MAX_WATCHLIST_SYMBOLS} symbols per watchlist",
                     MAX_WATCHLIST_SYMBOLS)
    session.add_all(WatchlistItem(watchlist_id=wl.id, symbol=s) for s in new)
    await session.flush()
    for symbol in wanted:
        queue_track(session, symbol, "watchlists")
    return await _watchlist_dto(session, wl)


async def remove_from_watchlist(
    session: AsyncSession, agent_id: UUID, watchlist_id: UUID, symbols: list[str]
) -> dict[str, Any]:
    wl = await _own_watchlist(session, agent_id, watchlist_id)
    if not isinstance(symbols, list) or not symbols:
        raise _invalid("symbols", "symbols must be a non-empty list of symbols")
    if not all(isinstance(s, str) and s.strip() for s in symbols):
        raise _invalid("symbols", "symbols must be non-empty strings")
    wanted = sorted({canonical_symbol(s) for s in symbols})
    removed = list(await session.scalars(
        delete(WatchlistItem)
        .where(WatchlistItem.watchlist_id == wl.id, WatchlistItem.symbol.in_(wanted))
        .returning(WatchlistItem.symbol)
    ))
    await _untrack_unwatched(session, sorted(removed))
    return await _watchlist_dto(session, wl)


async def list_watchlists(session: AsyncSession, agent_id: UUID) -> list[dict[str, Any]]:
    rows = await session.scalars(
        select(Watchlist).where(Watchlist.agent_id == agent_id)
        .order_by(Watchlist.created_at, Watchlist.name)
    )
    return [await _watchlist_dto(session, wl) for wl in rows.all()]


async def delete_watchlist(session: AsyncSession, agent_id: UUID, watchlist_id: UUID) -> dict:
    wl = await _own_watchlist(session, agent_id, watchlist_id)
    symbols = await _items(session, wl.id)
    await session.execute(delete(WatchlistItem).where(WatchlistItem.watchlist_id == wl.id))
    await session.delete(wl)
    await _untrack_unwatched(session, symbols)
    return {"id": watchlist_id, "deleted": True}


# -- alerts ----------------------------------------------------------------------------


async def _current_price(qs: QuoteSource, symbol: str) -> Decimal:
    try:
        quote = (await qs.get_quotes([symbol])).get(symbol)
    except SMTError:
        quote = None
    price = reference_price(quote) if quote is not None else None
    if price is None or price <= 0:
        raise SMTError(ErrorCode.INVALID_ORDER, "quota non disponibile", {"symbol": symbol})
    return price


async def create_alert(
    session: AsyncSession, qs: QuoteSource, agent_id: UUID, symbol: str,
    condition: AlertCondition, value: Decimal, note: str | None = None,
) -> Alert:
    if not isinstance(value, Decimal) or not value.is_finite():
        raise _invalid("value", "value must be a finite decimal number")
    if condition in _PRICE_CONDITIONS and value <= 0:
        raise _invalid("value", "value must be positive for a price alert")
    if note is not None and len(note) > MAX_NOTE_LEN:
        raise _invalid("note", f"note must be at most {MAX_NOTE_LEN} characters")
    instrument = await get_instrument(session, symbol)
    ref_price = None
    if condition not in _PRICE_CONDITIONS:
        ref_price = await _current_price(qs, instrument.symbol)
    await _lock_agent(session, agent_id)
    active = await session.scalar(
        select(func.count()).select_from(Alert)
        .where(Alert.agent_id == agent_id, Alert.status == ACTIVE)
    )
    if active >= MAX_ACTIVE_ALERTS:
        raise _limit(f"at most {MAX_ACTIVE_ALERTS} active alerts per agent", MAX_ACTIVE_ALERTS)
    alert = Alert(
        agent_id=agent_id, symbol=instrument.symbol, condition=condition, value=value,
        ref_price=ref_price, status=ACTIVE, note=note,
    )
    session.add(alert)
    await session.flush()
    await session.refresh(alert)
    queue_track(session, alert.symbol, "alerts")
    return alert


async def list_alerts(
    session: AsyncSession, agent_id: UUID, status: str | None = None
) -> list[Alert]:
    if status is not None and status not in LISTABLE_STATUSES:
        raise SMTError(ErrorCode.INVALID_ORDER, f"status must be one of {list(LISTABLE_STATUSES)}",
                       {"field": "status", "allowed": list(LISTABLE_STATUSES)})
    statuses = (status,) if status is not None else LISTABLE_STATUSES
    return list(await session.scalars(
        select(Alert).where(Alert.agent_id == agent_id, Alert.status.in_(statuses))
        .order_by(Alert.created_at, Alert.id)
    ))


async def untrack_if_no_active_alert(session: AsyncSession, symbol: str) -> None:
    await session.flush()
    still = await session.scalar(
        select(Alert.id).where(Alert.symbol == symbol, Alert.status == ACTIVE).limit(1)
    )
    if still is None:
        queue_untrack(session, symbol, "alerts")


async def delete_alert(session: AsyncSession, agent_id: UUID, alert_id: UUID) -> Alert:
    alert = await session.scalar(select(Alert).where(Alert.id == alert_id).with_for_update())
    if alert is None or alert.status == DELETED:
        raise SMTError(ErrorCode.NOT_FOUND, "alert not found", {"alert_id": str(alert_id)})
    if alert.agent_id != agent_id:
        raise SMTError(ErrorCode.FORBIDDEN, "alert of another agent")
    was_active = alert.status == ACTIVE
    alert.status = DELETED
    if was_active:
        await untrack_if_no_active_alert(session, alert.symbol)
    return alert
