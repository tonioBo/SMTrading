"""Corporate actions with ex-date `day`: cash dividends and splits on held positions.

Each portfolio is processed in its own transaction (portfolio row locked first, in id
order); every (symbol, day, kind, portfolio) is claimed in job_runs (`corp`) so it is
applied at most once, and the whole day is marked `corporate / {day}` once every
portfolio succeeded.

Ruling R21 (I5): the calendar is read for held symbols and symbols with open orders; on
a split every open order on the symbol is canceled, in every portfolio, and each
portfolio is notified (`split_orders_canceled`). Orders are not rescaled. Redis effects
of the cancels are dispatched after each commit when `redis` is given.
"""

import logging
from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_EVEN, Decimal, InvalidOperation, localcontext
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from smtrading.common.money import quantize_money
from smtrading.corporate.interest import account_id, debit_lines
from smtrading.engine.models import OPEN_STATUSES, Order
from smtrading.engine.quotesource import QuoteSource
from smtrading.engine.service import cancel_all_orders
from smtrading.events import discard_post_commit, dispatch_post_commit, notify
from smtrading.instruments.models import Instrument
from smtrading.jobs.runs import job_done, run_job_once
from smtrading.ledger.models import Account, AccountBalance, AccountKind, JournalType
from smtrading.ledger.service import LineSpec, get_balance, post_journal
from smtrading.marketdata.types import CalendarEvent
from smtrading.portfolios.locks import lock_portfolio
from smtrading.positions.models import Position

log = logging.getLogger("smtrading.corporate.actions")

JOB = "corporate"
EVENT_JOB = "corp"
AUTHOR = "corporate"
KINDS = ("dividend", "split")  # dividends first: they are paid on the pre-split quantity
_PREC = 60
_Q18 = Decimal(1).scaleb(-18)


@dataclass(frozen=True)
class _Inst:
    symbol: str
    currency: str


def _positive(value) -> Decimal | None:
    try:
        d = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None
    return d if d.is_finite() and d > 0 else None


async def _dividend(
    session: AsyncSession, portfolio_id: UUID, inst: _Inst, qty: Decimal,
    event: CalendarEvent,
) -> bool:
    amount = _positive(event.data.get("amount"))
    if amount is None:
        log.warning("dividend of %s on %s has no valid amount: %s",
                    inst.symbol, event.date, event.data)
        return False
    ccy = (event.data.get("currency") or inst.currency).upper()
    with localcontext() as ctx:
        ctx.prec = _PREC
        total = quantize_money(abs(qty) * amount, ccy)
    if total <= 0:
        return False
    if qty > 0:  # long: credited
        lines = [
            LineSpec(await account_id(session, portfolio_id, ccy, AccountKind.cash), total),
            LineSpec(
                await account_id(session, None, ccy, AccountKind.system_equity),
                total.copy_negate(),
            ),
        ]
    else:  # short: the lender's dividend is charged
        lines = await debit_lines(session, portfolio_id, ccy, total, AccountKind.system_equity)
    await post_journal(
        session, type=JournalType.DIVIDEND, lines=lines, author_type="system",
        author_id=AUTHOR, reason=f"dividend {inst.symbol} {amount} {ccy} x {qty} {event.date}",
    )
    return True


