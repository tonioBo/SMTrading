"""execute_fill: the single place where an order moves money.

Lock order: portfolio row (FOR UPDATE) -> order row (FOR UPDATE) -> chain lock (inside
post_journal). Nothing here commits; on SMTError the caller rolls the transaction back.

Redis side effects (the `order.filled` event, position tracking) are only queued on the
session; callers must `await session.commit()` and then
`await events.dispatch_post_commit(session, redis)` (or `discard_post_commit` on rollback).

A fill is split into the part that reduces the current position (close) and the part
that opens/adds (open), posted together in one TRADE journal:
- long proceeds go to cash[ccy] (repaying a margin loan in ccy); short proceeds go to
  collateral[ccy] and the position account goes negative;
- a cover pays from collateral[ccy] first, then cash[ccy], then FX from base / a loan;
  when no short in ccy is left, the remaining collateral is released to cash.
  Collateral is pooled per currency: what one short draws is not tied to its own
  proceeds, so a cover at a loss may use collateral posted by another short in ccy;
- a fill that opens exposure must leave buying_power_raw >= 0 (Ruling R8), measured on
  a valuation without cached (stale) quotes, else PROVIDER_UNAVAILABLE (retryable);
- status at fill (I3, not for LIQUIDATION journals): a locked portfolio fills nothing
  (PORTFOLIO_LOCKED); a reduce_only portfolio only fills purely reducing fills
  (REDUCE_ONLY); with the kill switch on, a fill that opens exposure is refused
  (KILL_SWITCH_ACTIVE), and so it is while a trading halt covers the portfolio
  (TRADING_HALTED, risk.halts); a fill that would increase a position of an ended
  «piano» still being closed, or turn it to the other side, is refused (RISK_LIMIT_EXCEEDED,
  details reason `plan_grace`, goals.plans): a resting entry placed before the end never
  adds to it;
- a reduce_only order (Ruling R22: closes, bracket exits) is clamped to |position|; with
  nothing to reduce (flat, or held on the order's side) it is canceled with reason
  `nothing_to_reduce` and no journal;
- no network I/O under the chain lock (I2): every quote/fx rate is read through a
  MemoQuoteSource, prefetched before the first post_journal and frozen from then on;
- broker fills (`source="broker"`, Rulings R2 and R7): quantity and price come from the
  broker (no slippage, no quote); fee, FX and funding work as usual. They record what
  already happened at the broker, so the status/kill switch/short/buying power checks
  are skipped and a shortfall becomes a margin loan instead of a refusal. A reduce_only
  clamp still applies to the journal; the order's progress follows the broker;
- partial fills (fill_qty given): the order is `partially_filled` until its filled
  quantity reaches qty (or `final`); avg_fill_price is the weighted average; OCO
  siblings and bracket children are only activated once it is `filled`;
- a purely reducing fill is never blocked:
  - R9: when cash[fee_ccy] cannot pay the whole fee, the whole fee is converted into ccy
    (ROUND_UP) and charged there, out of proceeds/collateral;
  - R12: it may borrow even at leverage 1, but first converts from base and then from
    ANY other cash currency (usual FX journal and fx fee).
"""

