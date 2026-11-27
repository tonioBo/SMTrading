"""The entries of the `scalper` (intraday mode B): the guards, then a bracket order through
the existing order path (`place_bracket_order`, risk checks included), written in the
database only; BrokerSync, woken after the commit, is the one that sends it.

One entry per trigger: the `auto_trades` row of (rule, trigger_key) is written in the
same transaction as the order, before it, so a trigger already worked (a repeated intent,
a restart after the commit) hits the unique key and sends nothing. Every automatic entry
is a `day_trade` for equities (risk step 10: own cap, day leverage, broker buying power)
and carries its `auto_rule_id` (step 11: the rule owns the symbol). The exits are the
bracket's children: `held` until the fill, then kept and watched by the existing exit
engine (Alpaca: an OCO pair kept in SMTrading), even with the `scalper` stopped. The
scalper never modifies its entries (`modify_order` would drop the `auto_rule_id`).

At send time, under the portfolio lock, the switch, the rule's status and its version are
read again (the scalper's snapshot is up to `reload_s` old): a changed one sends
nothing (`inactive` counted). A risk refusal is stored once (the rejected trade and
entry) and the rule makes no new attempt for REJECT_COOLDOWN_S: the repeats are only
counted (`rejected`), never new rows.

No transaction is open across a network call: the guards read Redis and one short
query, the order path reads the quote cache; the wake is published after the commit.
`enter` never raises: a failure is logged and the rule stays flat."""
import hashlib
import logging
import uuid
from collections.abc import Callable
from datetime import datetime, timedelta
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal

from sqlalchemy import exists, or_, select
from sqlalchemy.exc import IntegrityError

from smtrading.autorules import guards
from smtrading.autorules.feed import read_stream_status, stream_state
from smtrading.autorules.guards import INACTIVE, SIZE, check_entry, count_blocked
from smtrading.autorules.quotes import CacheQuoteSource
from smtrading.autorules.rules import ActiveRule
from smtrading.autorules.runner import Entry, RuleRunner
from smtrading.autorules.tables import ACTIVE, OPEN, REJECTED, SENT, AutoRule, AutoTrade
from smtrading.brokers.sync import HEARTBEAT_KEY, wake_broker_sync
from smtrading.common.errors import SMTError
from smtrading.engine.models import OPEN_STATUSES, Order, OrderSide, OrderType, TimeInForce
from smtrading.engine.service import PlaceOrderRequest, place_bracket_order
from smtrading.events import POST_COMMIT_KEY, dispatch_post_commit, json_safe
from smtrading.instruments.market_hours import market_status
from smtrading.instruments.models import AssetClass, Instrument
from smtrading.marketdata.types import Quote
from smtrading.portfolios.locks import lock_portfolio
from smtrading.portfolios.models import PortfolioStatus

log = logging.getLogger(__name__)

US_EQUITY = str(AssetClass.us_equity)
TRIGGER_CONSTRAINT = "uq_auto_trades_rule_trigger"
# after a risk refusal the rule makes no new attempt for this long (the refusal's cause,
# a kill switch or a used-up budget, rarely ends within seconds): one rejected row per
# cooldown, the repeats only counted (Redis)
REJECT_COOLDOWN_S = 300
REJECT_KEY = "autorules:rejected:{rule_id}"


def trigger_key(rule: ActiveRule, entry: Entry) -> str:
    return f"v{rule.version}:{int(entry.ts.timestamp() * 1_000_000)}"


def client_order_id(rule: ActiveRule, key: str) -> str:
    digest = hashlib.sha1(key.encode(), usedforsecurity=False).hexdigest()[:12]
    return f"smt-auto-{rule.rule_id.hex[:12]}-{digest}"


def _to_step(value: Decimal, step: Decimal, rounding: str) -> Decimal:
    return (value / step).to_integral_value(rounding) * step


def _ms(d: timedelta) -> int:
    return (d.days * 86400 + d.seconds) * 1000 + d.microseconds // 1000


