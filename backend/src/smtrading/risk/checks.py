"""pre_trade_check: validation and risk limits run before an order is accepted.

Checks run in a fixed order and stop at the first failure. Business rejections are
returned as CheckResult(ok=False, code, message), never raised. Every risk comparison
uses unrounded values (valuation *_raw fields, Ruling R8).
"""

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import ROUND_UP, Decimal, localcontext

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from smtrading.common.errors import ErrorCode, SMTError
from smtrading.common.money import decimal_to_str, quantize_money
from smtrading.engine.models import OrderSide, OrderType
from smtrading.engine.pricing import QuoteView, fill_price, notional_to_qty
from smtrading.engine.quotesource import QuoteSource
from smtrading.fees.calculator import compute_fx_fee, compute_trade_fee
from smtrading.fees.resolver import FeeQuote, load_rules, resolve_rule
from smtrading.instruments.models import Instrument
from smtrading.instruments.service import validate_price, validate_quantity
from smtrading.marketdata.types import Quote
from smtrading.portfolios.models import Portfolio, PortfolioStatus
from smtrading.risk.models import RiskProfile
from smtrading.risk.settings import get_default_spread_bps, get_stale_threshold, is_kill_switch_on
from smtrading.seed import DEFAULT_RISK_PROFILE_NAME
from smtrading.valuation.service import mark_price, value_portfolio

EUR = "EUR"
_PREC = 60
_LIMIT_TYPES = (OrderType.limit, OrderType.stop_limit)
_STOP_TYPES = (OrderType.stop, OrderType.stop_limit)


@dataclass
class CheckResult:
    ok: bool
    code: ErrorCode | None = None
    message: str | None = None
    estimated_price: Decimal | None = None
    estimated_notional_eur: Decimal | None = None
    estimated_fee: FeeQuote | None = None
    estimated_fx_fee_eur: Decimal | None = None
    buying_power_after_eur: Decimal | None = None


