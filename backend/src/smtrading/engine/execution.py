"""execute_fill: the single place where an order moves money.

Lock order: portfolio row (FOR UPDATE) -> order row (FOR UPDATE) -> chain lock (inside
post_journal). Nothing here commits; on SMTError the caller rolls the transaction back.

Covered so far: buys on a flat/long position and sells that reduce a long (not past zero).
"""

import uuid
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import ROUND_DOWN, ROUND_UP, Decimal, localcontext
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from smtrading.common.errors import ErrorCode, SMTError
from smtrading.common.money import decimal_to_str, quantize_money
from smtrading.engine.models import Fill, Order, OrderSide, OrderStatus
from smtrading.engine.pricing import QuoteView, fill_price, notional_to_qty
from smtrading.engine.quotesource import QuoteSource
from smtrading.events import publish_event
from smtrading.fees.calculator import FeeRuleData, compute_fx_fee, compute_trade_fee
from smtrading.fees.resolver import load_rules, resolve_rule
from smtrading.instruments.models import Instrument
from smtrading.ledger.models import AccountKind, Journal, JournalType
from smtrading.ledger.service import LineSpec, get_balance, get_or_create_account, post_journal
from smtrading.marketdata.streaming import track, untrack
from smtrading.marketdata.types import Quote
from smtrading.portfolios.models import Portfolio
from smtrading.positions.service import upsert_position
from smtrading.risk.settings import get_default_spread_bps
from smtrading.valuation.service import value_portfolio

_PREC = 60
FILLABLE = frozenset(
    {OrderStatus.new, OrderStatus.queued, OrderStatus.pending, OrderStatus.triggered}
)
_OPEN = FILLABLE | {OrderStatus.held}


@dataclass
class FillResult:
    order: Order
    fill: Fill
    journals: list[Journal]


def _no_buying_power(message: str, **details) -> SMTError:
    return SMTError(
        ErrorCode.INSUFFICIENT_BUYING_POWER, message,
        {k: decimal_to_str(v) if isinstance(v, Decimal) else v for k, v in details.items()},
    )


def _money_step(ccy: str) -> Decimal:
    return Decimal(1).scaleb(quantize_money(Decimal(0), ccy).as_tuple().exponent)


class _Accounts:
    """Per-call cache of accounts and their balances."""

    def __init__(self, session: AsyncSession, portfolio_id: UUID) -> None:
        self._s = session
        self._pid = portfolio_id
        self._ids: dict[tuple[bool, str, AccountKind], UUID] = {}

    async def id(self, asset: str, kind: AccountKind, *, system: bool = False) -> UUID:
        key = (system, asset, kind)
        if key not in self._ids:
            acct = await get_or_create_account(
                self._s, portfolio_id=None if system else self._pid, asset=asset, kind=kind
            )
            self._ids[key] = acct.id
        return self._ids[key]

    async def balance(self, asset: str, kind: AccountKind) -> Decimal:
        return await get_balance(self._s, await self.id(asset, kind))


@dataclass
class _FxLeg:
    ccy: str
    bought: Decimal  # b, in ccy
    paid: Decimal  # a, in base
    fee: Decimal  # in base


def _fx_cost(rule: FeeRuleData, b: Decimal, rate: Decimal, base: str) -> tuple[Decimal, Decimal]:
    with localcontext() as ctx:
        ctx.prec = _PREC
        a = quantize_money(b / rate, base, rounding=ROUND_UP)
    return a, compute_fx_fee(rule, a, base)


def _max_affordable(
    rule: FeeRuleData, available: Decimal, rate: Decimal, base: str, ccy: str
) -> Decimal:
    """Largest b (in ccy) whose cost a + fx fee fits in `available` base cash."""
    if available <= 0:
        return Decimal(0)
    with localcontext() as ctx:
        ctx.prec = _PREC
        b = quantize_money(
            available / (1 + rule.fx_pct / 100) * rate, ccy, rounding=ROUND_DOWN
        )
        step = _money_step(ccy)
        while b > 0 and sum(_fx_cost(rule, b, rate, base)) > available:
            b -= step
    return max(b, Decimal(0))