def _error_text(e: SMTError) -> str:
    """`CODE (reason): message`, for `auto_trades.error`."""
    reason = (e.details or {}).get("reason")
    code = f"{e.code} ({reason})" if reason else str(e.code)
    return f"{code}: {e.message}"[:200]


def _parse_ts(raw) -> datetime | None:
    if raw is None:
        return None
    try:
        ts = datetime.fromisoformat(raw.decode() if isinstance(raw, bytes) else raw)
    except ValueError:
        return None
    return ts if ts.tzinfo is not None else None


class Executor:
    def __init__(self, sessionmaker, redis, qs: CacheQuoteSource, *,
                 clock: Callable[[], datetime]) -> None:
        self._sm = sessionmaker
        self._redis = redis
        self.qs = qs
        self._clock = clock

    # --- the guards --------------------------------------------------------------

    async def _busy(self, rule: ActiveRule) -> bool:
        """A trade of the rule (any version) still `sent`/`open`, or an open order of the
        rule: one query."""
        async with self._sm() as session:
            return bool(await session.scalar(select(or_(
                exists().where(AutoTrade.rule_id == rule.rule_id,
                               AutoTrade.status.in_((SENT, OPEN))),
                exists().where(Order.auto_rule_id == rule.rule_id,
                               Order.status.in_(OPEN_STATUSES)),
            ))))

    async def _blocked(self, rule: ActiveRule, quote: Quote, now: datetime) -> str | None:
        if await self._redis.exists(REJECT_KEY.format(rule_id=rule.rule_id)):
            return guards.REJECTED
        status = await read_stream_status(self._redis)
        last_pass = _parse_ts(await self._redis.get(HEARTBEAT_KEY))
        args = {"quote": quote, "now": now, "params": rule.params,
                "market_open": market_status(rule.market, now).is_open,
                "stream": stream_state(status, rule.symbol), "broker_last_pass": last_pass}
        reason = check_entry(busy=False, **args)
        if reason is None:  # the database is asked only when nothing else blocks
            reason = check_entry(busy=await self._busy(rule), **args)
        return reason

    # --- the entry ---------------------------------------------------------------

    async def enter(self, rule: ActiveRule, runner: RuleRunner, entry: Entry,
                    quote: Quote) -> AutoTrade | None:
        """The trade of this trigger (`sent`, or `rejected` by the risk checks), or None
        (blocked, too small, already worked, or a failure, logged)."""
        try:
            return await self._enter(rule, runner, entry, quote)
        except Exception:
            log.exception("automatic rule %s: entry on %s failed", rule.rule_id, rule.symbol)
            return None

    async def _count(self, rule: ActiveRule, now: datetime, reason: str) -> None:
        try:
            await count_blocked(self._redis, rule.rule_id, now, reason)
        except Exception:
            log.warning("automatic rule %s: blocked entry not counted", rule.rule_id,
                        exc_info=True)

    def _request(self, rule: ActiveRule, qty: Decimal, expected: Decimal,
                 coid: str) -> PlaceOrderRequest:
        limit = None
        if rule.params["entry"] == "limit":
            limit = _to_step(expected * (1 + rule.params["limit_offset_pct"] / 100),
                             rule.tick_size, ROUND_CEILING)
        equity = rule.asset_class == US_EQUITY
        return PlaceOrderRequest(
            portfolio_id=rule.portfolio_id, symbol=rule.symbol, side=OrderSide.buy,
            type=OrderType.limit if limit is not None else OrderType.market,
            tif=TimeInForce.day if equity else TimeInForce.gtc, qty=qty,
            limit_price=limit, source="system", agent_id=None, client_order_id=coid,
            day_trade=equity, auto_rule_id=rule.rule_id)

    async def _enter(self, rule: ActiveRule, runner: RuleRunner, entry: Entry,
                     quote: Quote) -> AutoTrade | None:
        now = self._clock()
        reason = await self._blocked(rule, quote, now)
        if reason is not None:
            await self._count(rule, now, reason)
            return None
        key = trigger_key(rule, entry)
        expected = quote.ask
        async with self._sm() as session:
            if rule.qty is not None:
                qty = rule.qty
            else:
                lot = (await session.get_one(Instrument, rule.symbol)).lot_size
                qty = _to_step(rule.notional / expected, lot, ROUND_FLOOR)
            if qty <= 0:
                await self._count(rule, now, SIZE)
                return None
            stop, target = runner.levels(expected)
            req = self._request(rule, qty, expected, client_order_id(rule, key))
            trade = AutoTrade(
                rule_id=rule.rule_id, rule_version=rule.version,
                portfolio_id=rule.portfolio_id, symbol=rule.symbol, trigger_key=key,
                trigger_at=entry.ts, trigger_price=entry.price, trigger_bid=quote.bid,
                trigger_ask=quote.ask, trigger_values=json_safe(entry.values),
                expected_price=expected, status=SENT, qty=qty)
            # R11: the portfolio row first (the order path takes it again), then the rows
            portfolio = await lock_portfolio(session, rule.portfolio_id)
            if not await self._still_active(session, rule, portfolio):
                await session.rollback()
                await self._count(rule, now, INACTIVE)
                log.info("automatic rule %s: no longer active at send time", rule.rule_id)
                return None
            session.add(trade)
            try:
                async with session.begin_nested():
                    await session.flush()
            except IntegrityError as e:
                await session.rollback()
                if TRIGGER_CONSTRAINT not in str(e.orig):
                    raise  # logged by enter as an error
                log.info("automatic rule %s: trigger %s already worked", rule.rule_id, key)
                return None
            error = await self._place(session, req, target, stop, now, trade)
            if error is not None:
                trade.status, trade.error = REJECTED, _error_text(error)
            else:
                trade.sent_at = self._clock()
                trade.ms_trigger_to_send = _ms(trade.sent_at - entry.ts)
            await session.commit()
            await dispatch_post_commit(session, self._redis)
        if error is not None:
            log.info("automatic rule %s: entry on %s refused (%s); no new attempt for %s s",
                     rule.rule_id, rule.symbol, trade.error, REJECT_COOLDOWN_S)
            try:
                await self._redis.set(REJECT_KEY.format(rule_id=rule.rule_id),
                                      str(error.code), ex=REJECT_COOLDOWN_S)
            except Exception:
                log.warning("automatic rule %s: refusal cooldown not set", rule.rule_id,
                            exc_info=True)
            return trade
        try:
            await wake_broker_sync(self._redis, trade.entry_order_id)
        except Exception:  # the next full pass sends it
            log.warning("automatic rule %s: BrokerSync not woken", rule.rule_id,
                        exc_info=True)
        runner.sent(now)
        return trade

    async def _still_active(self, session, rule: ActiveRule, portfolio) -> bool:
        """Under the portfolio lock: the switch still on, the portfolio not archived, the
        rule still active at the version of the snapshot (the scalper's is up to
        `reload_s` old)."""
        if not portfolio.auto_rules_enabled or portfolio.status == PortfolioStatus.archived:
            return False
        row = (await session.execute(
            select(AutoRule.status, AutoRule.version).where(AutoRule.id == rule.rule_id)
        )).one_or_none()
        return row is not None and row.status == ACTIVE and row.version == rule.version

    async def _place(self, session, req: PlaceOrderRequest, target: Decimal, stop: Decimal,
                     now: datetime, trade: AutoTrade) -> SMTError | None:
        """The bracket in a savepoint: a refusal the engine stored (`persisted`: the
        rejected entry) stays; any other refusal leaves nothing behind."""
        effects = session.info.setdefault(POST_COMMIT_KEY, [])
        mark = len(effects)
        try:
            async with session.begin_nested():
                try:
                    orders = await place_bracket_order(
                        session, self.qs, req, take_profit=target, stop_loss=stop, now=now,
                        redis=self._redis)
                except SMTError as e:
                    if not e.persisted:
                        raise
                    order_id = (e.details or {}).get("order_id")
                    trade.entry_order_id = uuid.UUID(str(order_id)) if order_id else None
                    return e
        except SMTError as e:
            del session.info.setdefault(POST_COMMIT_KEY, [])[mark:]
            return e
        trade.entry_order_id = orders[0].id
        return None
