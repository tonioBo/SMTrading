"""pre_trade_check: validation and risk limits run before an order is accepted.

Checks run in a fixed order and stop at the first failure. Business rejections are
returned as CheckResult(ok=False, code, message), never raised. Every risk comparison
uses unrounded values (valuation *_raw fields, Ruling R8).

- `check_quote_age=False` skips the STALE_QUOTE check of market orders (placement while
  the market is closed: the age is checked again at execution).
- `reduce_only` orders (Ruling R22) are clamped to the position at fill, so they never
  increase exposure: they are checked as reducing.
- Ruling R25: with the kill switch on, only orders that strictly reduce the position
  (|after| < |before|, no sign flip; e.g. reduce_only closes) are accepted.
- «Blocco aperture» (risk.halts): while a halt covers the portfolio, the same test
  applies with TRADING_HALTED ("Aperture sospese dal CEO: <reason>"); the kill switch is
  checked first.
- With `redis`, the valuation may mark symbols with cached quotes (Ruling R23); only a
  strictly reducing order is accepted on such a stale valuation.
- A real-money portfolio (linked to a live kind) also runs `risk.live_checks.live_pre_trade`
  (link armed, strict kill switch, no short, cash cap, fuse) once the estimates are known
  and before the risk limits (order notional, position weight, buying power). With
  `live_effects=False` (a preview, a modify) the fuse never trips the portfolio.
- «Piani» (`goals.plans`): a position of an ended piano still being closed (pending,
  closing) cannot grow nor turn to the other side (long 1, sell 3), even when
  |after| <= |before|: RISK_LIMIT_EXCEEDED «posizione in chiusura per fine piano» with
  details {reason: "plan_grace" (a code kept for compatibility), symbol}. A reduce_only
  order is clamped first, so it never flips.
"""

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, localcontext
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from smtrading.autorules.ownership import owner_rule
from smtrading.brokers.models import BrokerLink
from smtrading.common.errors import ErrorCode, SMTError
from smtrading.common.money import decimal_to_str, quantize_money
from smtrading.engine.models import OrderSide, OrderType
from smtrading.engine.pricing import QuoteView, fill_price, notional_to_qty
from smtrading.engine.quotesource import QuoteSource
from smtrading.fees.calculator import compute_trade_fee
from smtrading.fees.resolver import FeeQuote, load_rules, resolve_rule
from smtrading.goals.plans import adds_to, open_closure, plan_closing_error
from smtrading.instruments.models import AssetClass, Instrument
from smtrading.instruments.service import validate_price, validate_quantity
from smtrading.intraday.day_trades import day_trade_leverage, fresh_buying_power, own_cap_usage
from smtrading.intraday.models import day_scope
from smtrading.ledger.models import Account, AccountBalance, AccountKind
from smtrading.marketdata.quality import SUSPECT_REASON
from smtrading.marketdata.types import Quote
from smtrading.portfolios.models import ARCHIVED_MESSAGE, Portfolio, PortfolioStatus
from smtrading.risk.halts import covering_halt, halt_message
from smtrading.risk.live_checks import fx_fee_eur, live_link_of, live_pre_trade, to_currency
from smtrading.risk.models import RiskProfile, TradingHalt
from smtrading.risk.profiles import effective_risk_profile
from smtrading.risk.settings import get_default_spread_bps, get_stale_threshold, is_kill_switch_on
from smtrading.valuation.service import PortfolioValuation, mark_price, value_portfolio

EUR = "EUR"
_PREC = 60
_LIMIT_TYPES = (OrderType.limit, OrderType.stop_limit)
_STOP_TYPES = (OrderType.stop, OrderType.stop_limit)
OWNED_MESSAGE = ("{symbol} è gestito da una regola automatica attiva su questo portafoglio: "
                 "mettila in pausa prima di operare a mano")
INACTIVE_MESSAGE = ("la regola automatica di {symbol} non è attiva e non ha operazioni "
                    "aperte: l'ordine non può partire")


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
    details: dict | None = None