import uuid
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import ROUND_DOWN, ROUND_UP, Decimal, localcontext
from typing import Literal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from smtrading.common.errors import ErrorCode, SMTError
from smtrading.common.money import decimal_to_str, quantize_money
from smtrading.engine.models import (
    FILLABLE_STATUSES,
    OPEN_STATUSES,
    BrokerState,
    Fill,
    Order,
    OrderSide,
    OrderStatus,
    OrderType,
)
from smtrading.engine.prefetch import prefetch_portfolio
from smtrading.engine.pricing import QuoteView, notional_to_qty
from smtrading.engine.pricing import fill_price as fill_price_of
from smtrading.engine.quotesource import MemoQuoteSource, QuoteSource
from smtrading.events import queue_event, queue_track, queue_untrack
from smtrading.fees.calculator import FeeRuleData, compute_fx_fee, compute_trade_fee
from smtrading.fees.resolver import load_rules, resolve_rule
from smtrading.goals.plans import adds_to, open_closure, plan_grace_error
from smtrading.instruments.models import Instrument
from smtrading.ledger.models import Account, AccountBalance, AccountKind, Journal, JournalType
from smtrading.ledger.service import LineSpec, get_balance, get_or_create_account, post_journal
from smtrading.marketdata.types import Quote
from smtrading.portfolios.locks import lock_portfolio
from smtrading.portfolios.models import ARCHIVED_MESSAGE, Portfolio, PortfolioStatus
from smtrading.portfolios.service import get_cash_balances
from smtrading.positions.service import upsert_position
from smtrading.risk.halts import covering_halt, halt_message
from smtrading.risk.profiles import effective_risk_profile
from smtrading.risk.settings import get_default_spread_bps, is_kill_switch_on
from smtrading.valuation.service import value_portfolio

_PREC = 60
NOTHING_TO_REDUCE = "nothing_to_reduce"


@dataclass
class FillResult:
    order: Order
    fill: Fill
    journals: list[Journal]


def broker_live(order: Order) -> bool:
    """The order exists (or may exist) at the broker: canceling it needs the broker."""
    return order.broker_link_id is not None and (
        order.broker_status not in (None, BrokerState.TO_SEND)
        or order.broker_sent_at is not None
    )


async def _held(session: AsyncSession, portfolio_id: UUID, symbol: str) -> Decimal:
    return await session.scalar(
        select(AccountBalance.balance)
        .join(Account, AccountBalance.account_id == Account.id)
        .where(Account.portfolio_id == portfolio_id, Account.asset == symbol,
               Account.kind == AccountKind.position)
    ) or Decimal(0)


async def _check_plan(session: AsyncSession, portfolio_id: UUID, symbol: str) -> None:
    """A position of an ended piano still being closed takes no addition (goals.plans)."""
    closing = await open_closure(session, portfolio_id, symbol)
    if closing is not None:
        raise plan_grace_error(closing, symbol)


async def plan_refusal(session: AsyncSession, order: Order) -> SMTError | None:
    """The plan_grace refusal of an open order whose rest would add to (or turn) a
    position of an ended piano still being closed; None for any other order (reduce_only
    ones included: they never add)."""
    if order.reduce_only:
        return None
    held = await _held(session, order.portfolio_id, order.symbol)
    remaining = None if order.qty is None else order.qty - (order.filled_qty or Decimal(0))
    if remaining is not None and remaining <= 0:
        return None
    if not adds_to(held, order.side == OrderSide.buy, remaining):
        return None
    closing = await open_closure(session, order.portfolio_id, order.symbol)
    return None if closing is None else plan_grace_error(closing, order.symbol)


async def broker_send_check(
    session: AsyncSession, portfolio: Portfolio, order: Order, *, clamp: bool = True,
    status_checks: bool = True,
) -> bool:
    """The fill-time local checks for an order about to be sent to the broker (the same
    the simulator applies at fill, I3): raises PORTFOLIO_LOCKED / REDUCE_ONLY /
    KILL_SWITCH_ACTIVE / TRADING_HALTED, and RISK_LIMIT_EXCEEDED (plan_grace) for an order
    that would add to a position of an ended piano; for a reduce_only order clamps qty to
    the position and returns False when there is nothing to reduce (`clamp=False`: an
    order already at the broker, qty left as is). The caller holds the portfolio lock."""
    held = await _held(session, portfolio.id, order.symbol)
    buy = order.side == OrderSide.buy
    closing = held != 0 and (held > 0) != buy
    remaining = None if order.qty is None else order.qty - (order.filled_qty or Decimal(0))
    if order.reduce_only:
        if not closing:
            return False
        if clamp and remaining is not None and remaining > abs(held):
            order.qty = (order.filled_qty or Decimal(0)) + abs(held)
            remaining = abs(held)
    reducing = closing and (
        order.reduce_only if remaining is None else remaining <= abs(held)
    )
    if status_checks:  # skipped for a margin liquidation (always goes)
        await _check_status(session, portfolio, reducing, order.symbol)
        if not reducing:
            await _check_plan(session, portfolio.id, order.symbol)
    return True