async def _plan_funding(
    qs: QuoteSource,
    accts: _Accounts,
    *,
    rule: FeeRuleData,
    base: str,
    leverage: Decimal,
    needs: dict[str, Decimal],
    incoming: dict[str, Decimal],
) -> tuple[list[_FxLeg], dict[str, Decimal]]:
    """Cover `needs` from cash, then FX from base, then a margin loan when leverage > 1."""
    legs: list[_FxLeg] = []
    borrow: dict[str, Decimal] = {}
    with localcontext() as ctx:
        ctx.prec = _PREC
        base_avail = (
            await accts.balance(base, AccountKind.cash)
            + incoming.get(base, Decimal(0)) - needs.get(base, Decimal(0))
        )
        for ccy in sorted(c for c in needs if c != base):
            have = await accts.balance(ccy, AccountKind.cash) + incoming.get(ccy, Decimal(0))
            short = needs[ccy] - have
            if short <= 0:
                continue
            rate = await qs.get_fx_rate(base, ccy)
            a, fee = _fx_cost(rule, short, rate, base)
            b = short
            if a + fee > base_avail:
                if leverage <= 1:
                    raise _no_buying_power(
                        "insufficient cash", currency=ccy, shortfall=short
                    )
                b = _max_affordable(rule, base_avail, rate, base, ccy)
                a, fee = _fx_cost(rule, b, rate, base) if b > 0 else (Decimal(0), Decimal(0))
                borrow[ccy] = short - b
            if b > 0:
                legs.append(_FxLeg(ccy=ccy, bought=b, paid=a, fee=fee))
                base_avail -= a + fee
        if base_avail < 0:
            if leverage <= 1:
                raise _no_buying_power(
                    "insufficient cash", currency=base, shortfall=base_avail.copy_negate()
                )
            borrow[base] = base_avail.copy_negate()
    return legs, borrow


async def _fx_lines(accts: _Accounts, base: str, legs: list[_FxLeg]) -> list[LineSpec]:
    lines: list[LineSpec] = []
    for leg in legs:
        lines += [
            LineSpec(await accts.id(base, AccountKind.cash), leg.paid.copy_negate()),
            LineSpec(await accts.id(base, AccountKind.system_fx, system=True), leg.paid),
            LineSpec(
                await accts.id(leg.ccy, AccountKind.system_fx, system=True),
                leg.bought.copy_negate(),
            ),
            LineSpec(await accts.id(leg.ccy, AccountKind.cash), leg.bought),
        ]
        if leg.fee > 0:
            lines += [
                LineSpec(await accts.id(base, AccountKind.cash), leg.fee.copy_negate()),
                LineSpec(await accts.id(base, AccountKind.system_fees, system=True), leg.fee),
            ]
    return lines