class _Reject(Exception):
    def __init__(self, code: ErrorCode, message: str, details: dict | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details


def _sign(x: Decimal) -> int:
    return (x > 0) - (x < 0)


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


def _kill_switch() -> _Reject:
    return _Reject(ErrorCode.KILL_SWITCH_ACTIVE, "trading is halted by the kill switch")


def _halted(halt: TradingHalt) -> _Reject:
    return _Reject(ErrorCode.TRADING_HALTED, halt_message(halt))


async def _held(session: AsyncSession, portfolio_id, symbol: str) -> Decimal:
    balance = await session.scalar(
        select(AccountBalance.balance)
        .join(Account, Account.id == AccountBalance.account_id)
        .where(
            Account.portfolio_id == portfolio_id, Account.kind == AccountKind.position,
            Account.asset == symbol,
        )
    )
    return balance if balance is not None else Decimal(0)


async def _quote(qs: QuoteSource, symbol: str) -> Quote | None:
    try:
        return (await qs.get_quotes([symbol])).get(symbol)
    except SMTError:
        return None


async def _estimate_price(
    session: AsyncSession, qs: QuoteSource, *, portfolio: Portfolio, instrument: Instrument,
    side: OrderSide, order_type: OrderType, limit_price: Decimal | None,
    stop_price: Decimal | None, check_quote_age: bool,
) -> tuple[Decimal, Quote | None]:
    quote = await _quote(qs, instrument.symbol)
    if order_type == OrderType.market and quote is None:
        raise _Reject(ErrorCode.PROVIDER_UNAVAILABLE, "quote unavailable")
    if order_type == OrderType.market and check_quote_age:
        threshold = await get_stale_threshold(session, instrument.asset_class)
        if (datetime.now(UTC) - quote.ts).total_seconds() > threshold:
            raise _Reject(ErrorCode.STALE_QUOTE, f"quote older than {threshold}s")
        if quote.suspect is not None:  # marketdata.quality: not confirmed by a second source
            raise _Reject(ErrorCode.STALE_QUOTE, "quote not confirmed yet, retry shortly",
                          {"reason": SUSPECT_REASON})
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


async def _check_own_day_trade_cap(
    session: AsyncSession, portfolio: Portfolio, instrument: Instrument, now: datetime
) -> None:
    """The portfolio's optional cap `max_day_trades_5d`; no query without a cap."""
    if portfolio.max_day_trades_5d is None:
        return
    usage = await own_cap_usage(session, portfolio, now, instrument.symbol)
    if usage.remaining is None or usage.remaining > 0:
        return
    when = (f"la prossima si libera il {usage.frees_on:%d/%m}" if usage.frees_on
            else "quando una delle aperte si chiude senza operazione")
    raise _Reject(
        ErrorCode.DAY_TRADE_LIMIT,
        f"Tetto operazioni di giornata del portafoglio raggiunto: {usage.used} usate su "
        f"{usage.limit} nelle ultime 5 sedute (più {usage.pending} già aperte); {when}",
        {"used": usage.used, "limit": usage.limit, "remaining": 0,
         "frees_on": usage.frees_on.isoformat() if usage.frees_on else None},
    )


def _check_day_trade_leverage(
    portfolio: Portfolio, link: BrokerLink | None, valuation: PortfolioValuation,
    notional_eur: Decimal, costs: Decimal, increases: bool,
) -> None:
    """Step 9's buying power (equity x leverage - exposure) with the day-trade leverage,
    when it is below the portfolio's (otherwise step 9 already checked the same)."""
    lev = day_trade_leverage(portfolio, link)
    if not increases or lev >= portfolio.leverage:
        return
    with localcontext() as ctx:
        ctx.prec = _PREC
        left = valuation.equity_raw * lev - valuation.exposure_raw - costs - notional_eur
    if left < 0:
        raise _Reject(
            ErrorCode.INSUFFICIENT_BUYING_POWER,
            f"Esposizione oltre la leva ammessa per le operazioni di giornata ({lev}) su "
            "questo portafoglio",
            {"reason": "day_trade_leverage", "leverage": str(lev)},
        )


async def _check_broker_buying_power(
    qs: QuoteSource, portfolio: Portfolio, link: BrokerLink | None, instrument: Instrument,
    order_notional: Decimal, increases: bool, now: datetime,
) -> None:
    """The broker's buying power from a fresh account state (in USD), when exposure grows;
    no fresh state: no refusal."""
    if not increases:
        return
    bp = fresh_buying_power(link, now, crypto=instrument.asset_class == AssetClass.crypto)
    if bp is None:
        return
    ccy = instrument.currency
    rate = Decimal(1) if ccy == "USD" else await qs.get_fx_rate(ccy, "USD")
    with localcontext() as ctx:
        ctx.prec = _PREC
        raw = order_notional * rate
    if raw <= bp:
        return
    usd = quantize_money(raw, "USD")
    read_at = link.pdt_state_at
    at = read_at.astimezone(ZoneInfo(portfolio.timezone)).strftime("%H:%M")
    raise _Reject(
        ErrorCode.INSUFFICIENT_BUYING_POWER,
        f"Importo di {decimal_to_str(usd)} $ oltre il potere d'acquisto del conto "
        f"{link.name} ({decimal_to_str(bp)} $, letto alle {at})",
        {"reason": "broker_buying_power", "notional": decimal_to_str(usd),
         "buying_power": decimal_to_str(bp), "read_at": read_at.isoformat()},
    )


async def _run(
    session: AsyncSession, qs: QuoteSource, *, portfolio: Portfolio, instrument: Instrument,
    side: OrderSide, qty: Decimal | None, notional: Decimal | None, order_type: OrderType,
    limit_price: Decimal | None, stop_price: Decimal | None, check_quote_age: bool,
    reduce_only: bool, redis, live_flatten: bool = False, now: datetime | None = None,
    live_effects: bool = True, exclude_order=None, day_trade: bool = False,
    auto_rule_id=None,
) -> CheckResult:
    # 1-3: switches and whitelist; Ruling R25: with the kill switch on, only orders that
    # strictly reduce the position pass (settled below, once the quantity is known)
    kill_switch = await is_kill_switch_on(session)
    halt = await covering_halt(session, portfolio.id)
    if kill_switch or halt is not None:
        held = await _held(session, portfolio.id, instrument.symbol)
        if held == 0 or (held > 0) == (side == OrderSide.buy):
            raise _kill_switch() if kill_switch else _halted(halt)
    if portfolio.status == PortfolioStatus.archived:
        raise _Reject(ErrorCode.PORTFOLIO_ARCHIVED, ARCHIVED_MESSAGE)
    if portfolio.status == PortfolioStatus.locked:
        raise _Reject(ErrorCode.PORTFOLIO_LOCKED, "portfolio is locked")
    profile = await effective_risk_profile(session, portfolio)
    _check_whitelist(profile, instrument)

    # 4-5: order shape, then the price estimate
    _check_order_shape(instrument, order_type, qty, notional, limit_price, stop_price)
    price, quote = await _estimate_price(
        session, qs, portfolio=portfolio, instrument=instrument, side=side,
        order_type=order_type, limit_price=limit_price, stop_price=stop_price,
        check_quote_age=check_quote_age,
    )
    if qty is None:
        qty = notional_to_qty(notional, price, instrument.lot_size)

    ccy, base = instrument.currency, portfolio.base_currency
    valuation = await value_portfolio(session, qs, portfolio.id, redis=redis)
    before = next(
        (p.qty for p in valuation.positions if p.symbol == instrument.symbol), Decimal(0)
    )
    with localcontext() as ctx:
        ctx.prec = _PREC
        if reduce_only:  # clamped to the position at fill (Ruling R22)
            qty = min(qty, abs(before))
            against = (before > 0) != (side == OrderSide.buy)
            if before == 0 or not against:
                qty = Decimal(0)
        after = before + qty if side == OrderSide.buy else before - qty
        increases = abs(after) > abs(before)
        reduces = abs(after) < abs(before) and _sign(after) in (0, _sign(before))
    if kill_switch and not reduces:
        raise _kill_switch()
    if halt is not None and not reduces:
        raise _halted(halt)
    if valuation.stale_symbols and not reduces:
        raise _Reject(
            ErrorCode.PROVIDER_UNAVAILABLE,
            "valuation uses cached quotes for " + ", ".join(valuation.stale_symbols),
        )

    if qty > 0 and adds_to(before, side == OrderSide.buy, qty):  # a flip included
        closing = await open_closure(session, portfolio.id, instrument.symbol)
        if closing is not None:
            e = plan_closing_error(instrument.symbol)
            raise _Reject(e.code, e.message, e.details)

    # 6-7: reduce-only and short permission
    if portfolio.status == PortfolioStatus.reduce_only and not reduces:
        raise _Reject(ErrorCode.REDUCE_ONLY, "portfolio is reduce-only")
    if side == OrderSide.sell and after < 0:
        if profile is not None and not profile.short_allowed:
            raise _Reject(ErrorCode.SHORT_NOT_ALLOWED, "short selling is disabled by the profile")
        if not instrument.shortable:
            raise _Reject(ErrorCode.SHORT_NOT_ALLOWED, f"{instrument.symbol} is not shortable")

    live_link = await live_link_of(session, portfolio)

    # 8a: order notional limit, before the fee and fx lookups
    to_eur = await qs.get_fx_rate(ccy, EUR)
    with localcontext() as ctx:
        ctx.prec = _PREC
        order_notional = quantize_money(qty * price, ccy)
        notional_eur = order_notional * to_eur
    max_order = profile.max_order_notional_eur if profile is not None else None
    if max_order is not None and notional_eur > max_order:
        raise _Reject(
            ErrorCode.RISK_LIMIT_EXCEEDED,
            f"order notional exceeds {decimal_to_str(max_order)} EUR",
        )

    # estimates: trade fee, fx fee
    rule = resolve_rule(
        await load_rules(session), portfolio_id=portfolio.id,
        asset_class=instrument.asset_class, side=str(side),
    )
    to_rule_ccy = await qs.get_fx_rate(ccy, rule.currency)
    rule_to_eur = await qs.get_fx_rate(rule.currency, EUR)
    with localcontext() as ctx:
        ctx.prec = _PREC
        fee = compute_trade_fee(rule, order_notional * to_rule_ccy)
        fee_eur = fee * rule_to_eur
    fx_fee = Decimal(0)
    if side == OrderSide.buy:  # the formula the live cash cap uses too
        fx_fee = await fx_fee_eur(qs, rule, base=base, ccy=ccy, notional=order_notional,
                                  fee=fee, cash_in_ccy=valuation.cash.get(ccy, Decimal(0)))
    with localcontext() as ctx:
        ctx.prec = _PREC
        costs = fee_eur + fx_fee
        bp_after = valuation.buying_power_raw - costs
        if increases:
            bp_after -= notional_eur

    if live_link is not None:  # real money: before the position and buying power limits
        est_fees = await to_currency(qs, fee, rule.currency, base) + await to_currency(
            qs, fx_fee, EUR, base)
        await live_pre_trade(
            session, qs, portfolio=portfolio, link=live_link, instrument=instrument,
            side=side, qty=qty, notional=notional,
            est_value=await to_currency(qs, order_notional, ccy, base), est_fees=est_fees,
            reduce_only=reduce_only, live_flatten=live_flatten,
            now=now or datetime.now(UTC), trip=live_effects, exclude_order=exclude_order,
        )

    # 8b: position limit
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

    # 10: day trades: the portfolio's own cap and the broker's buying power. Only buys
    # flagged day_trade of a portfolio in day_scope; sells are never refused
    if side == OrderSide.buy and day_trade and day_scope(portfolio):
        at = now or datetime.now(UTC)
        await _check_own_day_trade_cap(session, portfolio, instrument, at)
        link = (await session.get(BrokerLink, portfolio.broker_link_id)
                if portfolio.broker_link_id else None)
        _check_day_trade_leverage(portfolio, link, valuation, notional_eur, costs, increases)
        await _check_broker_buying_power(qs, portfolio, link, instrument, order_notional,
                                         increases, at)

    # 11: symbol owned by an automatic rule. Only with the portfolio's switch on (no query
    # otherwise); orders that close are never refused; the rule's own orders pass
    if portfolio.auto_rules_enabled and not (reduce_only or reduces):
        owner = await owner_rule(session, portfolio.id, instrument.symbol)
        if owner is not None and owner != auto_rule_id:
            raise _Reject(ErrorCode.RISK_LIMIT_EXCEEDED, OWNED_MESSAGE.format(
                symbol=instrument.symbol), {"reason": "auto_rule_owner",
                                            "rule_id": str(owner)})
        if owner is None and auto_rule_id is not None:
            raise _Reject(ErrorCode.RISK_LIMIT_EXCEEDED, INACTIVE_MESSAGE.format(
                symbol=instrument.symbol), {"reason": "auto_rule_inactive",
                                            "rule_id": str(auto_rule_id)})

    return CheckResult(
        ok=True, estimated_price=price, estimated_notional_eur=notional_eur,
        estimated_fee=FeeQuote(rule.id, fee, rule.currency),
        estimated_fx_fee_eur=fx_fee, buying_power_after_eur=bp_after,
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
    check_quote_age: bool = True,
    reduce_only: bool = False,
    redis=None,
    live_flatten: bool = False,
    now: datetime | None = None,
    live_effects: bool = True,
    exclude_order=None,
    day_trade: bool = False,
    auto_rule_id=None,
) -> CheckResult:
    try:
        return await _run(
            session, qs, portfolio=portfolio, instrument=instrument, side=side, qty=qty,
            notional=notional, order_type=order_type, limit_price=limit_price,
            stop_price=stop_price, check_quote_age=check_quote_age,
            reduce_only=reduce_only, redis=redis, live_flatten=live_flatten, now=now,
            live_effects=live_effects, exclude_order=exclude_order, day_trade=day_trade,
            auto_rule_id=auto_rule_id,
        )
    except _Reject as r:
        return CheckResult(ok=False, code=r.code, message=r.message, details=r.details)
    except SMTError as e:
        return CheckResult(ok=False, code=e.code, message=e.message)