async def hand_to_broker(session: AsyncSession, order: Order, link_id: UUID) -> bool:
    """Ruling R10 (a) / R11: a simulated trigger on a broker-linked portfolio (trailing
    stop) becomes a market order sent by BrokerSync, keeping its own reduce_only flag;
    nothing fills locally. The fill-time checks run first (broker_send_check): an error
    is raised for the caller to reject the order; False when a reduce_only order had
    nothing to reduce (canceled here)."""
    portfolio = await session.get(Portfolio, order.portfolio_id)
    if not await broker_send_check(session, portfolio, order):
        _cancel_nothing_to_reduce(session, order)
        await session.flush()
        return False
    order.type = OrderType.market
    order.trail_pct = order.trail_amount = None  # trail_ref_price records the trigger
    order.status = OrderStatus.pending
    order.broker_link_id = link_id
    order.broker_status = BrokerState.TO_SEND
    order.updated_at = datetime.now(UTC)
    return True


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
    src: str  # currency sold
    ccy: str  # currency bought
    bought: Decimal  # b, in ccy
    paid: Decimal  # a, in src
    fee: Decimal  # in src


def _fx_cost(rule: FeeRuleData, b: Decimal, rate: Decimal, src: str) -> tuple[Decimal, Decimal]:
    with localcontext() as ctx:
        ctx.prec = _PREC
        a = quantize_money(b / rate, src, rounding=ROUND_UP)
    return a, compute_fx_fee(rule, a, src)


def _max_affordable(
    rule: FeeRuleData, available: Decimal, rate: Decimal, src: str, ccy: str
) -> Decimal:
    """Largest b (in ccy) whose cost a + fx fee fits in `available` src cash."""
    if available <= 0:
        return Decimal(0)
    with localcontext() as ctx:
        ctx.prec = _PREC
        b = quantize_money(
            available / (1 + rule.fx_pct / 100) * rate, ccy, rounding=ROUND_DOWN
        )
        step = _money_step(ccy)
        while b > 0 and sum(_fx_cost(rule, b, rate, src)) > available:
            b -= step
    return max(b, Decimal(0))


async def _plan_funding(
    qs: QuoteSource,
    accts: _Accounts,
    *,
    rule: FeeRuleData,
    base: str,
    may_borrow: bool,
    other_sources: list[str],
    needs: dict[str, Decimal],
    incoming: dict[str, Decimal],
) -> tuple[list[_FxLeg], dict[str, Decimal]]:
    """Cover `needs` from cash, then FX from base, then FX from `other_sources` (in order),
    then a margin loan when `may_borrow`. Shortfalls in non-base currencies come first."""
    legs: list[_FxLeg] = []
    borrow: dict[str, Decimal] = {}
    with localcontext() as ctx:
        ctx.prec = _PREC
        avail: dict[str, Decimal] = {}
        for c in sorted({base, *needs, *incoming, *other_sources}):
            avail[c] = (
                await accts.balance(c, AccountKind.cash)
                + incoming.get(c, Decimal(0)) - needs.get(c, Decimal(0))
            )
        targets = sorted(c for c in needs if c != base and avail[c] < 0)
        if avail[base] < 0:
            targets.append(base)
        for ccy in targets:
            short = avail[ccy].copy_negate()
            sources = [c for c in [base, *other_sources] if c != ccy]
            for src in sources:
                if short <= 0 or avail[src] <= 0:
                    continue
                rate = await qs.get_fx_rate(src, ccy)
                b = short
                a, fee = _fx_cost(rule, b, rate, src)
                if a + fee > avail[src]:
                    if not may_borrow:
                        break  # fail below with the full shortfall
                    b = _max_affordable(rule, avail[src], rate, src, ccy)
                    if b <= 0:
                        continue
                    a, fee = _fx_cost(rule, b, rate, src)
                legs.append(_FxLeg(src=src, ccy=ccy, bought=b, paid=a, fee=fee))
                avail[src] -= a + fee
                short -= b
            if short > 0:
                if not may_borrow:
                    raise _no_buying_power("insufficient cash", currency=ccy, shortfall=short)
                borrow[ccy] = short
            avail[ccy] = Decimal(0)
    return legs, borrow