async def _activate_linked(session: AsyncSession, order: Order) -> None:
    """Cancel open OCO siblings; release held bracket children as one OCO group."""
    now = datetime.now(UTC)
    if order.oco_group_id is not None:
        await session.execute(
            update(Order)
            .where(
                Order.oco_group_id == order.oco_group_id,
                Order.id != order.id,
                Order.status.in_(_OPEN),
                (Order.parent_id.is_(None)) | (Order.parent_id != order.id),
            )
            .values(status=OrderStatus.canceled, updated_at=now)
            .execution_options(synchronize_session=False)
        )
    children = list(
        await session.scalars(
            select(Order)
            .where(Order.parent_id == order.id, Order.status == OrderStatus.held)
            .order_by(Order.created_at, Order.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    )
    if children:
        group = next(
            (c.oco_group_id for c in children if c.oco_group_id is not None), uuid.uuid4()
        )
        for child in children:
            child.status = OrderStatus.pending
            child.oco_group_id = group
            child.updated_at = now


async def execute_fill(
    session: AsyncSession,
    qs: QuoteSource,
    order_id: UUID,
    *,
    quote: Quote,
    redis=None,
    journal_type: JournalType = JournalType.TRADE,
) -> FillResult | None:
    portfolio_id = await session.scalar(select(Order.portfolio_id).where(Order.id == order_id))
    if portfolio_id is None:
        raise SMTError(ErrorCode.NOT_FOUND, "order not found", {"order_id": str(order_id)})
    portfolio = (
        await session.execute(
            select(Portfolio).where(Portfolio.id == portfolio_id).with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one()
    order = (
        await session.execute(
            select(Order).where(Order.id == order_id).with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one()
    if order.status not in FILLABLE:
        return None

    instrument = await session.get(Instrument, order.symbol)
    if instrument is None:
        raise SMTError(ErrorCode.SYMBOL_NOT_FOUND, "instrument not found", {"symbol": order.symbol})
    ccy, base, side, symbol = instrument.currency, portfolio.base_currency, order.side, order.symbol

    spread = await get_default_spread_bps(session, instrument.asset_class)
    price = fill_price(
        order.type, side, QuoteView(quote.bid, quote.ask, quote.last, quote.ts),
        limit_price=order.limit_price, slippage_bps=portfolio.slippage_bps,
        default_spread_bps=spread, tick=instrument.tick_size,
    )
    qty = order.qty if order.qty is not None else notional_to_qty(
        order.notional, price, instrument.lot_size
    )

    rule = resolve_rule(
        await load_rules(session), portfolio_id=portfolio.id,
        asset_class=instrument.asset_class, side=str(side),
    )
    to_rule_ccy = await qs.get_fx_rate(ccy, rule.currency)
    with localcontext() as ctx:
        ctx.prec = _PREC
        notional = quantize_money(qty * price, ccy)
        fee = compute_trade_fee(rule, notional * to_rule_ccy)
    fee_ccy = rule.currency

    accts = _Accounts(session, portfolio.id)
    held = await accts.balance(symbol, AccountKind.position)
    if side == OrderSide.buy and held < 0:
        raise SMTError(ErrorCode.INVALID_ORDER, "not implemented: buy on a short position")
    if side == OrderSide.sell and qty > held:
        raise SMTError(
            ErrorCode.SHORT_NOT_ALLOWED, "sell exceeds the long position",
            {"symbol": symbol, "held": decimal_to_str(held), "qty": decimal_to_str(qty)},
        )

    needs: dict[str, Decimal] = defaultdict(Decimal)
    incoming: dict[str, Decimal] = defaultdict(Decimal)
    with localcontext() as ctx:
        ctx.prec = _PREC
        if side == OrderSide.buy:
            needs[ccy] += notional
        else:
            incoming[ccy] += notional
        needs[fee_ccy] += fee
    legs, borrow = await _plan_funding(
        qs, accts, rule=rule, base=base, leverage=portfolio.leverage,
        needs=dict(needs), incoming=dict(incoming),
    )

    fill_id = uuid.uuid4()
    journals: list[Journal] = []
    post = {
        "author_type": "system", "author_id": order.agent_id,
        "order_id": order.id, "fill_id": fill_id,
    }
    if legs:
        journals.append(await post_journal(
            session, type=JournalType.FX, lines=await _fx_lines(accts, base, legs), **post,
        ))

    buy = side == OrderSide.buy
    qty_delta = qty if buy else qty.copy_negate()  # into the position
    cash_delta = notional.copy_negate() if buy else notional  # into cash[ccy]
    lines = [
        LineSpec(await accts.id(symbol, AccountKind.position), qty_delta, price, ccy),
        LineSpec(
            await accts.id(symbol, AccountKind.system_equity, system=True),
            qty_delta.copy_negate(),
        ),
        LineSpec(await accts.id(ccy, AccountKind.cash), cash_delta),
        LineSpec(
            await accts.id(ccy, AccountKind.system_equity, system=True), cash_delta.copy_negate()
        ),
    ]
    if fee > 0:
        lines += [
            LineSpec(await accts.id(fee_ccy, AccountKind.cash), fee.copy_negate()),
            LineSpec(await accts.id(fee_ccy, AccountKind.system_fees, system=True), fee),
        ]
    for loan_ccy, amount in sorted(borrow.items()):
        amount = quantize_money(amount, loan_ccy, rounding=ROUND_UP)
        lines += [
            LineSpec(await accts.id(loan_ccy, AccountKind.margin_loan), amount.copy_negate()),
            LineSpec(await accts.id(loan_ccy, AccountKind.cash), amount),
        ]
    if side == OrderSide.sell:
        debt = await accts.balance(ccy, AccountKind.margin_loan)
        with localcontext() as ctx:
            ctx.prec = _PREC
            net = notional - fee if fee_ccy == ccy else notional
            repay = min(net, debt.copy_negate()) if debt < 0 else Decimal(0)
        if repay > 0:
            lines += [
                LineSpec(await accts.id(ccy, AccountKind.cash), repay.copy_negate()),
                LineSpec(await accts.id(ccy, AccountKind.margin_loan), repay),
            ]
    trade = await post_journal(session, type=journal_type, lines=lines, **post)
    journals.append(trade)

    position = await upsert_position(session, portfolio.id, symbol, side, qty, price)

    if buy and portfolio.leverage > 1:
        valuation = await value_portfolio(session, qs, portfolio.id)
        if valuation.buying_power_eur < 0:
            raise _no_buying_power(
                "buying power exceeded", buying_power_eur=valuation.buying_power_eur
            )

    fill = Fill(
        id=fill_id, order_id=order.id, portfolio_id=portfolio.id, symbol=symbol, side=side,
        qty=qty, price=price, quote_bid=quote.bid, quote_ask=quote.ask, quote_last=quote.last,
        quote_ts=quote.ts, fee_amount=fee, fee_currency=fee_ccy, fee_rule_id=rule.id,
        journal_id=trade.id,
    )
    session.add(fill)
    order.status = OrderStatus.filled
    order.filled_qty = qty
    order.avg_fill_price = price
    order.updated_at = datetime.now(UTC)
    await _activate_linked(session, order)
    await session.flush()

    if redis is not None:
        await publish_event(redis, "order.filled", {
            "order_id": order.id, "fill_id": fill.id, "portfolio_id": portfolio.id,
            "agent_id": order.agent_id, "symbol": symbol, "side": side, "qty": qty,
            "price": price, "notional": notional, "currency": ccy, "fee": fee,
            "fee_currency": fee_ccy, "position_qty": position.qty,
        })
        if held == 0 and position.qty != 0:
            await track(redis, [symbol], source="positions")
        elif held != 0 and position.qty == 0:
            await untrack(redis, [symbol], source="positions")
    return FillResult(order=order, fill=fill, journals=journals)
