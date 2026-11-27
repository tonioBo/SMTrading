"""The brakes of mode B, per rule (`auto_rule_versions.brakes`) and per portfolio
(`portfolios.auto_brakes`, `validate_brakes(level="portfolio")`).

- Caps, checked before every entry (`blocked`): trades per hour (the last 60 minutes) and
  per day, open positions (trades `sent` or `open`), for the rule and for the portfolio.
  A cap reached blocks the entry (`braked`, counted by the executor) and changes no
  status: the rule enters again once under the cap.
- Brakes that stop, judged after every closed trade (`after_close`) on the closed trades
  of the day and their net results in the portfolio's currency: losses in a row (the
  newest closed trades with `net_result < 0`; a win or a break-even ends the run), the
  day's loss reaching `max_loss_day` or `max_loss_day_pct` % of the day's opening equity
  (the `day_open` snapshot, `equity_eur` converted at the portfolio's rate). The rule's
  brakes stop the rule; the portfolio's stop every active rule of the portfolio
  («fermata dai freni», `rules.brake`) until someone resumes it by hand. The rule's
  losses in a row count only the closes since its last activation (a resume starts a
  new run); the day's losses count the whole day (a rule resumed on a day already over
  its limit stops again at its next close). One Telegram notice (`source="system"`) per
  trip: per rule, day, reason and activation (`job_runs`), so a rule resumed by hand
  and stopped again is announced again.
- The day is the portfolio's local day (midnight in `portfolios.timezone`, as
  `risk.daily_loss.next_local_midnight`).

A brake never closes a position: an open trade keeps its stop, target and the scalper's
time and end-of-session exits; the exits never pass through here. The kill switch stays
the global brake (the engine's risk checks).

Fail closed for entries: a failed check blocks the entry (`braked`), and a close whose
brakes could not be judged blocks every entry of its portfolio until it is judged (tried
again at the next entry): its failure is logged at most every FAILURE_LOG_EVERY, and one
Telegram notice says the portfolio's automatic entries are blocked. Nothing here raises
to the scalper. The reads come first, the
exchange rates with no session open, then one short transaction (portfolio row lock, then
the rules', R11) for the brakes and their notices."""
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from sqlalchemy import func, or_, select

from smtrading.autorules.guards import BRAKED as BLOCKED_BRAKED
from smtrading.autorules.models import Brakes, validate_brakes
from smtrading.autorules.rules import ActiveRule, brake
from smtrading.autorules.tables import (
    ACTIVE,
    BRAKED,
    CLOSED,
    OPEN,
    SENT,
    AutoRule,
    AutoRuleVersion,
    AutoTrade,
)
from smtrading.corporate.snapshots import ensure_day_open, local_date
from smtrading.engine.prefetch import prefetch_portfolio
from smtrading.engine.quotesource import MemoQuoteSource
from smtrading.events import dispatch_post_commit
from smtrading.jobs.runs import run_job_once
from smtrading.portfolios.locks import lock_portfolio
from smtrading.portfolios.models import Portfolio
from smtrading.risk.models import EquitySnapshot, SnapshotKind
from smtrading.telegram.outbox import enqueue

log = logging.getLogger(__name__)

BRAKE_JOB = "autorules_brake"
PENDING_JOB = "autorules_brake_pending"
FAILURE_LOG_EVERY = timedelta(minutes=10)
HOUR = timedelta(hours=1)
COUNTED = (SENT, OPEN, CLOSED)  # a trade that reached the market (not refused, canceled)
BUSY = (SENT, OPEN)
EUR = "EUR"


@dataclass(frozen=True)
class Trip:
    code: str  # stable, for the once-per-day notice: "{scope}_{brake}"
    reason: str  # Italian, the rule's `status_reason`


def day_start(timezone: str, now: datetime) -> datetime:
    """The last local midnight of the portfolio's time zone."""
    zone = ZoneInfo(timezone)
    return datetime.combine(now.astimezone(zone).date(), time(0), tzinfo=zone)


def _money(d: Decimal) -> str:
    """1234.5 → «1.234,50»."""
    return f"{d:,.2f}".replace(",", "_").replace(".", ",").replace("_", ".")


def _pct(d: Decimal) -> str:
    return format(d.normalize(), "f").replace(".", ",")


def _losing_run(nets: list[Decimal]) -> int:
    """The losses in a row at the start of `nets` (newest first)."""
    n = 0
    for net in nets:
        if not net < 0:
            break
        n += 1
    return n