async def _split(
    session: AsyncSession, portfolio_id: UUID, inst: _Inst, qty: Decimal,
    event: CalendarEvent,
) -> bool:
    ratio = _positive(event.data.get("ratio"))
    if ratio is None:
        log.warning("split of %s on %s has no valid ratio: %s",
                    inst.symbol, event.date, event.data)
        return False
    with localcontext() as ctx:
        ctx.prec = _PREC
        delta = (qty * (ratio - 1)).quantize(_Q18, rounding=ROUND_HALF_EVEN)
    if delta == 0:
        return False
    await post_journal(
        session, type=JournalType.CORPORATE_ACTION,
        lines=[
            LineSpec(await account_id(session, portfolio_id, inst.symbol, AccountKind.position),
                     delta),
            LineSpec(await account_id(session, None, inst.symbol, AccountKind.system_equity),
                     delta.copy_negate()),
        ],
        allow_negative=frozenset({AccountKind.position}) if qty < 0 else frozenset(),
        author_type="system", author_id=AUTHOR,
        reason=f"split {inst.symbol} ratio {ratio} {event.date}",
    )
    position = (
        await session.execute(
            select(Position)
            .where(Position.portfolio_id == portfolio_id, Position.symbol == inst.symbol)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if position is None:
        log.warning("split of %s: portfolio %s has no projection row", inst.symbol, portfolio_id)
        return True
    with localcontext() as ctx:
        ctx.prec = _PREC
        position.qty = qty + delta
        position.avg_price = (position.avg_price / ratio).quantize(
            _Q18, rounding=ROUND_HALF_EVEN
        )
    await session.flush()
    return True


_APPLY = {"dividend": _dividend, "split": _split}


async def _apply_portfolio(
    session: AsyncSession, portfolio_id: UUID, held: list[str], split_orders: list[str],
    events: dict[str, list[CalendarEvent]], instruments: dict[str, _Inst], day: date,
) -> int:
    """Dividends/splits on the `held` symbols; cancel the open orders on the
    `split_orders` symbols (Ruling R21). The caller commits."""
    await lock_portfolio(session, portfolio_id)
    journals = 0
    for symbol in held:
        for event in events.get(symbol, []):
            key = f"{symbol}:{day.isoformat()}:{event.kind}:{portfolio_id}"
            if not await run_job_once(session, EVENT_JOB, key):
                continue
            # read after the lock: the ledger is the source of truth for quantities
            qty = await get_balance(
                session, await account_id(session, portfolio_id, symbol, AccountKind.position)
            )
            if qty == 0:
                continue
            if await _APPLY[event.kind](session, portfolio_id, instruments[symbol], qty, event):
                journals += 1
    for symbol in split_orders:
        canceled = await cancel_all_orders(session, portfolio_id, symbol, by="system")
        if canceled:
            split = next(e for e in events[symbol] if e.kind == "split")
            await notify(
                session, portfolio_id=portfolio_id, kind="split_orders_canceled",
                payload={
                    "symbol": symbol, "date": day, "ratio": split.data.get("ratio"),
                    "canceled": canceled,
                },
            )
            log.warning("split of %s on %s: %s open orders of portfolio %s canceled",
                        symbol, day, canceled, portfolio_id)
    return journals


async def run_corporate_actions(
    session: AsyncSession, qs: QuoteSource, day: date, *, redis=None
) -> int:
    """Apply the dividends and splits with ex-date `day`; returns the journals created.

    Commits once per portfolio. A failing portfolio is rolled back, logged and left for
    the next run; the day is marked done only when none failed.
    """
    key = day.isoformat()
    if await job_done(session, JOB, key):
        return 0
    rows = (await session.execute(
        select(Account.portfolio_id, Account.asset)
        .join(AccountBalance, AccountBalance.account_id == Account.id)
        .where(
            Account.portfolio_id.is_not(None), Account.kind == AccountKind.position,
            AccountBalance.balance != 0,
        )
        .order_by(Account.portfolio_id, Account.asset)
    )).all()
    holdings: dict[UUID, list[str]] = defaultdict(list)
    for portfolio_id, symbol in rows:
        holdings[portfolio_id].append(symbol)
    open_orders: dict[UUID, set[str]] = defaultdict(set)
    for portfolio_id, symbol in (await session.execute(
        select(Order.portfolio_id, Order.symbol).distinct()
        .where(Order.status.in_(OPEN_STATUSES))
    )).all():
        open_orders[portfolio_id].add(symbol)
    symbols = sorted(
        {s for held in holdings.values() for s in held}
        | {s for syms in open_orders.values() for s in syms}
    )

    events: dict[str, list[CalendarEvent]] = defaultdict(list)
    instruments: dict[str, _Inst] = {}
    if symbols:
        for e in await qs.get_calendar(symbols, day, day):
            if e.date == day and e.kind in KINDS:
                events[e.symbol].append(e)
        for evs in events.values():
            evs.sort(key=lambda e: KINDS.index(e.kind))
        instruments = {
            symbol: _Inst(symbol, ccy) for symbol, ccy in (await session.execute(
                select(Instrument.symbol, Instrument.currency)
                .where(Instrument.symbol.in_(list(events)))
            )).all()
        }
    await session.commit()
    splits = {s for s, evs in events.items() if any(e.kind == "split" for e in evs)}

    total, failed = 0, False
    for portfolio_id in sorted(set(holdings) | set(open_orders)):
        affected = [s for s in holdings.get(portfolio_id, []) if s in events]
        split_orders = sorted(open_orders.get(portfolio_id, set()) & splits)
        if not affected and not split_orders:
            continue
        try:
            total += await _apply_portfolio(
                session, portfolio_id, affected, split_orders, events, instruments, day
            )
            await session.commit()
        except Exception:
            await session.rollback()
            discard_post_commit(session)
            failed = True
            log.exception("corporate actions of portfolio %s for %s failed", portfolio_id, key)
            continue
        await dispatch_post_commit(session, redis)
    if not failed:
        await run_job_once(session, JOB, key)
        await session.commit()
    return total
