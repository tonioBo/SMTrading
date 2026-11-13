"""Equity snapshots: `day_open` (one per portfolio and local date) and `periodic`."""

import logging
from collections.abc import Callable
from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from smtrading.common.errors import SMTError
from smtrading.engine.quotesource import QuoteSource
from smtrading.portfolios.models import Portfolio
from smtrading.risk.models import EquitySnapshot, SnapshotKind
from smtrading.valuation.service import PortfolioValuation, value_portfolio

log = logging.getLogger("smtrading.corporate.snapshots")


def local_date(portfolio: Portfolio, now: datetime) -> date:
    return now.astimezone(ZoneInfo(portfolio.timezone)).date()


def _values(valuation: PortfolioValuation) -> dict:
    return {
        "equity_eur": valuation.equity_eur,
        "cash_eur": valuation.cash_eur,
        "exposure_eur": valuation.exposure_eur,
        "margin_used_eur": valuation.initial_req_eur,
    }


async def _day_open(session: AsyncSession, portfolio_id, day: date) -> EquitySnapshot | None:
    return await session.scalar(
        select(EquitySnapshot).where(
            EquitySnapshot.portfolio_id == portfolio_id,
            EquitySnapshot.kind == SnapshotKind.day_open,
            EquitySnapshot.local_date == day,
        )
    )


async def ensure_day_open(
    session: AsyncSession, qs: QuoteSource, portfolio: Portfolio, now: datetime, *,
    redis=None,
) -> EquitySnapshot:
    """The `day_open` snapshot of the portfolio's local date of `now`, created if missing.

    Concurrent callers race on the unique (portfolio_id, kind, local_date) constraint:
    the loser's insert does nothing and it reads the winner's row.
    """
    day = local_date(portfolio, now)
    existing = await _day_open(session, portfolio.id, day)
    if existing is not None:
        return existing
    valuation = await value_portfolio(session, qs, portfolio.id, redis=redis)
    await session.execute(
        insert(EquitySnapshot)
        .values(
            portfolio_id=portfolio.id, ts=now, kind=SnapshotKind.day_open, local_date=day,
            **_values(valuation),
        )
        .on_conflict_do_nothing(constraint="uq_equity_snapshots_day_open")
    )
    return await _day_open(session, portfolio.id, day)


async def take_snapshots(
    sessionmaker: async_sessionmaker[AsyncSession], qs: QuoteSource, *,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> None:
    """A `periodic` snapshot of every portfolio (and its `day_open` if missing).

    Each portfolio has its own transaction; errors are logged and skipped (a business
    or provider SMTError as a warning, without traceback).
    """
    async with sessionmaker() as session:
        ids = list(await session.scalars(select(Portfolio.id).order_by(Portfolio.id)))
    for portfolio_id in ids:
        try:
            async with sessionmaker() as session:
                portfolio = await session.get(Portfolio, portfolio_id)
                now = clock()
                await ensure_day_open(session, qs, portfolio, now)
                valuation = await value_portfolio(session, qs, portfolio_id)
                session.add(EquitySnapshot(
                    portfolio_id=portfolio_id, ts=now, kind=SnapshotKind.periodic,
                    local_date=None, **_values(valuation),
                ))
                await session.commit()
        except SMTError as e:
            log.warning("snapshot of portfolio %s skipped: %s %s", portfolio_id, e.code, e.message)
        except Exception:
            log.exception("snapshot of portfolio %s failed", portfolio_id)
