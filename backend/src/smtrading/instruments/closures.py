"""Manual market closures («Chiusure straordinarie», admin page Mercati).

The rows live in `market_closures`; `market_status` stays synchronous and reads the
process registry (`market_hours.set_closures`). `ClosureRefresher` reloads it every 60 s
in the worker and in the API; an admin write reloads the API's at once."""

import asyncio
import logging
from datetime import UTC, date, datetime, time, timedelta

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Date,
    DateTime,
    Identity,
    String,
    Time,
    UniqueConstraint,
    func,
    select,
)
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import Mapped, mapped_column

from smtrading.common.db import Base
from smtrading.common.errors import ErrorCode, SMTError
from smtrading.instruments.market_hours import (
    CALENDARS,
    CLOSED,
    EARLY_CLOSE,
    Closure,
    set_closures,
)

log = logging.getLogger(__name__)

KINDS = (CLOSED, EARLY_CLOSE)
LOOKBACK = timedelta(days=7)  # rows kept in the registry before today (UTC)
MAX_NOTE_LEN = 200
_MARKETS_SQL = ", ".join(f"'{m}'" for m in CALENDARS)
_KINDS_SQL = ", ".join(f"'{k}'" for k in KINDS)


class MarketClosure(Base):
    __tablename__ = "market_closures"
    __table_args__ = (
        UniqueConstraint("market", "day", name="uq_market_closures_market_day"),
        CheckConstraint(f"market in ({_MARKETS_SQL})", name="ck_market_closures_market"),
        CheckConstraint(f"kind in ({_KINDS_SQL})", name="ck_market_closures_kind"),
        CheckConstraint(
            "(kind = 'early_close') = (close_time IS NOT NULL)",
            name="ck_market_closures_close_time",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    market: Mapped[str] = mapped_column(String(32))
    day: Mapped[date] = mapped_column(Date)
    kind: Mapped[str] = mapped_column(String(16))
    close_time: Mapped[time | None] = mapped_column(Time)  # local time of the exchange
    note: Mapped[str] = mapped_column(String(MAX_NOTE_LEN), default="")
    created_by: Mapped[str] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


def _closure(row: MarketClosure) -> Closure:
    return Closure(row.market, row.day, row.kind, row.close_time)


async def refresh_closures(session: AsyncSession) -> int:
    """Loads the rows from a week ago (UTC) on into the registry; returns their count."""
    since = datetime.now(UTC).date() - LOOKBACK
    rows = list(await session.scalars(select(MarketClosure).where(MarketClosure.day >= since)))
    set_closures(_closure(r) for r in rows)
    return len(rows)


class ClosureRefresher:
    """Reloads the registry every `interval_s`; a failed tick is logged and retried."""

    def __init__(self, sessionmaker: async_sessionmaker, *, interval_s: float = 60) -> None:
        self._sm = sessionmaker
        self._interval_s = interval_s

    async def tick(self) -> int:
        async with self._sm() as session:
            return await refresh_closures(session)

    async def run(self) -> None:
        while True:
            try:
                await self.tick()
            except Exception:
                log.warning("market closures not refreshed; retrying in %ss",
                            self._interval_s, exc_info=True)
            await asyncio.sleep(self._interval_s)


def _invalid(field: str, message: str) -> SMTError:
    return SMTError(ErrorCode.INVALID_ORDER, message, {"field": field})


async def add_closure(session: AsyncSession, *, market: str, day: date, kind: str,
                      close_time: time | None, note: str, by: str) -> MarketClosure:
    """Validates and stores one closure (flushed, not committed): a calendar market, a day
    from today (UTC) on, `close_time` with an early close only, one per market and day."""
    if market not in CALENDARS:
        raise _invalid("market", f"market deve essere uno tra {list(CALENDARS)}")
    if day < datetime.now(UTC).date():
        raise _invalid("day", "il giorno non può essere passato")
    if kind not in KINDS:
        raise _invalid("kind", f"kind deve essere uno tra {list(KINDS)}")
    if kind == EARLY_CLOSE and close_time is None:
        raise _invalid("close_time", "indicare l'ora della chiusura anticipata")
    note = (note or "").strip()
    if len(note) > MAX_NOTE_LEN:
        raise _invalid("note", f"la nota può avere al massimo {MAX_NOTE_LEN} caratteri")
    exists = await session.scalar(select(MarketClosure.id).where(
        MarketClosure.market == market, MarketClosure.day == day))
    if exists is not None:
        raise _invalid("day", "esiste già una chiusura per questo mercato e giorno")
    row = MarketClosure(market=market, day=day, kind=kind,
                        close_time=close_time if kind == EARLY_CLOSE else None,
                        note=note, created_by=by)
    session.add(row)
    await session.flush()
    return row


async def delete_closure(session: AsyncSession, closure_id: int) -> None:
    row = await session.get(MarketClosure, closure_id)
    if row is None:
        raise SMTError(ErrorCode.NOT_FOUND, "chiusura non trovata", {"closure_id": closure_id})
    await session.delete(row)
    await session.flush()
