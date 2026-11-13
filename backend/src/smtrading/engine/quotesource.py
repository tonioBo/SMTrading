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


class MemoQuoteSource:
    """Per-call memo over a QuoteSource (I2: no network I/O under the chain lock).

    Quotes are memoized by symbol and fx rates by pair for the lifetime of the instance;
    failures are remembered too, so a missing quote is not fetched again. `prefetch`
    loads, best-effort, what a call will need before it posts its first journal; after
    `freeze()` a miss raises PROVIDER_UNAVAILABLE instead of reaching the provider.
    `wrap` returns an existing memo unchanged, so nested calls share one memo.
    """

    def __init__(self, qs: QuoteSource) -> None:
        self._qs = qs
        self._quotes: dict[str, Quote] = {}
        self._failed: dict[str, SMTError] = {}
        self._fx: dict[tuple[str, str], Decimal] = {}
        self._frozen = False

    @classmethod
    def wrap(cls, qs: QuoteSource) -> "MemoQuoteSource":
        return qs if isinstance(qs, MemoQuoteSource) else cls(qs)

    @property
    def frozen(self) -> bool:
        return self._frozen

    def freeze(self) -> None:
        self._frozen = True

    def _frozen_miss(self, what: str, **details) -> SMTError:
        return _unavailable(f"{what} not prefetched before the ledger write", **details)

    async def get_quotes(self, symbols: list[str], *, max_age_s: int = 5) -> dict[str, Quote]:
        for s in symbols:
            if s in self._failed:
                raise self._failed[s]
        missing = [s for s in dict.fromkeys(symbols) if s not in self._quotes]
        if missing:
            if self._frozen:
                raise self._frozen_miss("quote", symbols=missing)
            self._quotes.update(await self._qs.get_quotes(missing, max_age_s=max_age_s))
        return {s: self._quotes[s] for s in symbols}

    async def get_fx_rate(self, base: str, quote: str) -> Decimal:
        key = (base.upper(), quote.upper())
        if key[0] == key[1]:
            return Decimal(1)
        if key not in self._fx:
            if self._frozen:
                raise self._frozen_miss("fx rate", base=key[0], quote=key[1])
            self._fx[key] = await self._qs.get_fx_rate(*key)
        return self._fx[key]

    async def get_calendar(
        self, symbols: list[str], start: date, end: date
    ) -> list[CalendarEvent]:
        if self._frozen:
            raise self._frozen_miss("calendar")
        return await self._qs.get_calendar(symbols, start, end)

    async def prefetch(
        self, symbols: list[str] | set[str], fx_pairs: list[tuple[str, str]] | set,
    ) -> None:
        """Load quotes and fx rates; errors are remembered (quotes) or ignored (fx: a
        later lookup before the freeze retries it). No-op once frozen."""
        if self._frozen:
            return
        wanted = sorted(s for s in set(symbols) if s not in self._quotes and s not in self._failed)
        if wanted:
            try:
                await self.get_quotes(wanted)
            except SMTError:
                for s in wanted:  # all-or-error sources: retry one by one
                    try:
                        await self.get_quotes([s])
                    except SMTError as e:
                        self._failed[s] = e
        for base, quote in sorted(set(fx_pairs)):
            try:
                await self.get_fx_rate(base, quote)
            except SMTError:
                pass
