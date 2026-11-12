"""Mark-to-market valuation of a portfolio: equity, exposure, margin requirements.

Quantities come from the ledger `position` balances (source of truth); `avg_price` from
the `positions` projection. Every amount is converted to EUR with qs.get_fx_rate(ccy, "EUR").
The *_eur totals are display values rounded to cents; risk decisions must use the *_raw
totals or PortfolioValuation.margin_level(), which compare unrounded values.
"""

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, localcontext
from typing import Literal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from smtrading.common.errors import ErrorCode, SMTError
from smtrading.common.money import quantize_money
from smtrading.engine.quotesource import QuoteSource
from smtrading.instruments.models import Instrument
from smtrading.ledger.models import Account, AccountBalance, AccountKind
from smtrading.marketdata.types import Quote
from smtrading.portfolios.models import Portfolio
from smtrading.positions.models import Position

log = logging.getLogger(__name__)

EUR = "EUR"
_PREC = 60


@dataclass
class PositionValuation:
    symbol: str
    qty: Decimal
    avg_price: Decimal
    mark: Decimal
    currency: str
    market_value: Decimal  # signed, instrument currency
    market_value_eur: Decimal
    unrealized_pnl: Decimal  # instrument currency
    unrealized_pnl_eur: Decimal


MarginLevel = Literal["ok", "warning", "maintenance"]


@dataclass
class PortfolioValuation:
    """*_eur fields are display values (cents). Decisions use *_raw or margin_level()."""

    portfolio_id: UUID
    base_currency: str
    cash: dict[str, Decimal]
    collateral: dict[str, Decimal]
    margin_loan: dict[str, Decimal]
    positions: list[PositionValuation]
    equity_eur: Decimal
    cash_eur: Decimal
    exposure_eur: Decimal
    initial_req_eur: Decimal
    maint_req_eur: Decimal
    warn_req_eur: Decimal
    buying_power_eur: Decimal
    ts: datetime
    # Unrounded EUR totals: the values risk decisions must compare.
    equity_raw: Decimal
    exposure_raw: Decimal
    initial_req_raw: Decimal
    maint_req_raw: Decimal
    warn_req_raw: Decimal
    buying_power_raw: Decimal

    def margin_level(self) -> MarginLevel:
        if self.exposure_raw == 0:
            return "ok"
        if self.equity_raw < self.maint_req_raw:
            return "maintenance"
        if self.equity_raw < self.warn_req_raw:
            return "warning"
        return "ok"


def mark_price(quote: Quote) -> Decimal:
    """Mid when both bid and ask are present, else last; PROVIDER_UNAVAILABLE if neither."""
    if quote.bid is not None and quote.ask is not None:
        with localcontext() as ctx:
            ctx.prec = _PREC
            return (quote.bid + quote.ask) / 2
    if quote.last is not None:
        return quote.last
    raise SMTError(
        ErrorCode.PROVIDER_UNAVAILABLE, "quote has no price", {"symbol": quote.symbol}
    )


class _Fx:
    """Per-call cache of ccy -> EUR rates."""

    def __init__(self, qs: QuoteSource) -> None:
        self._qs = qs
        self._rates: dict[str, Decimal] = {EUR: Decimal(1)}

    async def to_eur(self, amount: Decimal, ccy: str) -> Decimal:
        if ccy not in self._rates:
            self._rates[ccy] = await self._qs.get_fx_rate(ccy, EUR)
        with localcontext() as ctx:
            ctx.prec = _PREC
            return amount * self._rates[ccy]


def _eur(amount: Decimal) -> Decimal:
    return quantize_money(amount, EUR)


async def _balances(
    session: AsyncSession, portfolio_id: UUID
) -> dict[AccountKind, dict[str, Decimal]]:
    rows = await session.execute(
        select(Account.kind, Account.asset, AccountBalance.balance)
        .join(AccountBalance, AccountBalance.account_id == Account.id)
        .where(Account.portfolio_id == portfolio_id)
    )
    out: dict[AccountKind, dict[str, Decimal]] = {k: {} for k in AccountKind}
    for kind, asset, balance in rows.all():
        out[kind][asset] = balance
    return out


