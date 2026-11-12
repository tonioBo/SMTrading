"""QuoteSource: the market data the engine needs, satisfied by MarketDataRouter.

FakeQuoteSource is an in-memory implementation for tests and offline tooling.
"""

from datetime import UTC, date, datetime
from decimal import Decimal, localcontext
from typing import Protocol, runtime_checkable

from smtrading.common.errors import ErrorCode, SMTError
from smtrading.marketdata.types import CalendarEvent, Quote


@runtime_checkable
class QuoteSource(Protocol):
    async def get_quotes(self, symbols: list[str], *, max_age_s: int = 5) -> dict[str, Quote]:
        """All requested quotes or SMTError (PROVIDER_UNAVAILABLE / SYMBOL_NOT_FOUND)."""
        ...

    async def get_fx_rate(self, base: str, quote: str) -> Decimal:
        """Units of `quote` per one unit of `base` (1 when equal)."""
        ...

    async def get_calendar(
        self, symbols: list[str], start: date, end: date
    ) -> list[CalendarEvent]:
        ...


def _unavailable(message: str, **details) -> SMTError:
    return SMTError(ErrorCode.PROVIDER_UNAVAILABLE, message, details)


class FakeQuoteSource:
    def __init__(self) -> None:
        self._quotes: dict[str, Quote] = {}
        self._fx: dict[tuple[str, str], Decimal] = {}
        self._calendar: list[CalendarEvent] = []

    def set_quote(
        self, symbol: str, bid: Decimal | None, ask: Decimal | None, last: Decimal | None,
        ts: datetime | None = None,
    ) -> None:
        self._quotes[symbol] = Quote(
            symbol=symbol, bid=bid, ask=ask, last=last, ts=ts or datetime.now(UTC),
            provider="fake",
        )

    def set_fx(self, base: str, quote: str, rate: Decimal) -> None:
        if not rate > 0:
            raise ValueError("fx rate must be positive")
        base, quote = base.upper(), quote.upper()
        self._fx[(base, quote)] = rate
        with localcontext() as ctx:
            ctx.prec = 60
            self._fx[(quote, base)] = Decimal(1) / rate

    def set_calendar(self, events: list[CalendarEvent]) -> None:
        self._calendar = list(events)

    async def get_quotes(self, symbols: list[str], *, max_age_s: int = 5) -> dict[str, Quote]:
        missing = [s for s in symbols if s not in self._quotes]
        if missing:
            raise _unavailable("quote unavailable", symbols=missing)
        return {s: self._quotes[s] for s in symbols}

    async def get_fx_rate(self, base: str, quote: str) -> Decimal:
        base, quote = base.upper(), quote.upper()
        if base == quote:
            return Decimal(1)
        rate = self._fx.get((base, quote))
        if rate is None:
            raise _unavailable("fx rate unavailable", base=base, quote=quote)
        return rate

    async def get_calendar(
        self, symbols: list[str], start: date, end: date
    ) -> list[CalendarEvent]:
        wanted = set(symbols)
        events = [
            e for e in self._calendar if e.symbol in wanted and start <= e.date <= end
        ]
        return sorted(events, key=lambda e: (e.date, e.symbol, e.kind))
