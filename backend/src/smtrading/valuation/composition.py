"""Portfolio composition (portal «Composizione»): every open position with its EUR value,
weight and P/L, grouped by asset class and by currency, the net cash as its own slice.

Same sources as valuation.service: quantities from the ledger `position` balances,
`avg_price` from the `positions` projection, amounts converted with
qs.get_fx_rate(ccy, "EUR"). Unlike value_portfolio, a position without any quote (no live
one, no cached one) does not fail the request: its last/value/weight/P/L are null (shown
as «—», never 0), it is listed in `unpriced_symbols`, it is left out of the groups, and
`equity_eur` is null because the total is unknown. A position marked with a cached quote
is valued and flagged `stale` (Ruling R23).

The cash slice («Contante») is the net cash: cash + collateral (posted for shorts) +
margin loan (negative), so `cash_eur + Σ value_eur == equity_eur` as in the valuation.
Weights are percentages of the priced total (cash + priced positions), which is the
equity when every position is priced; shorts have negative values and weights. In
`by_currency` the cash is counted in its own currency (currency exposure).
"""

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, localcontext
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from smtrading.common.errors import ErrorCode, SMTError
from smtrading.common.money import quantize_money
from smtrading.engine.quotesource import QuoteSource
from smtrading.instruments.models import Instrument
from smtrading.ledger.models import AccountKind
from smtrading.marketdata.cache import read_quote
from smtrading.marketdata.types import Quote
from smtrading.mcp.dto import to_jsonable
from smtrading.portfolios.models import Portfolio
from smtrading.positions.models import Position
from smtrading.valuation.service import EUR, _balances, _Fx, mark_price

_PREC = 60
_PCT = Decimal("0.01")
CASH_KEY, CASH_LABEL = "cash", "Contante"
ASSET_CLASS_LABELS = {
    "us_equity": "Azioni USA",
    "eu_equity": "Azioni EU",
    "crypto": "Crypto",
    "forex": "Forex",
}
_MONEY_KINDS = (AccountKind.cash, AccountKind.collateral, AccountKind.margin_loan)


@dataclass
class _Row:
    symbol: str
    name: str
    asset_class: str
    market: str
    currency: str
    qty: Decimal
    avg_price: Decimal
    last: Decimal | None
    value_eur: Decimal | None  # unrounded
    pnl_eur: Decimal | None  # unrounded
    pnl_pct: Decimal | None
    stale: bool


async def _quote_or_none(qs: QuoteSource, symbol: str, redis) -> tuple[Quote | None, bool]:
    """(quote, stale): the live quote, else the cached one (stale), else None."""
    try:
        return (await qs.get_quotes([symbol]))[symbol], False
    except (SMTError, KeyError):
        pass
    cached = await read_quote(redis, symbol) if redis is not None else None
    return cached, cached is not None


async def _quotes(qs: QuoteSource, symbols: list[str], redis) -> dict[str, tuple[Quote | None, bool]]:
    try:
        batch = await qs.get_quotes(symbols)
        if all(s in batch for s in symbols):
            return {s: (batch[s], False) for s in symbols}
    except SMTError:
        pass
    return {s: await _quote_or_none(qs, s, redis) for s in symbols}


def _pct(part: Decimal, total: Decimal) -> Decimal | None:
    if total == 0:
        return None
    with localcontext() as ctx:
        ctx.prec = _PREC
        return (part / total * 100).quantize(_PCT)


def _money(amount: Decimal | None) -> Decimal | None:
    return None if amount is None else quantize_money(amount, EUR)


def _groups(parts: dict[tuple[str, str], Decimal], total: Decimal) -> list[dict[str, Any]]:
    """[{key, label, value_eur, weight_pct}] by absolute value, biggest first."""
    rows = sorted(parts.items(), key=lambda kv: (-abs(kv[1]), kv[0][0]))
    return [
        {"key": key, "label": label, "value_eur": _money(value), "weight_pct": _pct(value, total)}
        for (key, label), value in rows
    ]