async def value_portfolio(
    session: AsyncSession, qs: QuoteSource, portfolio_id: UUID
) -> PortfolioValuation:
    portfolio = await session.get(Portfolio, portfolio_id)
    if portfolio is None:
        raise SMTError(
            ErrorCode.NOT_FOUND, "portfolio not found", {"portfolio_id": str(portfolio_id)}
        )
    balances = await _balances(session, portfolio_id)
    held = {s: q for s, q in balances[AccountKind.position].items() if q != 0}
    symbols = sorted(held)

    currencies: dict[str, str] = {}
    avg_prices: dict[str, Decimal] = {}
    quotes: dict[str, Quote] = {}
    if symbols:
        currencies = dict(
            (await session.execute(
                select(Instrument.symbol, Instrument.currency)
                .where(Instrument.symbol.in_(symbols))
            )).all()
        )
        avg_prices = dict(
            (await session.execute(
                select(Position.symbol, Position.avg_price)
                .where(Position.portfolio_id == portfolio_id, Position.symbol.in_(symbols))
            )).all()
        )
        quotes = await qs.get_quotes(symbols)

    fx = _Fx(qs)
    positions: list[PositionValuation] = []
    for sym in symbols:
        quote = quotes.get(sym)
        if quote is None:
            raise SMTError(ErrorCode.PROVIDER_UNAVAILABLE, "quote unavailable", {"symbol": sym})
        qty = held[sym]
        ccy = currencies.get(sym)
        if ccy is None:
            raise SMTError(ErrorCode.SYMBOL_NOT_FOUND, "instrument not found", {"symbol": sym})
        avg = avg_prices.get(sym)
        if avg is None:
            log.warning(
                "position %s of portfolio %s has no projection row; avg_price=0",
                sym, portfolio_id,
            )
            avg = Decimal(0)
        mark = mark_price(quote)
        with localcontext() as ctx:
            ctx.prec = _PREC
            mv = qty * mark
            pnl = (mark - avg) * qty
        positions.append(PositionValuation(
            symbol=sym, qty=qty, avg_price=avg, mark=mark, currency=ccy,
            market_value=mv, market_value_eur=await fx.to_eur(mv, ccy),
            unrealized_pnl=pnl, unrealized_pnl_eur=await fx.to_eur(pnl, ccy),
        ))

    cash = balances[AccountKind.cash]
    collateral = balances[AccountKind.collateral]
    margin_loan = balances[AccountKind.margin_loan]
    cash_parts = [await fx.to_eur(amount, ccy) for ccy, amount in cash.items()]
    other_parts = [
        await fx.to_eur(amount, ccy) for ccy, amount in [*collateral.items(), *margin_loan.items()]
    ]

    with localcontext() as ctx:
        ctx.prec = _PREC
        cash_eur = sum(cash_parts, Decimal(0))
        other_eur = sum(other_parts, Decimal(0))
        market_eur = sum((p.market_value_eur for p in positions), Decimal(0))
        exposure = sum((abs(p.market_value_eur) for p in positions), Decimal(0))
        equity = cash_eur + other_eur + market_eur
        lev = portfolio.leverage
        initial = exposure / lev
        maint = initial * portfolio.maintenance_margin_pct / 100
        warn = initial * portfolio.warning_margin_pct / 100
        buying_power = equity * lev - exposure

    return PortfolioValuation(
        portfolio_id=portfolio_id, base_currency=portfolio.base_currency,
        cash=cash, collateral=collateral, margin_loan=margin_loan, positions=positions,
        equity_eur=_eur(equity), cash_eur=_eur(cash_eur), exposure_eur=_eur(exposure),
        initial_req_eur=_eur(initial), maint_req_eur=_eur(maint), warn_req_eur=_eur(warn),
        buying_power_eur=_eur(buying_power), ts=datetime.now(UTC),
        equity_raw=equity, exposure_raw=exposure, initial_req_raw=initial,
        maint_req_raw=maint, warn_req_raw=warn, buying_power_raw=buying_power,
    )
