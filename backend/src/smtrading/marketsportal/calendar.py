"""«Calendario della settimana» (plan 30): the earnings and ex-dividend dates of the
catalog's equities this week and next (from the cached fundamentals: Yahoo v7 quote),
the instruments held in the active portfolios highlighted, plus the central banks'
meetings.

Macro events: Finnhub's economic calendar is a paid endpoint (403 on the free key,
checked 21/11/2026), so the macro part is a curated static list of the 2026 Fed (FOMC)
and ECB monetary policy meetings from their official calendars, dated MACRO_AS_OF; CPI
and payroll dates are not shown (no free source).
"""

from collections import defaultdict
from datetime import date, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from smtrading.instruments.models import AssetClass, Instrument
from smtrading.portfolios.models import Portfolio, PortfolioStatus
from smtrading.positions.models import Position

MACRO_AS_OF = "2026-11-21"
MACRO_SOURCES = {
    "Fed": "https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm",
    "BCE": "https://www.ecb.europa.eu/press/calendars/mgcgc/html/index.it.html",
}
# (decision day, bank, label): the FOMC's second day, the ECB's monetary policy meeting day
MACRO_2026: tuple[tuple[str, str, str], ...] = (
    ("2026-01-28", "Fed", "Riunione FOMC (27-28 gennaio)"),
    ("2026-02-05", "BCE", "Riunione di politica monetaria BCE"),
    ("2026-03-18", "Fed", "Riunione FOMC (17-18 marzo)"),
    ("2026-03-19", "BCE", "Riunione di politica monetaria BCE"),
    ("2026-04-29", "Fed", "Riunione FOMC (28-29 aprile)"),
    ("2026-04-30", "BCE", "Riunione di politica monetaria BCE"),
    ("2026-06-11", "BCE", "Riunione di politica monetaria BCE"),
    ("2026-06-17", "Fed", "Riunione FOMC (16-17 giugno)"),
    ("2026-07-23", "BCE", "Riunione di politica monetaria BCE"),
    ("2026-07-29", "Fed", "Riunione FOMC (28-29 luglio)"),
    ("2026-09-10", "BCE", "Riunione di politica monetaria BCE"),
    ("2026-09-16", "Fed", "Riunione FOMC (15-16 settembre)"),
    ("2026-10-28", "Fed", "Riunione FOMC (27-28 ottobre)"),
    ("2026-10-29", "BCE", "Riunione di politica monetaria BCE"),
    ("2026-12-09", "Fed", "Riunione FOMC (8-9 dicembre)"),
    ("2026-12-17", "BCE", "Riunione di politica monetaria BCE"),
)
EQUITY_CLASSES = (AssetClass.us_equity, AssetClass.eu_equity)


def window(today: date) -> tuple[date, date]:
    """Monday of this week to Sunday of next week."""
    start = today - timedelta(days=today.weekday())
    return start, start + timedelta(days=13)


async def equities(session: AsyncSession) -> list[Instrument]:
    return list(await session.scalars(
        select(Instrument).where(Instrument.active.is_(True),
                                 Instrument.asset_class.in_(EQUITY_CLASSES))
        .order_by(Instrument.symbol)))


async def held(session: AsyncSession) -> dict[str, list[str]]:
    """symbol -> the non-archived portfolios holding it (open position)."""
    rows = (await session.execute(
        select(Position.symbol, Portfolio.name)
        .join(Portfolio, Portfolio.id == Position.portfolio_id)
        .where(Position.qty != 0, Portfolio.status != PortfolioStatus.archived)
        .order_by(Portfolio.name))).all()
    out: dict[str, list[str]] = defaultdict(list)
    for sym, name in rows:
        if name not in out[sym]:
            out[sym].append(name)
    return dict(out)


def assemble(instruments: list[Instrument], figures: dict[str, dict],
             holdings: dict[str, list[str]], today: date) -> dict:
    """The events of the window, by date then held first then symbol."""
    start, end = window(today)
    events: list[dict] = []
    for i in instruments:
        f = figures.get(i.symbol) or {}
        for kind, key in (("earnings", "next_earnings"), ("ex_dividend", "ex_dividend")):
            raw = f.get(key)
            if not raw:
                continue
            d = date.fromisoformat(raw)
            if not start <= d <= end:
                continue
            events.append({
                "date": d.isoformat(), "kind": kind, "symbol": i.symbol, "name": i.name,
                "sector": i.sector, "held": i.symbol in holdings,
                "portfolios": holdings.get(i.symbol, []),
                "estimate": bool(f.get("earnings_estimate")) if kind == "earnings" else False,
                "label": None,
            })
    for day, bank, label in MACRO_2026:
        d = date.fromisoformat(day)
        if start <= d <= end:
            events.append({"date": day, "kind": "macro", "symbol": None, "name": bank,
                           "sector": None, "held": False, "portfolios": [],
                           "estimate": False, "label": label})
    events.sort(key=lambda e: (e["date"], e["kind"] != "macro", not e["held"],
                               e["symbol"] or ""))
    return {
        "from": start.isoformat(), "to": end.isoformat(), "today": today.isoformat(),
        "events": events, "covered": sum(1 for i in instruments if i.symbol in figures),
        "total": len(instruments),
        "macro": {"as_of": MACRO_AS_OF, "sources": MACRO_SOURCES,
                  "note": "Riunioni delle banche centrali dai calendari ufficiali 2026; "
                          "CPI e payroll non disponibili (nessuna fonte gratuita)."},
    }