async def _fx_lines(accts: _Accounts, legs: list[_FxLeg]) -> list[LineSpec]:
    lines: list[LineSpec] = []
    for leg in legs:
        lines += [
            LineSpec(await accts.id(leg.src, AccountKind.cash), leg.paid.copy_negate()),
            LineSpec(await accts.id(leg.src, AccountKind.system_fx, system=True), leg.paid),
            LineSpec(
                await accts.id(leg.ccy, AccountKind.system_fx, system=True),
                leg.bought.copy_negate(),
            ),
            LineSpec(await accts.id(leg.ccy, AccountKind.cash), leg.bought),
        ]
        if leg.fee > 0:
            lines += [
                LineSpec(await accts.id(leg.src, AccountKind.cash), leg.fee.copy_negate()),
                LineSpec(
                    await accts.id(leg.src, AccountKind.system_fees, system=True), leg.fee
                ),
            ]
    return lines


async def _check_short_allowed(
    session: AsyncSession, portfolio: Portfolio, instrument: Instrument
) -> None:
    profile = await effective_risk_profile(session, portfolio)
    if profile is not None and not profile.short_allowed:
        raise SMTError(
            ErrorCode.SHORT_NOT_ALLOWED, "short selling is disabled by the profile",
            {"symbol": instrument.symbol},
        )
    if not instrument.shortable:
        raise SMTError(
            ErrorCode.SHORT_NOT_ALLOWED, f"{instrument.symbol} is not shortable",
            {"symbol": instrument.symbol},
        )


@dataclass(frozen=True)
class _Split:
    after: Decimal  # position after the fill
    close_qty: Decimal  # part that reduces the current position
    open_qty: Decimal  # part that opens or adds
    close_notional: Decimal
    open_notional: Decimal

    @property
    def reducing(self) -> bool:
        return self.open_qty == 0


def _split_fill(
    held: Decimal, buy: bool, qty: Decimal, price: Decimal, notional: Decimal, ccy: str
) -> _Split:
    with localcontext() as ctx:
        ctx.prec = _PREC
        after = held + qty if buy else held - qty
        close_qty = min(qty, abs(held)) if held != 0 and (held > 0) != buy else Decimal(0)
        open_qty = qty - close_qty
        close_notional = notional if open_qty == 0 else quantize_money(close_qty * price, ccy)
        return _Split(after, close_qty, open_qty, close_notional, notional - close_notional)


async def _plan_release(
    session: AsyncSession, accts: _Accounts, portfolio_id: UUID, *, symbol: str, ccy: str,
    split: _Split, ccy_charge: Decimal,
) -> Decimal:
    """Collateral[ccy] released to cash for a cover: what the cover (and its fee in ccy)
    costs, or all of it once no short in `ccy` is left."""
    collateral = await accts.balance(ccy, AccountKind.collateral)
    if split.after >= 0 and not await _has_other_short(session, portfolio_id, ccy, symbol):
        return collateral
    with localcontext() as ctx:
        ctx.prec = _PREC
        return min(collateral, split.close_notional + ccy_charge)


async def _has_other_short(
    session: AsyncSession, portfolio_id: UUID, ccy: str, symbol: str
) -> bool:
    """True if the portfolio is short another instrument quoted in `ccy`."""
    found = await session.scalar(
        select(Account.id)
        .join(AccountBalance, AccountBalance.account_id == Account.id)
        .join(Instrument, Instrument.symbol == Account.asset)
        .where(
            Account.portfolio_id == portfolio_id, Account.kind == AccountKind.position,
            Account.asset != symbol, Instrument.currency == ccy, AccountBalance.balance < 0,
        )
        .limit(1)
    )
    return found is not None