async def portfolio_composition(
    session: AsyncSession, qs: QuoteSource, portfolio_id: UUID, *, redis=None
) -> dict[str, Any]:
    """The JSON-ready composition (numbers as strings, see the module docstring)."""
    if await session.get(Portfolio, portfolio_id) is None:
        raise SMTError(
            ErrorCode.NOT_FOUND, "portfolio not found", {"portfolio_id": str(portfolio_id)}
        )
    balances = await _balances(session, portfolio_id)
    held = {s: q for s, q in balances[AccountKind.position].items() if q != 0}
    symbols = sorted(held)
    fx = _Fx(qs)

    instruments: dict[str, Instrument] = {}
    avg_prices: dict[str, Decimal] = {}
    quotes: dict[str, tuple[Quote | None, bool]] = {}
    if symbols:
        instruments = {
            i.symbol: i for i in await session.scalars(
                select(Instrument).where(Instrument.symbol.in_(symbols)))
        }
        avg_prices = dict((await session.execute(
            select(Position.symbol, Position.avg_price)
            .where(Position.portfolio_id == portfolio_id, Position.symbol.in_(symbols))
        )).all())
        quotes = await _quotes(qs, symbols, redis)

    rows: list[_Row] = []
    for sym in symbols:
        inst = instruments.get(sym)
        if inst is None:
            raise SMTError(ErrorCode.SYMBOL_NOT_FOUND, "instrument not found", {"symbol": sym})
        qty, avg = held[sym], avg_prices.get(sym, Decimal(0))
        quote, stale = quotes.get(sym, (None, False))
        mark: Decimal | None = None
        if quote is not None:
            try:
                mark = mark_price(quote)
            except SMTError:
                mark = None
        value = pnl = pnl_pct = None
        if mark is not None:
            with localcontext() as ctx:
                ctx.prec = _PREC
                local_pnl = (mark - avg) * qty
                cost = abs(qty) * avg
                pnl_pct = (local_pnl / cost * 100).quantize(_PCT) if cost else None
            value = await fx.to_eur(qty * mark, inst.currency)
            pnl = await fx.to_eur(local_pnl, inst.currency)
        rows.append(_Row(
            symbol=sym, name=inst.name, asset_class=str(inst.asset_class), market=inst.market,
            currency=inst.currency, qty=qty, avg_price=avg, last=mark, value_eur=value,
            pnl_eur=pnl, pnl_pct=pnl_pct, stale=stale and mark is not None,
        ))

    cash_by_ccy: dict[str, Decimal] = {}
    for kind in _MONEY_KINDS:
        for ccy, amount in balances[kind].items():
            if amount != 0:
                cash_by_ccy[ccy] = cash_by_ccy.get(ccy, Decimal(0)) + await fx.to_eur(amount, ccy)

    priced = [r for r in rows if r.value_eur is not None]
    unpriced = [r.symbol for r in rows if r.value_eur is None]
    with localcontext() as ctx:
        ctx.prec = _PREC
        cash_eur = sum(cash_by_ccy.values(), Decimal(0))
        total = cash_eur + sum((r.value_eur for r in priced), Decimal(0))  # type: ignore[misc]
        by_class: dict[tuple[str, str], Decimal] = {}
        by_ccy: dict[tuple[str, str], Decimal] = {}
        for r in priced:
            ck = (r.asset_class, ASSET_CLASS_LABELS.get(r.asset_class, r.asset_class))
            by_class[ck] = by_class.get(ck, Decimal(0)) + r.value_eur  # type: ignore[operator]
            by_ccy[(r.currency, r.currency)] = (
                by_ccy.get((r.currency, r.currency), Decimal(0)) + r.value_eur)  # type: ignore[operator]
        if cash_eur != 0 or not by_class:
            by_class[(CASH_KEY, CASH_LABEL)] = cash_eur
        for ccy, amount in cash_by_ccy.items():
            by_ccy[(ccy, ccy)] = by_ccy.get((ccy, ccy), Decimal(0)) + amount
        if not by_ccy:
            by_ccy[(EUR, EUR)] = Decimal(0)

    rows.sort(key=lambda r: (r.value_eur is None, -abs(r.value_eur or 0), r.symbol))
    positions = [
        {
            "symbol": r.symbol, "name": r.name, "asset_class": r.asset_class,
            "market": r.market, "currency": r.currency, "qty": r.qty,
            "avg_price": r.avg_price, "last": r.last, "value_eur": _money(r.value_eur),
            "weight_pct": None if r.value_eur is None else _pct(r.value_eur, total),
            "pnl_eur": _money(r.pnl_eur), "pnl_pct": r.pnl_pct, "stale": r.stale,
        }
        for r in rows
    ]
    return to_jsonable({
        "as_of": datetime.now(UTC),
        "equity_eur": None if unpriced else _money(total),
        "cash_eur": _money(cash_eur),
        "positions": positions,
        "by_asset_class": _groups(by_class, total),
        "by_currency": _groups(by_ccy, total),
        "unpriced_symbols": unpriced,
    })