def stop_trip(brakes: Brakes, nets: list[Decimal], *, currency: str,
              opening: Decimal | None, portfolio: bool,
              streak: list[Decimal] | None = None) -> Trip | None:
    """The first brake that stops for the closed trades' results `nets` of the day (newest
    first, in `currency`), or None. `opening`: the day's opening equity in `currency`;
    `streak`: the results the losses in a row are counted on (default `nets`)."""
    scope = "portfolio" if portfolio else "rule"
    whose = " del portafoglio" if portfolio else ""
    if brakes.max_consecutive_losses is not None:
        run = _losing_run(nets if streak is None else streak)
        if run >= brakes.max_consecutive_losses:
            where = " nel portafoglio" if portfolio else ""
            losses = "perdita" if run == 1 else "perdite"
            return Trip(f"{scope}_losses", f"{run} {losses} di fila{where}")
    loss = -sum(nets, Decimal(0))
    if not loss > 0:
        return None
    if brakes.max_loss_day is not None and loss >= brakes.max_loss_day:
        return Trip(f"{scope}_loss_day",
                    f"perdita del giorno{whose} {_money(loss)} {currency} "
                    f"(limite {_money(brakes.max_loss_day)} {currency})")
    if brakes.max_loss_day_pct is not None and opening is not None and opening > 0:
        limit = opening * brakes.max_loss_day_pct / 100
        if loss >= limit:
            return Trip(f"{scope}_loss_day_pct",
                        f"perdita del giorno{whose} {_money(loss)} {currency} (limite "
                        f"{_pct(brakes.max_loss_day_pct)}% del patrimonio d'apertura, "
                        f"{_money(limit)} {currency})")
    return None


def notice_text(portfolio: str, rule: AutoRule, trip: Trip) -> str:
    return (f"Regola automatica fermata dai freni · {portfolio} · {rule.symbol} "
            f"({rule.model} v{rule.version}): {trip.reason}. "
            "Riattivala a mano dal portale quando vuoi.")


def pending_text(portfolio: str) -> str:
    return (f"Regole automatiche · {portfolio}: non è stato possibile valutare i freni "
            "dopo un'operazione chiusa. Le nuove entrate automatiche del portafoglio "
            "restano bloccate finché la valutazione non riesce; le uscite continuano.")


@dataclass
class _Day:
    """What the brakes of a close are judged on (read in one session)."""
    name: str
    timezone: str
    currency: str
    rule_brakes: Brakes | None  # None: the rule (or its version) is gone
    activated_at: datetime | None  # the rule's last activation (while active)
    portfolio_brakes: Brakes
    closed: list[tuple[uuid.UUID, Decimal, str | None, datetime]]  # newest first
    opening_eur: Decimal | None