class _Reject(Exception):
    def __init__(self, code: ErrorCode, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _sign(x: Decimal) -> int:
    return (x > 0) - (x < 0)


async def _risk_profile(session: AsyncSession, portfolio: Portfolio) -> RiskProfile | None:
    """The portfolio's profile, else the seeded Default; None means no limits."""
    if portfolio.risk_profile_id is not None:
        return await session.get(RiskProfile, portfolio.risk_profile_id)
    return await session.scalar(
        select(RiskProfile).where(RiskProfile.name == DEFAULT_RISK_PROFILE_NAME)
    )


def _check_whitelist(profile: RiskProfile | None, instrument: Instrument) -> None:
    if profile is None:
        return
    for allowed, value, what in (
        (profile.allowed_symbols, instrument.symbol, "symbol"),
        (profile.allowed_asset_classes, str(instrument.asset_class), "asset class"),
        (profile.allowed_markets, instrument.market, "market"),
    ):
        if allowed and value not in allowed:
            raise _Reject(ErrorCode.SYMBOL_NOT_ALLOWED, f"{what} {value} is not allowed")


def _check_order_shape(
    instrument: Instrument, order_type: OrderType, qty: Decimal | None,
    notional: Decimal | None, limit_price: Decimal | None, stop_price: Decimal | None,
) -> None:
    if (qty is None) == (notional is None):
        raise _Reject(ErrorCode.INVALID_ORDER, "exactly one of qty and notional is required")
    if qty is not None:
        validate_quantity(instrument, qty)
    elif not notional > 0:
        raise _Reject(ErrorCode.INVALID_ORDER, "notional must be positive")
    if order_type in _LIMIT_TYPES and limit_price is None:
        raise _Reject(ErrorCode.INVALID_ORDER, f"{order_type} order requires a limit price")
    if order_type in _STOP_TYPES and stop_price is None:
        raise _Reject(ErrorCode.INVALID_ORDER, f"{order_type} order requires a stop price")
    for price in (limit_price, stop_price):
        if price is not None:
            validate_price(instrument, price)


async def _quote(qs: QuoteSource, symbol: str) -> Quote | None:
    try:
        return (await qs.get_quotes([symbol])).get(symbol)
    except SMTError:
        return None


async def _estimate_price(
    session: AsyncSession, qs: QuoteSource, *, portfolio: Portfolio, instrument: Instrument,
    side: OrderSide, order_type: OrderType, limit_price: Decimal | None,
    stop_price: Decimal | None,
) -> tuple[Decimal, Quote | None]:
    quote = await _quote(qs, instrument.symbol)
    if order_type == OrderType.market:
        if quote is None:
            raise _Reject(ErrorCode.PROVIDER_UNAVAILABLE, "quote unavailable")
        threshold = await get_stale_threshold(session, instrument.asset_class)
        if (datetime.now(UTC) - quote.ts).total_seconds() > threshold:
            raise _Reject(ErrorCode.STALE_QUOTE, f"quote older than {threshold}s")
    if order_type in _LIMIT_TYPES:
        return limit_price, quote
    if order_type == OrderType.stop:
        return stop_price, quote
    if quote is None:  # market or trailing stop
        if stop_price is not None:
            return stop_price, None
        raise _Reject(ErrorCode.PROVIDER_UNAVAILABLE, "quote unavailable")
    spread = await get_default_spread_bps(session, instrument.asset_class)
    price = fill_price(
        OrderType.market, side, QuoteView(quote.bid, quote.ask, quote.last, quote.ts),
        limit_price=None, slippage_bps=portfolio.slippage_bps,
        default_spread_bps=spread, tick=instrument.tick_size,
    )
    return price, quote


async def _run(
    session: AsyncSession, qs: QuoteSource, *, portfolio: Portfolio, instrument: Instrument,
    side: OrderSide, qty: Decimal | None, notional: Decimal | None, order_type: OrderType,
    limit_price: Decimal | None, stop_price: Decimal | None,
) -> CheckResult:
    # 1-3: switches and whitelist
    if await is_kill_switch_on(session):
        raise _Reject(ErrorCode.KILL_SWITCH_ACTIVE, "trading is halted by the kill switch")
    if portfolio.status == PortfolioStatus.locked:
        raise _Reject(ErrorCode.PORTFOLIO_LOCKED, "portfolio is locked")
    profile = await _risk_profile(session, portfolio)
    _check_whitelist(profile, instrument)

    # 4-5: order shape, then the price estimate
    _check_order_shape(instrument, order_type, qty, notional, limit_price, stop_price)
    price, quote = await _estimate_price(
        session, qs, portfolio=portfolio, instrument=instrument, side=side,
        order_type=order_type, limit_price=limit_price, stop_price=stop_price,
    )
    if qty is None:
        qty = notional_to_qty(notional, price, instrument.lot_size)

    ccy, base = instrument.currency, portfolio.base_currency
    valuation = await value_portfolio(session, qs, portfolio.id)
    before = next(
        (p.qty for p in valuation.positions if p.symbol == instrument.symbol), Decimal(0)
    )
    with localcontext() as ctx:
        ctx.prec = _PREC
        after = before + qty if side == OrderSide.buy else before - qty
        increases = abs(after) > abs(before)
        reduces = abs(after) < abs(before) and _sign(after) in (0, _sign(before))

    # 6-7: reduce-only and short permission
    if portfolio.status == PortfolioStatus.reduce_only and not reduces:
        raise _Reject(ErrorCode.REDUCE_ONLY, "portfolio is reduce-only")
    if side == OrderSide.sell and after < 0:
        if profile is not None and not profile.short_allowed:
            raise _Reject(ErrorCode.SHORT_NOT_ALLOWED, "short selling is disabled by the profile")
        if not instrument.shortable:
            raise _Reject(ErrorCode.SHORT_NOT_ALLOWED, f"{instrument.symbol} is not shortable")

    # estimates: notional, trade fee, fx fee
    to_eur = await qs.get_fx_rate(ccy, EUR)
    rule = resolve_rule(
        await load_rules(session), portfolio_id=portfolio.id,
        asset_class=instrument.asset_class, side=str(side),
    )
    to_rule_ccy = await qs.get_fx_rate(ccy, rule.currency)
    rule_to_eur = await qs.get_fx_rate(rule.currency, EUR)
    with localcontext() as ctx:
        ctx.prec = _PREC
        order_notional = quantize_money(qty * price, ccy)
        notional_eur = order_notional * to_eur
        fee = compute_trade_fee(rule, order_notional * to_rule_ccy)
        fee_eur = fee * rule_to_eur
    fx_fee_eur = Decimal(0)
    if side == OrderSide.buy and ccy != base:
        with localcontext() as ctx:
            ctx.prec = _PREC
            shortfall = order_notional - valuation.cash.get(ccy, Decimal(0))
        if shortfall > 0:
            base_to_ccy = await qs.get_fx_rate(base, ccy)
            base_to_eur = await qs.get_fx_rate(base, EUR)
            with localcontext() as ctx:
                ctx.prec = _PREC
                paid = quantize_money(shortfall / base_to_ccy, base, rounding=ROUND_UP)
                fx_fee_eur = compute_fx_fee(rule, paid, base) * base_to_eur
    with localcontext() as ctx:
        ctx.prec = _PREC
        costs = fee_eur + fx_fee_eur
        bp_after = valuation.buying_power_raw - costs
        if increases:
            bp_after -= notional_eur

    # 8: order and position limits
    max_order = profile.max_order_notional_eur if profile is not None else None
    if max_order is not None and notional_eur > max_order:
        raise _Reject(
            ErrorCode.RISK_LIMIT_EXCEEDED,
            f"order notional exceeds {decimal_to_str(max_order)} EUR",
        )
    if increases and profile is not None and profile.max_position_pct is not None:
        mark = mark_price(quote) if quote is not None else price
        equity = valuation.equity_raw
        with localcontext() as ctx:
            ctx.prec = _PREC
            weight = abs(after) * mark * to_eur / equity * 100 if equity > 0 else None
        if weight is None or weight > profile.max_position_pct:
            raise _Reject(
                ErrorCode.RISK_LIMIT_EXCEEDED,
                f"position would exceed {decimal_to_str(profile.max_position_pct)}% of equity",
            )

    # 9: buying power, only when exposure grows
    if increases and bp_after < 0:
        raise _Reject(ErrorCode.INSUFFICIENT_BUYING_POWER, "insufficient buying power")

    return CheckResult(
        ok=True, estimated_price=price, estimated_notional_eur=notional_eur,
        estimated_fee=FeeQuote(rule.id, fee, rule.currency),
        estimated_fx_fee_eur=fx_fee_eur, buying_power_after_eur=bp_after,
    )


async def pre_trade_check(
    session: AsyncSession,
    qs: QuoteSource,
    *,
    portfolio: Portfolio,
    instrument: Instrument,
    side: OrderSide,
    qty: Decimal | None,
    notional: Decimal | None,
    order_type: OrderType,
    limit_price: Decimal | None,
    stop_price: Decimal | None,
) -> CheckResult:
    try:
        return await _run(
            session, qs, portfolio=portfolio, instrument=instrument, side=side, qty=qty,
            notional=notional, order_type=order_type, limit_price=limit_price,
            stop_price=stop_price,
        )
    except _Reject as r:
        return CheckResult(ok=False, code=r.code, message=r.message)
    except SMTError as e:
        return CheckResult(ok=False, code=e.code, message=e.message)