async def activate_linked(session: AsyncSession, order: Order) -> None:
    """Cancel open OCO siblings; release held bracket children as one OCO group."""
    now = datetime.now(UTC)
    if order.oco_group_id is not None:
        siblings = list(await session.scalars(
            select(Order)
            .where(
                Order.oco_group_id == order.oco_group_id,
                Order.id != order.id,
                Order.status.in_(OPEN_STATUSES),
                (Order.parent_id.is_(None)) | (Order.parent_id != order.id),
            )
            .order_by(Order.created_at, Order.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        ))
        for sibling in siblings:
            sibling.updated_at = now
            if broker_live(sibling):  # canceled once the broker confirms (BrokerSync)
                sibling.broker_status = BrokerState.CANCEL_REQUESTED
                continue
            sibling.status = OrderStatus.canceled
            queue_event(session, "order.canceled", {
                "order_id": sibling.id, "portfolio_id": sibling.portfolio_id,
                "agent_id": sibling.agent_id, "symbol": sibling.symbol, "by": "system",
                "reason": "oco_sibling_filled",
            })
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
            if order.broker_link_id is not None:  # the exits follow the entry to the broker
                child.broker_link_id = order.broker_link_id
                child.broker_status = BrokerState.TO_SEND


def _advance(order: Order, qty: Decimal, price: Decimal, *, final: bool) -> None:
    """Add a fill of `qty` at `price` to the order's progress (weighted average price);
    `filled` once qty is reached or the fill is `final`, else `partially_filled`."""
    with localcontext() as ctx:
        ctx.prec = _PREC
        before = order.filled_qty or Decimal(0)
        total = before + qty
        old_avg = order.avg_fill_price or Decimal(0)
        order.avg_fill_price = price if before == 0 else (
            (old_avg * before + price * qty) / total
        ).quantize(Decimal("1e-18")).normalize()
    order.filled_qty = total
    done = final or (order.qty is not None and total >= order.qty)
    order.status = OrderStatus.filled if done else OrderStatus.partially_filled
    order.updated_at = datetime.now(UTC)


def _cancel_nothing_to_reduce(session: AsyncSession, order: Order) -> None:
    order.status = OrderStatus.canceled
    order.reject_message = NOTHING_TO_REDUCE
    order.updated_at = datetime.now(UTC)
    queue_event(session, "order.canceled", {
        "order_id": order.id, "portfolio_id": order.portfolio_id, "agent_id": order.agent_id,
        "symbol": order.symbol, "by": "system", "reason": NOTHING_TO_REDUCE,
    })


async def _check_status(
    session: AsyncSession, portfolio: Portfolio, reducing: bool, symbol: str
) -> None:
    """I3: portfolio status, kill switch and trading halt, enforced again at fill time."""
    details = {"portfolio_id": str(portfolio.id), "symbol": symbol}
    if portfolio.status == PortfolioStatus.archived:
        raise SMTError(ErrorCode.PORTFOLIO_ARCHIVED, ARCHIVED_MESSAGE, details)
    if portfolio.status == PortfolioStatus.locked:
        raise SMTError(ErrorCode.PORTFOLIO_LOCKED, "portfolio is locked", details)
    if portfolio.status == PortfolioStatus.reduce_only and not reducing:
        raise SMTError(ErrorCode.REDUCE_ONLY, "portfolio is reduce-only", details)
    if not reducing and await is_kill_switch_on(session):
        raise SMTError(
            ErrorCode.KILL_SWITCH_ACTIVE, "trading is halted by the kill switch", details
        )
    if not reducing and (halt := await covering_halt(session, portfolio.id)) is not None:
        raise SMTError(ErrorCode.TRADING_HALTED, halt_message(halt), details)


async def execute_fill(
    session: AsyncSession,
    qs: QuoteSource,
    order_id: UUID,
    *,
    quote: Quote | None = None,
    journal_type: JournalType = JournalType.TRADE,
    redis=None,
    fill_qty: Decimal | None = None,
    fill_price: Decimal | None = None,
    source: Literal["sim", "broker"] = "sim",
    final: bool = False,
    fee_override: Decimal | None = None,
    fee_override_ccy: str | None = None,
) -> FillResult | None:
    """Fill `order_id` at `quote`; None if the order is no longer fillable (or a
    reduce_only order had nothing to reduce: canceled for a simulated fill, progress
    recorded without a journal for a broker fill).

    `fill_qty`/`fill_price` override the quantity (a partial fill when below what is
    left) and the price (no slippage). A broker fill needs both; `final` marks the
    broker's last fill of an order without qty (notional).

    Ruling R12: a purely reducing fill (close/cover) is never blocked for lack of cash. It
    may take a margin loan even at leverage 1, but only after converting from the base
    currency and then from any other cash currency (FX journal with the usual fx fee).

    `fee_override` (a broker fill whose fee the broker reported, plan 8) replaces the fee
    the rules compute; it is in `fee_override_ccy` (default: the rule's currency). The
    Fill keeps no fee rule then.

    `qs` is wrapped in a MemoQuoteSource (shared if it already is one) and frozen before
    the first journal. `redis`, when given, lets the post-fill valuation fall back to
    cached quotes (I4).
    """
    broker = source == "broker"
    if broker and (fill_qty is None or fill_price is None):
        raise ValueError("a broker fill needs fill_qty and fill_price")
    if quote is None and fill_price is None:
        raise ValueError("a simulated fill needs a quote or fill_price")
    qs = MemoQuoteSource.wrap(qs)
    portfolio_id = await session.scalar(select(Order.portfolio_id).where(Order.id == order_id))
    if portfolio_id is None:
        raise SMTError(ErrorCode.NOT_FOUND, "order not found", {"order_id": str(order_id)})
    portfolio = await lock_portfolio(session, portfolio_id)
    order = (
        await session.execute(
            select(Order).where(Order.id == order_id).with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one()
    if order.status not in FILLABLE_STATUSES:
        return None

    instrument = await session.get(Instrument, order.symbol)
    if instrument is None:
        raise SMTError(ErrorCode.SYMBOL_NOT_FOUND, "instrument not found", {"symbol": order.symbol})
    ccy, base, side, symbol = instrument.currency, portfolio.base_currency, order.side, order.symbol

    if fill_price is not None:
        price = fill_price
    else:
        spread = await get_default_spread_bps(session, instrument.asset_class)
        price = fill_price_of(
            order.type, side, QuoteView(quote.bid, quote.ask, quote.last, quote.ts),
            limit_price=order.limit_price, slippage_bps=portfolio.slippage_bps,
            default_spread_bps=spread, tick=instrument.tick_size,
        )
    if fill_qty is not None:
        qty = fill_qty
    elif order.qty is not None:
        qty = order.qty - order.filled_qty
    else:
        qty = notional_to_qty(order.notional, price, instrument.lot_size)
    if qty <= 0:
        raise SMTError(ErrorCode.INVALID_ORDER, "nothing left to fill", {"order_id": str(order.id)})
    progress = qty  # what the order's filled quantity grows by
    buy = side == OrderSide.buy
    accts = _Accounts(session, portfolio.id)
    held = await accts.balance(symbol, AccountKind.position)
    if order.reduce_only:
        if held == 0 or (held > 0) == buy:
            if broker:  # it happened at the broker: follow it, nothing to journal
                _advance(order, progress, price, final=final)
                if order.status == OrderStatus.filled:
                    await activate_linked(session, order)
                await session.flush()
                return None
            _cancel_nothing_to_reduce(session, order)
            await session.flush()
            return None
        qty = min(qty, abs(held))
        if not broker:
            progress = qty

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
    fee_rule_id = rule.id
    if fee_override is not None:  # the broker's actual fee
        fee_ccy, fee_rule_id = (fee_override_ccy or rule.currency).upper(), None
        fee = quantize_money(max(fee_override, Decimal(0)), fee_ccy)

    split = _split_fill(held, buy, qty, price, notional, ccy)
    after, close_notional, open_notional = split.after, split.close_notional, split.open_notional
    reducing = split.reducing
    if journal_type != JournalType.LIQUIDATION and not broker:
        await _check_status(session, portfolio, split.reducing, symbol)
        if not split.reducing:  # adds to the position or turns it (a flip)
            await _check_plan(session, portfolio.id, symbol)
    if after < 0 and not buy and not broker:
        await _check_short_allowed(session, portfolio, instrument)

    # Ruling R9: a reducing fill is never blocked by missing fee-currency cash; the fee is
    # converted into the instrument currency and taken out of proceeds/collateral.
    charge, charge_ccy = fee, fee_ccy
    if (
        reducing and fee > 0 and fee_ccy != ccy
        and await accts.balance(fee_ccy, AccountKind.cash) < fee
    ):
        rate = await qs.get_fx_rate(fee_ccy, ccy)
        with localcontext() as ctx:
            ctx.prec = _PREC
            charge, charge_ccy = quantize_money(fee * rate, ccy, rounding=ROUND_UP), ccy
    ccy_charge = charge if charge_ccy == ccy else Decimal(0)

    release = (
        await _plan_release(
            session, accts, portfolio.id, symbol=symbol, ccy=ccy, split=split,
            ccy_charge=ccy_charge,
        )
        if buy and held < 0 else Decimal(0)
    )

    needs: dict[str, Decimal] = defaultdict(Decimal)
    incoming: dict[str, Decimal] = defaultdict(Decimal)
    with localcontext() as ctx:
        ctx.prec = _PREC
        if buy:
            needs[ccy] += notional
        else:
            incoming[ccy] += close_notional
        incoming[ccy] += release
        needs[charge_ccy] += charge
    # Ruling R12: a reducing fill may also sell any other cash currency, then borrow.
    other_sources = sorted(
        c for c, bal in (await get_cash_balances(session, portfolio.id)).items()
        if bal > 0 and c not in (base, ccy)
    ) if reducing else []
    legs, borrow = await _plan_funding(
        qs, accts, rule=rule, base=base,
        may_borrow=portfolio.leverage > 1 or reducing or broker,
        other_sources=other_sources, needs=dict(needs), incoming=dict(incoming),
    )

    check_power = not reducing and not broker
    if check_power:  # the post-fill valuation needs quotes and fx rates (I2)
        await prefetch_portfolio(session, qs, portfolio.id, symbols=[symbol])
    qs.freeze()  # chain lock from here to commit: no more provider calls

    fill_id = uuid.uuid4()
    journals: list[Journal] = []
    post = {
        "author_type": "system", "author_id": order.agent_id,
        "order_id": order.id, "fill_id": fill_id,
    }
    if legs:
        journals.append(await post_journal(
            session, type=JournalType.FX, lines=await _fx_lines(accts, legs), **post,
        ))

    qty_delta = qty if buy else qty.copy_negate()  # into the position
    lines = [
        LineSpec(await accts.id(symbol, AccountKind.position), qty_delta, price, ccy),
        LineSpec(
            await accts.id(symbol, AccountKind.system_equity, system=True),
            qty_delta.copy_negate(),
        ),
    ]
    if release > 0:
        lines += [
            LineSpec(await accts.id(ccy, AccountKind.collateral), release.copy_negate()),
            LineSpec(await accts.id(ccy, AccountKind.cash), release),
        ]
    # buy: cash[ccy] pays the whole notional; sell: the closed long's proceeds go to cash,
    # the short's proceeds to collateral
    flows = (
        [(AccountKind.cash, notional.copy_negate())] if buy
        else [(AccountKind.cash, close_notional), (AccountKind.collateral, open_notional)]
    )
    for kind, amount in flows:
        if amount != 0:
            lines += [
                LineSpec(await accts.id(ccy, kind), amount),
                LineSpec(
                    await accts.id(ccy, AccountKind.system_equity, system=True),
                    amount.copy_negate(),
                ),
            ]
    if charge > 0:
        lines += [
            LineSpec(await accts.id(charge_ccy, AccountKind.cash), charge.copy_negate()),
            LineSpec(await accts.id(charge_ccy, AccountKind.system_fees, system=True), charge),
        ]
    for loan_ccy, amount in sorted(borrow.items()):
        amount = quantize_money(amount, loan_ccy, rounding=ROUND_UP)
        lines += [
            LineSpec(await accts.id(loan_ccy, AccountKind.margin_loan), amount.copy_negate()),
            LineSpec(await accts.id(loan_ccy, AccountKind.cash), amount),
        ]
    if close_notional > 0 and not buy:
        debt = await accts.balance(ccy, AccountKind.margin_loan)
        with localcontext() as ctx:
            ctx.prec = _PREC
            net = close_notional - ccy_charge
            repay = min(net, debt.copy_negate()) if debt < 0 else Decimal(0)
        if repay > 0:
            lines += [
                LineSpec(await accts.id(ccy, AccountKind.cash), repay.copy_negate()),
                LineSpec(await accts.id(ccy, AccountKind.margin_loan), repay),
            ]
    trade = await post_journal(
        session, type=journal_type, lines=lines,
        allow_negative=frozenset({AccountKind.position}) if after < 0 else frozenset(),
        **post,
    )
    journals.append(trade)

    position = await upsert_position(session, portfolio.id, symbol, side, qty, price)

    # Exposure-increasing fills must leave buying power >= 0 (raw, Ruling R8); reducing
    # fills are never blocked; broker fills already happened.
    if check_power:
        valuation = await value_portfolio(session, qs, portfolio.id, redis=redis)
        if valuation.stale_symbols:  # retryable: the processor leaves the order resting
            raise SMTError(
                ErrorCode.PROVIDER_UNAVAILABLE,
                "valuation uses cached quotes for " + ", ".join(valuation.stale_symbols),
                {"portfolio_id": str(portfolio.id), "symbol": symbol},
            )
        if valuation.buying_power_raw < 0:
            raise _no_buying_power(
                "buying power exceeded", buying_power_raw=valuation.buying_power_raw
            )

    fill = Fill(
        id=fill_id, order_id=order.id, portfolio_id=portfolio.id, symbol=symbol, side=side,
        qty=qty, price=price,
        quote_bid=quote.bid if quote else None, quote_ask=quote.ask if quote else None,
        quote_last=quote.last if quote else None, quote_ts=quote.ts if quote else None,
        source=source, fee_amount=charge, fee_currency=charge_ccy, fee_rule_id=fee_rule_id,
        journal_id=trade.id,
    )
    session.add(fill)
    _advance(order, progress, price, final=final or fill_qty is None)
    if order.status == OrderStatus.filled:
        await activate_linked(session, order)
    await session.flush()

    queue_event(
        session,
        "order.filled" if order.status == OrderStatus.filled else "order.partially_filled",
        {
            "order_id": order.id, "fill_id": fill.id, "portfolio_id": portfolio.id,
            "agent_id": order.agent_id, "symbol": symbol, "side": side, "qty": qty,
            "price": price, "notional": notional, "currency": ccy, "fee": charge,
            "fee_currency": charge_ccy, "position_qty": position.qty,
            "filled_qty": order.filled_qty, "source": source,
        },
    )
    if held == 0 and position.qty != 0:
        queue_track(session, symbol, "positions")
    elif held != 0 and position.qty == 0:
        queue_untrack(session, symbol, "positions")
    return FillResult(order=order, fill=fill, journals=journals)