class BrakeChecker:
    def __init__(self, sessionmaker, qs, *, clock, redis=None) -> None:
        self._sm = sessionmaker
        self.qs = qs
        self._clock = clock
        self._redis = redis  # for the stream's untrack after a brake (post-commit)
        # closes whose brakes could not be judged: portfolio -> trade id -> trade
        self._pending: dict[uuid.UUID, dict[int, AutoTrade]] = {}
        self._logged: dict[object, datetime] = {}  # failure -> last logged
        self._noticed: set[int] = set()  # pending trades whose notice is queued

    def _log_due(self, key: object, now: datetime) -> bool:
        """True at most once every FAILURE_LOG_EVERY per `key`."""
        last = self._logged.get(key)
        if last is not None and now - last < FAILURE_LOG_EVERY:
            return False
        self._logged[key] = now
        return True

    # --- before an entry -----------------------------------------------------------

    async def blocked(self, rule: ActiveRule, now: datetime) -> str | None:
        """`braked` when a cap of the rule or the portfolio is reached, the rule is
        already braked, a close of the portfolio is still to be judged, or the check
        fails; None otherwise."""
        try:
            if not await self._judged(rule.portfolio_id, now):
                return BLOCKED_BRAKED
            return await self._caps(rule, now)
        except Exception:
            if self._log_due(("caps", rule.rule_id), now):
                log.warning("automatic rule %s: brake check failed; entries blocked",
                            rule.rule_id, exc_info=True)
            return BLOCKED_BRAKED

    async def _judged(self, portfolio_id: uuid.UUID, now: datetime) -> bool:
        pending = self._pending.get(portfolio_id)
        if pending:
            for trade in list(pending.values()):
                await self.after_close(trade, now)
        return not self._pending.get(portfolio_id)

    async def _caps(self, rule: ActiveRule, now: datetime) -> str | None:
        async with self._sm() as s:
            row = (await s.execute(
                select(Portfolio.timezone, AutoRule.status)
                .join(AutoRule, AutoRule.portfolio_id == Portfolio.id)
                .where(AutoRule.id == rule.rule_id))).one_or_none()
            if row is None:
                return None  # gone: the send-time check says so
            if row.status == BRAKED:
                return BLOCKED_BRAKED
            start, hour = day_start(row.timezone, now), now - HOUR
            mine = AutoTrade.rule_id == rule.rule_id
            counted = AutoTrade.status.in_(COUNTED)
            busy = AutoTrade.status.in_(BUSY)

            def n(cond):
                return func.count().filter(cond)

            counts = (await s.execute(
                select(n(mine & counted & (AutoTrade.trigger_at >= hour)),
                       n(mine & counted & (AutoTrade.trigger_at >= start)),
                       n(mine & busy),
                       n(counted & (AutoTrade.trigger_at >= hour)),
                       n(counted & (AutoTrade.trigger_at >= start)),
                       n(busy))
                .where(AutoTrade.portfolio_id == rule.portfolio_id,
                       or_(AutoTrade.trigger_at >= min(start, hour), busy)))).one()
        for brakes, (per_hour, per_day, held) in (
                (rule.brakes, counts[:3]), (rule.portfolio_brakes, counts[3:])):
            for cap, value in ((brakes.max_trades_hour, per_hour),
                               (brakes.max_trades_day, per_day),
                               (brakes.max_open_positions, held)):
                if cap is not None and value >= cap:
                    return BLOCKED_BRAKED
        return None

    # --- after a close -------------------------------------------------------------

    async def after_close(self, trade: AutoTrade, now: datetime) -> None:
        """Judge the brakes that stop after `trade` closed; never raises (a failure keeps
        the portfolio's entries blocked until judged)."""
        try:
            await self._judge(trade, now)
        except Exception:
            if self._log_due(("judge", trade.id), now):
                log.exception("automatic trade %s: brakes not judged; entries of the "
                              "portfolio blocked until they are", trade.id)
            self._pending.setdefault(trade.portfolio_id, {})[trade.id] = trade
            await self._notice_pending(trade)
            return
        self._logged.pop(("judge", trade.id), None)
        self._noticed.discard(trade.id)
        pending = self._pending.get(trade.portfolio_id)
        if pending is not None:
            pending.pop(trade.id, None)
            if not pending:
                del self._pending[trade.portfolio_id]

    async def _notice_pending(self, trade: AutoTrade) -> None:
        """One Telegram notice per trade left to judge (best effort: the database may be
        what failed; tried again at the next failure until queued)."""
        if trade.id in self._noticed:
            return
        try:
            async with self._sm() as s:
                name = await s.scalar(select(Portfolio.name).where(
                    Portfolio.id == trade.portfolio_id))
                if await run_job_once(s, PENDING_JOB, str(trade.id)):
                    await enqueue(s, source="system", category="system", level="warning",
                                  text=pending_text(name or "?"))
                await s.commit()
        except Exception:
            log.debug("automatic trade %s: pending-brakes notice not queued", trade.id,
                      exc_info=True)
            return
        self._noticed.add(trade.id)

    async def _read(self, trade: AutoTrade, now: datetime) -> _Day:
        async with self._sm() as s:
            p = await s.get_one(Portfolio, trade.portfolio_id)
            rule = (await s.execute(
                select(AutoRuleVersion.brakes, AutoRule.status, AutoRule.status_changed_at)
                .join(AutoRule, (AutoRule.id == AutoRuleVersion.rule_id)
                      & (AutoRule.version == AutoRuleVersion.version))
                .where(AutoRule.id == trade.rule_id))).one_or_none()
            raw = None if rule is None else rule.brakes
            activated = (rule.status_changed_at
                         if rule is not None and rule.status == ACTIVE else None)
            start = day_start(p.timezone, now)
            closed = [tuple(r) for r in await s.execute(
                select(AutoTrade.rule_id, AutoTrade.net_result, AutoTrade.net_currency,
                       AutoTrade.exit_filled_at)
                .where(AutoTrade.portfolio_id == p.id, AutoTrade.status == CLOSED,
                       AutoTrade.net_result.is_not(None),
                       AutoTrade.exit_filled_at >= start)
                .order_by(AutoTrade.exit_filled_at.desc(), AutoTrade.id.desc()))]
            opening = await s.scalar(select(EquitySnapshot.equity_eur).where(
                EquitySnapshot.portfolio_id == p.id,
                EquitySnapshot.kind == SnapshotKind.day_open,
                EquitySnapshot.local_date == local_date(p, now)))
            return _Day(p.name, p.timezone, p.base_currency,
                        validate_brakes(raw, level="rule") if raw is not None else None,
                        activated,
                        validate_brakes(p.auto_brakes or {}, level="portfolio"),
                        closed, opening)

    async def _opening_eur(self, portfolio_id: uuid.UUID, now: datetime) -> Decimal:
        """The day's `day_open` snapshot, created when missing (the portfolio's
        valuation, prefetched as the daily-loss check does)."""
        async with self._sm() as s:
            memo = MemoQuoteSource.wrap(self.qs)
            await prefetch_portfolio(s, memo, portfolio_id)
            portfolio = await s.get_one(Portfolio, portfolio_id)
            snap = await ensure_day_open(s, memo, portfolio, now, redis=self._redis)
            await s.commit()
            return snap.equity_eur

    async def _judge(self, trade: AutoTrade, now: datetime) -> None:
        day = await self._read(trade, now)
        # the exchange rates, with no session open
        rates = {c: await self.qs.get_fx_rate(c, day.currency)
                 for c in {c for _, _, c, _ in day.closed if c and c != day.currency}}
        results = [(rid, net * rates.get(c, Decimal(1)), at)
                   for rid, net, c, at in day.closed]
        mine = [net for rid, net, _ in results if rid == trade.rule_id]
        everyone = [net for _, net, _ in results]
        # the run of losses starts again at the rule's last activation (a resume)
        mine_since = [net for rid, net, at in results if rid == trade.rule_id
                      and (day.activated_at is None or at >= day.activated_at)]
        opening = None
        pct_brakes = [b for b in (day.rule_brakes, day.portfolio_brakes)
                      if b is not None and b.max_loss_day_pct is not None]
        if pct_brakes and (sum(mine, Decimal(0)) < 0 or sum(everyone, Decimal(0)) < 0):
            opening_eur = day.opening_eur
            if opening_eur is None:
                opening_eur = await self._opening_eur(trade.portfolio_id, now)
            opening = opening_eur * await self.qs.get_fx_rate(EUR, day.currency)
        rule_trip = None if day.rule_brakes is None else stop_trip(
            day.rule_brakes, mine, currency=day.currency, opening=opening, portfolio=False,
            streak=mine_since)
        portfolio_trip = stop_trip(day.portfolio_brakes, everyone, currency=day.currency,
                                   opening=opening, portfolio=True)
        if rule_trip is None and portfolio_trip is None:
            return
        await self._stop(trade, now, rule_trip, portfolio_trip)

    async def _stop(self, trade: AutoTrade, now: datetime, rule_trip: Trip | None,
                    portfolio_trip: Trip | None) -> None:
        async with self._sm() as s:
            portfolio = await lock_portfolio(s, trade.portfolio_id)  # R11: portfolio first
            active = list(await s.scalars(
                select(AutoRule).where(AutoRule.portfolio_id == portfolio.id,
                                       AutoRule.status == ACTIVE)
                .order_by(AutoRule.id).with_for_update()
                .execution_options(populate_existing=True)))
            targets = [(r, rule_trip) for r in active
                       if rule_trip is not None and r.id == trade.rule_id]
            if portfolio_trip is not None:
                targets += [(r, portfolio_trip) for r in active
                            if r.id != trade.rule_id or rule_trip is None]
            if not targets:
                await s.rollback()
                return
            day = local_date(portfolio, now).isoformat()
            for rule, trip in targets:
                activated = int(rule.status_changed_at.timestamp() * 1_000_000)
                await brake(s, rule.id, trip.reason)
                key = f"{rule.id}:{day}:{trip.code}:{activated}"
                if await run_job_once(s, BRAKE_JOB, key):
                    await enqueue(s, source="system", category="system", level="warning",
                                  text=notice_text(portfolio.name, rule, trip))
            await s.commit()
            await dispatch_post_commit(s, self._redis)
        for rule, trip in targets:
            log.warning("automatic rule %s on %s stopped by the brakes: %s", rule.id,
                        rule.symbol, trip.code)
